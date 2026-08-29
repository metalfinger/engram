"""Engram self-update — keep a machine's hooks and skills current, unattended.

WHY THIS EXISTS. Hooks and skills are FILES ON DISK, so unlike everything served
over MCP they do not follow the server when it changes. That gap has bitten
twice: a skill on the Mac sat six weeks stale, and the presence hook sat a
version behind for a day — the fix deployed, simply never running there, with
nothing saying so. `kb_setup_machine` can repair both, but only when a human
remembers to ask a session to run it, and the thing nobody remembers is exactly
what goes stale.

So: a plain script, no Claude session, no tokens in a chat. Run it from launchd
(macOS), Task Scheduler (Windows), cron, or by hand.

WHAT IT WILL AND WILL NOT TOUCH.
  * writes ONLY files named `engram*` into ~/.claude/hooks, and skill
    directories the server's manifest names, under ~/.claude/skills;
  * never touches settings.json — wiring a hook into the right events is a
    judgement call about a file other tools also own, and that stays with
    `kb_setup_machine` and a human;
  * verifies the downloaded bundle hashes to the digest the server advertised
    before installing anything. A partial download or a stale edge cache is
    then a refusal, not a silent half-install — the failure this whole feature
    exists to end.

It trusts the server it is configured for, which is Hiren's own box over TLS
with a scoped bearer token. That is the same trust the hooks already place in
it; this script does not widen it.
"""

import hashlib
import io
import json
import os
import ssl
import sys
import time
import urllib.request
import zipfile

HOME = os.path.expanduser("~")
CONF = os.path.join(HOME, ".engram", "upload.json")
LINECONF = os.path.join(HOME, ".engram", "statusline.json")
HOOKS_DIR = os.path.join(HOME, ".claude", "hooks")
SKILLS_DIR = os.path.join(HOME, ".claude", "skills")
LOG = os.path.join(HOME, ".engram", "update.log")


def _log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        # Keep the last ~200 lines: a log nobody rotates becomes a log nobody reads.
        old = []
        if os.path.isfile(LOG):
            with open(LOG, "r", encoding="utf-8") as f:
                old = f.read().splitlines()[-199:]
        with open(LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(old + [line]) + "\n")
    except Exception:
        pass


def _ctx():
    """certifi when available — python.org builds on macOS verify nothing without
    it, which is how the presence hook silently dropped every upload for days."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _credentials():
    for path in (LINECONF, CONF):
        try:
            with open(path, "r", encoding="utf-8") as f:
                conf = json.load(f)
            url = str(conf.get("url") or "").rstrip("/")
            token = str(conf.get("token") or "")
            if url and token:
                return url, token
        except Exception:
            continue
    return "", ""


def _get(url, token, accept_json=True):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        # Cloudflare rejects urllib's default agent outright (error 1010).
        "User-Agent": "engram-update/1.0",
    })
    with urllib.request.urlopen(req, timeout=30, context=_ctx()) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if accept_json else raw


def _dir_digest(directory, names):
    """The server's recipe exactly: sorted relative POSIX path bytes then file
    bytes, first 12 hex chars. Only the named files — ~/.claude/hooks is shared
    with other tools, and digesting the whole directory would report drift
    forever."""
    h = hashlib.sha256()
    for name in sorted(names):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        h.update(name.replace(os.sep, "/").encode("utf-8"))
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _owned_hooks():
    try:
        return [n for n in os.listdir(HOOKS_DIR)
                if n.startswith("engram") and os.path.isfile(os.path.join(HOOKS_DIR, n))]
    except OSError:
        return []


def _tree_digest(root):
    """Whole-tree digest, for a skill directory Engram owns outright."""
    if not os.path.isdir(root):
        return ""
    h = hashlib.sha256()
    entries = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            entries.append((os.path.relpath(full, root).replace(os.sep, "/"), full))
    for rel, full in sorted(entries):
        h.update(rel.encode("utf-8"))
        with open(full, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _safe_join(dest, rel):
    """Resolve `rel` under `dest`, refusing anything that would escape it.

    ZIP SLIP GUARD. A zip member name is attacker-controlled data the moment
    the bytes are — and this script's whole premise is downloading bytes from
    the network. An entry like `../../../.ssh/authorized_keys` or an absolute
    path, joined naively, writes outside the hooks directory entirely. The
    digest check earlier in this file guards against a STALE or SWAPPED
    bundle; it says nothing about a well-formed-but-malicious member inside a
    bundle that otherwise hashes correctly, which is a different attack this
    guard exists to close. Returns None (never a path) for anything unsafe, so
    the caller's contract is simple: no return means no write."""
    # normpath collapses ../ segments so a traversal attempt is visible as a
    # path that, once resolved, no longer starts under dest.
    target = os.path.normpath(os.path.join(dest, rel.replace("/", os.sep)))
    dest_norm = os.path.normpath(dest)
    if target != dest_norm and not target.startswith(dest_norm + os.sep):
        return None
    return target


