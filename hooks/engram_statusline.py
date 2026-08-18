"""Engram status line for Claude Code.

Prints one line of ambient situation: which project this session is on, the
worktree branch, whose turn it is in any room or thread, whether something is
blocked on Hiren, and — the one that prevents real damage — whether another
session is already working in the files under this directory.

WHY A STATUS LINE. Everything here is already knowable through kb_* tools, but
every tool result lands in the model's context window and costs tokens. Terminal
chrome costs nothing, so this is the right home for what you want CONTINUOUSLY
rather than occasionally.

SPEED IS THE WHOLE DESIGN. Claude Code re-renders this constantly, so it must
never block on the network. It reads a cache file and, when that is stale, spawns
a detached refresh and prints the stale value anyway. A status line that pauses
your prompt is worse than one that is thirty seconds behind.

Failure-soft like every other hook here: any error prints a minimal line and
exits 0, because a broken status line must never make Claude Code look broken.
"""

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CONF = os.path.join(HOME, ".engram", "upload.json")
CACHE = os.path.join(HOME, ".engram", "status-cache.json")
LINECONF = os.path.join(HOME, ".engram", "statusline.json")
STALE_AFTER = 30  # seconds; a refresh runs detached, this value is never waited on
PARENT_TIMEOUT = 3  # seconds before we give up on someone else's status line


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _project_for(cwd):
    """The `.engram-project` pin, walking up. Same rule the presence hook uses, so
    the status line and the roster never disagree about which project this is."""
    d = os.path.abspath(cwd or ".")
    for _ in range(40):
        try:
            p = os.path.join(d, ".engram-project")
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            return line.strip()
        except Exception:
            pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


def _branch(cwd):
    try:
        out = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _spawn_refresh(session_id):
    """Refresh the cache WITHOUT waiting. The point is that rendering never blocks."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh", session_id or ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _parent_output(raw):
    """Run whatever status line was here BEFORE Engram, and return its output.

    A status line is personal — Hiren's already carried model, git, context and
    token counts from helix. Replacing that to show Engram state would be a
    downgrade dressed as a feature, so we wrap instead of clobber: set
    ~/.engram/statusline.json to {"parent": "<command>"} and its output is printed
    above ours, unchanged. One statusLine entry then works on every machine, and
    each machine's own config decides what else appears — which is the point,
    since the whole reason this lives in a file is that the setup must be
    identical across three PCs.

    Empty string on anything going wrong, including a parent that hangs: our own
    segment is worth more than a prompt that stalls waiting for someone else's.
    """
    try:
        with open(LINECONF, "r", encoding="utf-8") as f:
            parent = str(json.load(f).get("parent") or "").strip()
        if not parent:
            return ""
        out = subprocess.run(
            parent, shell=True, input=raw, capture_output=True, text=True,
            timeout=PARENT_TIMEOUT,
        )
        return (out.stdout or "").rstrip("\n")
    except Exception:
        return ""


def _credentials():
    """Where the status line gets its url + token, in order of preference.

    `upload.json` is the presence hook's config and is deliberately ABSENT on the
    server's own machine (spooling beats uploading there — a spooled record
    survives the server being down). But that left the status line blind on the
    very machine Hiren works on most: no credentials, no room state, just project
    and branch. So the status line accepts its own credentials in
    `statusline.json`, keeping the two concerns apart — reading state is not the
    same decision as writing presence, and they should not have to share a file.
    """
    line = _read_json(LINECONF)
    url = str(line.get("url") or "").rstrip("/")
    token = str(line.get("token") or "")
    if url and token:
        return url, token
    conf = _read_json(CONF)
    return str(conf.get("url") or "").rstrip("/"), str(conf.get("token") or "")


def _refresh(session_id):
    url, token = _credentials()
    if not url or not token:
        return
    try:
        import urllib.parse
        import urllib.request

        q = urllib.parse.urlencode({"speaker": session_id or ""})
        req = urllib.request.Request(
            url + "/dashboard/api/status?" + q,
            headers={
                "Authorization": "Bearer " + token,
                # Cloudflare 1010s urllib's default agent before it reaches Engram.
                "User-Agent": "engram-statusline/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["at"] = time.time()
        tmp = CACHE + ".tmp"
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE)
    except Exception:
        return


def main():
    # Windows consoles and pipes default to cp1252, where writing '⬢' raises
    # UnicodeEncodeError at flush — AFTER the finally block has already exited 0.
    # The result is a silently BLANK status line and no error anywhere, which is
    # exactly how this was found: it printed nothing and reported success.
    # errors='replace' means a terminal that cannot render the glyph degrades to
    # '?' instead of losing the whole line.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) > 2 and sys.argv[1] == "--refresh":
        _refresh(sys.argv[2])
        return

    payload = {}
    raw = ""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()
    session_id = str(payload.get("session_id") or "")

    cached = _read_json(CACHE)
    if time.time() - float(cached.get("at") or 0) > STALE_AFTER:
        _spawn_refresh(session_id)

    parts = []
    project = _project_for(cwd)
    if project:
        parts.append("⬢ " + project)
    else:
        # An unpinned repo teaches the routing table nothing, so say so once, quietly.
        parts.append("⬢ unpinned")
    branch = _branch(cwd)
    if branch and branch != "HEAD":
        parts.append(branch)

    turn = cached.get("your_turn") or []
    if turn:
        parts.append("⏳ your turn: " + ", ".join(turn[:2]))
    asks = cached.get("needs_you") or []
    if asks:
        parts.append("❓ waiting on you: " + ", ".join(asks[:2]))

    # The one that prevents real damage: someone else already in this worktree.
    here = os.path.basename(os.path.abspath(cwd))
    others = [w for w in (cached.get("working") or [])
              if here and here in str(w.get("path") or "")]
    if others:
        who = ", ".join(sorted({str(w.get("session") or "?") for w in others})[:2])
        parts.append("⚠ also here: " + who)

    line = " · ".join(parts)
    above = _parent_output(raw)
    sys.stdout.write((above + "\n" + line) if above else line)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # ASCII only: this is the path taken when something already went wrong, so
        # it must not be able to fail for the same reason.
        sys.stdout.write("engram")
    finally:
        sys.exit(0)
