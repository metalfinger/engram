"""v3: avatar accepts uploaded data:image URIs (dashboard downscales client-side);
the scheme check stays the XSS backstop."""

import pytest

from engram_server.registry import tenancy_db_path
from engram_server.tenancy import TenancyError, TenancyStore


@pytest.fixture()
def store(settings, tmp_path):
    s = TenancyStore(tmp_path / "engram.db")
    inv = s.create_invite("a@example.com")
    s.accept_invite(inv.token, "ava", "a@example.com", "github", "github:ava")
    return s


def test_data_image_avatar_accepted(store):
    u = store.set_profile("ava", avatar_url="data:image/jpeg;base64,/9j/4AAQtiny")
    assert u.avatar_url.startswith("data:image/jpeg")


def test_https_avatar_still_accepted(store):
    u = store.set_profile("ava", avatar_url="https://example.com/me.png")
    assert u.avatar_url == "https://example.com/me.png"


def test_hostile_schemes_refused(store):
    for bad in ("javascript:alert(1)", "http://x/y.png", "data:text/html,<script>", "file:///etc/passwd"):
        with pytest.raises(TenancyError):
            store.set_profile("ava", avatar_url=bad)


def test_oversize_data_uri_refused(store):
    with pytest.raises(TenancyError, match="too large"):
        store.set_profile("ava", avatar_url="data:image/jpeg;base64," + "A" * 100_001)
