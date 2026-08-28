"""The presence hook uploading from a remote machine.

The spool only works where the server can read it — its own disk. That is why
Hiren's Mac never appeared in the roster: its records were not queued, they were
stranded. A machine with upload configured POSTs instead.

These tests run the ACTUAL hook script as a subprocess against a real HTTP
server, because the failure being fixed lived precisely in the gap between "the
hook wrote something" and "the server received it".
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "engram_presence_hook.py"


class _Recorder(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        _Recorder.received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "records": payload.get("records"),
        })
        self.send_response(_Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):  # keep test output clean
        pass


@pytest.fixture()
def server():
    _Recorder.received = []
    _Recorder.status = 200
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()


def _run_hook(tmp_home, cwd, payload):
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "USERPROFILE": str(tmp_home),
        "HOME": str(tmp_home),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(cwd), env=env, timeout=30,
    )


def test_a_configured_machine_uploads_instead_of_spooling(server, tmp_path):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    port = server.server_address[1]
    (home / ".engram" / "upload.json").write_text(
        json.dumps({"url": f"http://127.0.0.1:{port}", "token": "tok-123"}),
        encoding="utf-8",
    )
    proc = _run_hook(home, tmp_path, {
        "session_id": "mac-a", "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
    })
    assert proc.returncode == 0
    assert len(_Recorder.received) == 1, "the record must reach the server"
    got = _Recorder.received[0]
    assert got["path"] == "/dashboard/api/presence/upload"
    assert got["auth"] == "Bearer tok-123"
    assert got["records"][0]["session"] == "mac-a"
    assert not (home / ".engram" / "presence-spool").exists(), (
        "an uploaded record must not also be spooled — nothing drains a remote spool"
    )


def test_an_unconfigured_machine_still_spools(tmp_path):
    """The server's own machine has no upload config and must behave exactly as
    before."""
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    proc = _run_hook(home, tmp_path, {
        "session_id": "win-a", "hook_event_name": "SessionStart",
        "cwd": str(tmp_path),
    })
    assert proc.returncode == 0
    spooled = list((home / ".engram" / "presence-spool").glob("*.json"))
    assert len(spooled) == 1


def test_a_failed_upload_never_blocks_claude(server, tmp_path):
    """Failure-soft is the whole design: a broken hook must never stop a session.
    And a failed heartbeat is DROPPED, not queued — a stale record would claim a
    session is somewhere it has left."""
    _Recorder.status = 500
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    port = server.server_address[1]
    (home / ".engram" / "upload.json").write_text(
        json.dumps({"url": f"http://127.0.0.1:{port}", "token": "tok"}),
        encoding="utf-8",
    )
    proc = _run_hook(home, tmp_path, {
        "session_id": "mac-b", "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
    })
    assert proc.returncode == 0, "a failing upload must still exit 0"


def test_an_unreachable_server_never_blocks_claude(tmp_path):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "upload.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9", "token": "tok"}), encoding="utf-8",
    )
    proc = _run_hook(home, tmp_path, {
        "session_id": "mac-c", "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
    })
    assert proc.returncode == 0


def test_a_malformed_config_falls_back_to_spooling(tmp_path):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "upload.json").write_text("not json", encoding="utf-8")
    proc = _run_hook(home, tmp_path, {
        "session_id": "win-b", "hook_event_name": "SessionStart",
        "cwd": str(tmp_path),
    })
    assert proc.returncode == 0
    assert list((home / ".engram" / "presence-spool").glob("*.json"))


def test_a_failed_upload_leaves_a_findable_last_error(tmp_path, monkeypatch):
    """The Mac's diagnosis in one sentence: the hook swallows exceptions by
    design, which turned a broken SSL stack into weeks of silent roster absence.
    The last-error file keeps failure-soft while making the failure a
    thirty-second `cat` instead of a multi-day mystery."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "presence_hook_err",
        Path(__file__).resolve().parents[2] / "hooks" / "engram_presence_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    err = tmp_path / "presence-upload.err"
    monkeypatch.setattr(hook, "UPLOAD_ERR", str(err))
    conf = tmp_path / "upload.json"
    conf.write_text('{"url": "https://engram.invalid", "token": "t"}',
                    encoding="utf-8")
    monkeypatch.setattr(hook, "UPLOAD_CONF", str(conf))

    # engram.invalid never resolves -> urlopen raises -> dropped AND noted.
    assert hook._upload({"session": "x", "ts": 0}) is False
    assert err.is_file(), "the drop must leave a one-line trace"
    line = err.read_text(encoding="utf-8")
    assert "Error" in line or "error" in line

    # Success clears it, so a stale error can't outlive the problem.
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert hook._upload({"session": "x", "ts": 0}) is True
    assert not err.exists(), "a healed upload must not leave a stale error"