def _install_zip(raw, dest, want_prefix=None, only_engram=False):
    """Unpack, creating parents. `only_engram` guards the SHARED hooks directory:
    other tools' hooks live beside ours and must never be overwritten. Every
    member is path-checked by `_safe_join` before anything is written."""
    written = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            rel = member
            if want_prefix and rel.startswith(want_prefix):
                rel = rel[len(want_prefix):]
            base = os.path.basename(rel)
            if only_engram and not base.startswith("engram"):
                continue
            target = _safe_join(dest, rel)
            if target is None:
                _log("REFUSED zip member outside destination: %r" % member)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(z.read(member))
            if base.endswith(".sh"):
                try:
                    os.chmod(target, 0o755)
                except OSError:
                    pass
            written.append(rel)
    return written


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    url, token = _credentials()
    if not url or not token:
        _log("no credentials in ~/.engram — run kb_setup_machine once first")
        return 0

    try:
        status = _get(url + "/dashboard/api/status", token)
    except Exception as exc:
        _log("cannot reach Engram (%s: %s)" % (type(exc).__name__, exc))
        return 0  # the server being down is not this script's failure

    served = str(status.get("hooks_digest") or "")
    local = _dir_digest(HOOKS_DIR, _owned_hooks())
    changed = []

    if served and served != local:
        try:
            raw = _get(url + "/downloads/engram-hooks.zip?v=" + served, token,
                       accept_json=False)
            # VERIFY BEFORE INSTALLING. A stale edge cache once served an old
            # bundle while reporting success — always-green-but-wrong. If the
            # bytes do not hash to what the server advertised, refuse.
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                h = hashlib.sha256()
                names = sorted(n for n in z.namelist() if not n.endswith("/"))
                for n in names:
                    base = os.path.basename(n)
                    if not base.startswith("engram"):
                        continue
                    h.update(base.encode("utf-8"))
                    h.update(z.read(n))
                got = h.hexdigest()[:12]
            if got != served:
                _log("REFUSED hooks bundle: advertised %s, downloaded %s "
                     "(stale cache or partial download)" % (served, got))
            else:
                written = _install_zip(raw, HOOKS_DIR, want_prefix="hooks/",
                                       only_engram=True)
                changed.append("hooks -> %s (%d files)" % (served, len(written)))
        except Exception as exc:
            _log("hooks update failed (%s: %s)" % (type(exc).__name__, exc))

    # Skills: the manifest lives behind the same auth; a machine without it
    # simply skips this half rather than failing the run.
    try:
        machine = _get(url + "/dashboard/api/machine", token)
        skills = machine.get("skills") or []
    except Exception:
        skills = []
    if skills:
        stale = [s for s in skills
                 if _tree_digest(os.path.join(SKILLS_DIR, s.get("name", ""))) != s.get("digest")]
        if stale:
            try:
                raw = _get(url + "/downloads/engram-skill.zip", token, accept_json=False)
                written = _install_zip(raw, SKILLS_DIR)
                changed.append("skills -> %s (%d files)" %
                               (", ".join(s.get("name", "?") for s in stale), len(written)))
            except Exception as exc:
                _log("skills update failed (%s: %s)" % (type(exc).__name__, exc))

    if changed:
        _log("updated: " + "; ".join(changed))
        _log("NOTE: settings.json untouched — if a NEW hook shipped, run "
             "kb_setup_machine once to wire it into the right events")
    else:
        _log("already current (hooks %s)" % (local or "none"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — an updater must never be the outage
        _log("unexpected: %s: %s" % (type(exc).__name__, exc))
        sys.exit(0)
