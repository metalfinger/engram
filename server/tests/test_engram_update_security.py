"""Zip Slip guard in hooks/engram_update.py — a background security review flagged
that `_install_zip` joined zip-member names into the destination without checking
the result stayed inside it. A malicious or corrupted bundle with a `../` entry
(or an absolute path) could otherwise write anywhere the running user can write —
e.g. into ~/.ssh — since this script's whole premise is unpacking bytes fetched
over the network. The digest check elsewhere in the file guards against a STALE
or SWAPPED bundle; it says nothing about a well-formed-but-malicious member
inside one that otherwise hashes correctly, which is what this guards against.
"""

from __future__ import annotations

import importlib.util
import io
import os
import zipfile
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "hooks" / "engram_update.py"
    spec = importlib.util.spec_from_file_location("engram_update_security", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _zip_with(names_and_bytes: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in names_and_bytes.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_relative_traversal_member_is_refused(mod, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside.txt"
    raw = _zip_with({
        "../outside.txt": b"malicious payload",
        "engram_ok.py": b"fine",
    })
    written = mod._install_zip(raw, str(dest))
    assert not outside.exists(), "a ../ member must never escape the destination"
    assert (dest / "engram_ok.py").is_file(), "a legitimate sibling member still installs"
    assert "../outside.txt" not in written


def test_a_deep_traversal_member_is_refused(mod, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    raw = _zip_with({"../../../../../../../../tmp/pwned.sh": b"echo pwned"})
    mod._install_zip(raw, str(dest))
    # Nothing written anywhere outside dest — walk up from dest and confirm clean.
    assert list(dest.rglob("*")) == [], "a deep traversal must produce zero writes"


@pytest.mark.skipif(os.name == "nt", reason="absolute-path zip members are a POSIX-only vector")
def test_an_absolute_path_member_is_refused(mod, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    raw = _zip_with({"/etc/pwned.conf": b"malicious"})
    mod._install_zip(raw, str(dest))
    assert not Path("/etc/pwned.conf").exists()


def test_safe_join_accepts_ordinary_members(mod, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    target = mod._safe_join(str(dest), "engram_presence_hook.py")
    assert target is not None
    assert Path(target).parent == dest


def test_safe_join_rejects_escape(mod, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert mod._safe_join(str(dest), "../escape.py") is None
    assert mod._safe_join(str(dest), "../../escape.py") is None


def test_a_prefixed_bundle_with_traversal_is_still_caught(mod, tmp_path):
    """The real hooks bundle strips a `hooks/` prefix before joining — the guard
    must apply to the STRIPPED name, not the raw zip member, or a crafted prefix
    could hide a traversal from the check."""
    dest = tmp_path / "dest"
    dest.mkdir()
    raw = _zip_with({"hooks/../../../outside2.txt": b"bad"})
    mod._install_zip(raw, str(dest), want_prefix="hooks/")
    assert not (tmp_path / "outside2.txt").exists()


def test_legitimate_hooks_bundle_still_installs_cleanly(mod, tmp_path):
    """Regression guard: the fix must not break the real, benign case."""
    dest = tmp_path / "dest"
    dest.mkdir()
    raw = _zip_with({
        "hooks/engram_presence_hook.py": b"# real hook",
        "hooks/other-tool-hook.sh": b"#!/bin/sh\necho hi",
    })
    written = mod._install_zip(raw, str(dest), want_prefix="hooks/", only_engram=True)
    assert written == ["engram_presence_hook.py"], (
        "only_engram must still filter to engram* files after the path fix"
    )
    assert (dest / "engram_presence_hook.py").read_bytes() == b"# real hook"
    assert not (dest / "other-tool-hook.sh").exists()
