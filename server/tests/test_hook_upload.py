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
