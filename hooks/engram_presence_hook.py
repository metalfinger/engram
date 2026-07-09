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
    if event == "SessionStart":
        status = "working"
    elif event == "UserPromptSubmit":
        status = "working"
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
        "status": status,
        "ts": int(time.time()),
    }

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