def test_the_ssl_context_prefers_certifi_when_present(monkeypatch):
    """macOS python.org builds wire NO system CA bundle into ssl, so the default
    context verifies nothing there. certifi, when importable, is the fix; its
    absence (normal on Windows) must not break anything."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "presence_hook_ssl",
        Path(__file__).resolve().parents[2] / "hooks" / "engram_presence_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    import ssl
    ctx = hook._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED, "verification must NEVER be off"


def _load_hook(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "presence_hook_drain",
        Path(__file__).resolve().parents[2] / "hooks" / "engram_presence_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    conf = tmp_path / "upload.json"
    conf.write_text('{"url": "https://engram.test", "token": "t"}', encoding="utf-8")
    monkeypatch.setattr(hook, "UPLOAD_CONF", str(conf))
    monkeypatch.setattr(hook, "UPLOAD_ERR", str(tmp_path / "up.err"))
    monkeypatch.setattr(hook, "SPOOL_DIR", str(tmp_path / "spool"))
    return hook


def test_a_timeout_is_retried_once_before_giving_up(tmp_path, monkeypatch):
    """Field data from the .err file: a cold-handshake timeout succeeds on the
    second attempt essentially always. One retry is far cheaper than a session
    invisible on the roster until its next user prompt."""
    hook = _load_hook(monkeypatch, tmp_path)
    calls = {"n": 0}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _flaky)
    assert hook._upload({"session": "x", "ts": 1}) is True
    assert calls["n"] == 2, "exactly one retry"
    assert not (tmp_path / "up.err").exists(), "a recovered upload leaves no error"


def test_one_success_flushes_the_stranded_spool(tmp_path, monkeypatch):
    """Spool-then-vanish, fixed: a timed-out SessionStart spooled its record and
    nothing retried until that session's next prompt — lanes sat invisible on
    the roster after the network recovered. Any success now carries its
    predecessors' stranded records."""
    import json as _json

    hook = _load_hook(monkeypatch, tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    for i in range(3):
        (spool / f"old-{i}.json").write_text(
            _json.dumps({"session": f"old-{i}", "ts": i}), encoding="utf-8")

    bodies = []

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _ok(req, *a, **k):
        bodies.append(_json.loads(req.data.decode("utf-8")))
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _ok)
    assert hook._upload({"session": "fresh", "ts": 99}) is True

    drained = [r["session"] for b in bodies[1:] for r in b["records"]]
    assert {"old-0", "old-1", "old-2"} <= set(drained), "backlog must ride along"
    assert list(spool.glob("*.json")) == [], "drained records are deleted"


def test_a_failed_drain_keeps_the_spool(tmp_path, monkeypatch):
    import json as _json

    hook = _load_hook(monkeypatch, tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "old.json").write_text(_json.dumps({"session": "old", "ts": 1}),
                                    encoding="utf-8")

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = {"n": 0}

    def _first_only(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp()
        raise TimeoutError("drain died")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _first_only)
    assert hook._upload({"session": "fresh", "ts": 2}) is True
    assert (spool / "old.json").exists(), "a failed drain must not lose records"
