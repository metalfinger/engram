"""Engram auto-presence hook.

Drops a plain-JSON presence record into the Engram spool dir on SessionStart /
UserPromptSubmit / SessionEnd. The server ingests the spool through its own git
lock (see engram_server/presence_spool.py) -- this hook NEVER touches the brain
checkout or runs git-writes, so it can't race the server's commit lock.

Failure-soft by design: any error exits 0 so a broken hook never blocks Claude.
Reads the Claude Code hook payload (session_id, hook_event_name, cwd) from stdin.
"""

import json
import os
import socket
import subprocess
import sys
import time

SPOOL_DIR = os.path.join(os.path.expanduser("~"), ".engram", "presence-spool")


UPLOAD_CONF = os.path.join(os.path.expanduser("~"), ".engram", "upload.json")


def _upload(record):
    """POST this record to Engram from a machine that is NOT the server's.

    Returns True when the record was accepted (so the caller skips the spool),
    False when upload is not configured or did not work — on the server's own
    machine that is the normal path and the spool takes over.

    Config: ~/.engram/upload.json = {"url": "https://engram.example", "token": "..."}
    Same place the deploy key and CF token already live. The token is never printed.

    A FAILED UPLOAD IS DROPPED, not queued. This is a heartbeat: the next one is
    seconds away, and a stale presence record is worse than a missing one — it
    would claim a session was somewhere it has since left.
    """
    try:
        with open(UPLOAD_CONF, "r", encoding="utf-8") as f:
            conf = json.load(f)
        url = str(conf.get("url") or "").rstrip("/")
        token = str(conf.get("token") or "")
        if not url or not token:
            return False
    except Exception:
        return False

    try:
        import urllib.request

        body = json.dumps({"records": [record]}).encode("utf-8")
        req = urllib.request.Request(
            url + "/dashboard/api/presence/upload",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )
        # Short timeout on purpose: this runs on every tool call, and a hook that
        # hangs is worse than a roster that lags.
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _git(cwd, *args):
    """Read-only git query; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def _detect_project(cwd, toplevel):
    """Read a `.engram-project` pin so a repo/dir can attach its sessions to a
    default Engram project (shown as that project's room in the live office).

    Walk up from cwd to the repo top (or filesystem root) looking for a
    `.engram-project` file; its first non-empty line is the project id. Empty on
    miss. Purely a home-room hint — it never restricts what the session can access.
    """
    try:
        cur = os.path.abspath(cwd)
        stop = os.path.abspath(toplevel) if toplevel else None
        while True:
            pin = os.path.join(cur, ".engram-project")
            if os.path.isfile(pin):
                with open(pin, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            return line
                return ""
            parent = os.path.dirname(cur)
            if cur == stop or parent == cur:
                break
            cur = parent
    except Exception:
        pass
    return ""


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = data.get("session_id", "")
    event = data.get("hook_event_name", "")
    cwd = data.get("cwd") or os.getcwd()
    if not session_id or not event:
        return

    # Status vocab must match the server's (working|idle|blocked|available|done);
    # anything else coerces to "working", so write the canonical value directly.
    # PostToolUse heartbeats while Claude grinds through long turns (no user prompt
    # needed); Stop marks the turn done -> available (idling at the desk, still live).
    if event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
        status = "working"
    elif event == "Stop":
        status = "available"
    elif event == "SessionEnd":
        status = "done"
    else:
        return

    # Read-only git detection (all empty if cwd isn't a repo).
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    repo = os.path.basename(toplevel) if toplevel else ""
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") if toplevel else ""
    repo_remote = _git(cwd, "remote", "get-url", "origin") if toplevel else ""
    host = socket.gethostname()
    project = _detect_project(cwd, toplevel)  # `.engram-project` pin → home room

    # Friendly display name: host + repo (or cwd basename when not a repo).
    where = repo or os.path.basename(cwd.rstrip("/\\")) or cwd
    name = "{}/{}".format(host, where)

    record = {
        "session": session_id,
        "name": name,
        "cwd": cwd.replace("\\", "/"),
        "repo": repo,
        "branch": branch,
        "repo_remote": repo_remote,
        "host": host,
        "project": project,
        "status": status,
        "ts": int(time.time()),
    }

    # REMOTE MACHINES UPLOAD; the server's own machine spools.
    #
    # The spool only works where the server can read it — its own disk. That is why
    # Hiren's Mac never appeared in the roster: its records were not queued, they
    # were stranded. A machine with upload configured POSTs instead, through the
    # same batched write path on the server side.
    if _upload(record):
        return

    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        path = os.path.join(SPOOL_DIR, session_id + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)  # atomic: server never reads a half-written file
    except Exception:
        return


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
