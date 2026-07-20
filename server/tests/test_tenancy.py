"""M0.1 — TenancyStore: accounts + invites (the multi-user foundation)."""

import pytest

from engram_server.tenancy import (
    RESERVED_HANDLES,
    TenancyError,
    TenancyStore,
    validate_handle,
)


@pytest.fixture()
def tenancy(tmp_path):
    store = TenancyStore(tmp_path / "engram.db")
    yield store
    store.close()


# -- handles ----------------------------------------------------------------


def test_handle_normalizes_case_and_whitespace():
    assert validate_handle("  Amiyanshu ") == "amiyanshu"


@pytest.mark.parametrize(
    "bad",
    ["a", "-lead", "trail-", "has space", "has_underscore", "Ûnicode", "x" * 33, ""],
)
def test_handle_rejects_bad_shapes(bad):
    with pytest.raises(TenancyError):
        validate_handle(bad)


def test_handle_rejects_reserved():
    for reserved in ("admin", "engram", "share", "hiren"):
        assert reserved in RESERVED_HANDLES
        with pytest.raises(TenancyError, match="reserved"):
            validate_handle(reserved)


@pytest.mark.parametrize(
    "device", ["con", "CON", "Con", "nul", "prn", "aux", "com1", "com9", "lpt1", "lpt9"]
)
def test_handle_rejects_windows_device_names(device):
    # These pass the regex but soft-brick the account on Windows (git can't mkdir them).
    with pytest.raises(TenancyError, match="reserved"):
        validate_handle(device)


@pytest.mark.parametrize("device", ["con.md", "nul.txt"])
def test_handle_rejects_dotted_device_names(device):
    # Dots aren't allowed in a handle at all, so these are rejected by the regex
    # first (a device name can never actually carry an extension); the extension
    # strip in validate_handle is belt-and-suspenders for any future caller.
    with pytest.raises(TenancyError):
        validate_handle(device)


def test_handle_allows_device_name_as_substring():
    # Only the exact stem is reserved — "console", "computer", "connor" are fine.
    for ok in ("console", "computer", "connor", "com10", "lpt10"):
        assert validate_handle(ok) == ok


# -- bootstrap owner --------------------------------------------------------


def test_bootstrap_owner_is_idempotent_and_may_use_reserved_handle(tenancy):
    first = tenancy.bootstrap_owner(
        "hiren", "hir.012612@gmail.com", "github", "github:metalfinger"
    )
    again = tenancy.bootstrap_owner(
        "hiren", "hir.012612@gmail.com", "github", "github:metalfinger"
    )
    assert first.id == again.id
    assert first.handle == "hiren"
    assert tenancy.user_by_subject("github:metalfinger").id == first.id


# -- invite lifecycle -------------------------------------------------------


def test_invite_accept_creates_user_and_consumes_invite(tenancy):
    invite = tenancy.create_invite("colleague@example.com", preset="member")
    assert invite.live

    user = tenancy.accept_invite(
        invite.token, "Colleague", "colleague@example.com",
        "google", "google:colleague@example.com",
    )
    assert user.handle == "colleague"
    assert user.status == "active"

    spent = tenancy.get_invite(invite.token)
    assert spent.accepted_by == user.id
    assert not spent.live
    with pytest.raises(TenancyError, match="already used"):
        tenancy.accept_invite(
            invite.token, "other", "other@example.com", "google", "google:other@example.com"
        )


def test_github_bound_invite_only_that_subject_can_accept(tenancy):
    inv = tenancy.create_invite(
        "octocat@users.noreply.github.com", bind_subject="github:octocat"
    )
    assert inv.bind_subject == "github:octocat"
    # a different identity cannot accept it
    with pytest.raises(TenancyError, match="reserved for github:octocat"):
        tenancy.accept_invite(inv.token, "cat", "cat@x.com", "google", "google:someone@x.com")
    # the bound GitHub identity can
    user = tenancy.accept_invite(
        inv.token, "octocat", "octocat@users.noreply.github.com", "github", "github:octocat"
    )
    assert user.handle == "octocat"


def test_github_bound_invite_refused_if_subject_has_account(tenancy):
    tenancy.bootstrap_owner("hiren", "h@x.com", "github", "github:metalfinger")
    with pytest.raises(TenancyError, match="already has an account"):
        tenancy.create_invite("x@users.noreply.github.com", bind_subject="github:metalfinger")


def test_invite_expired_and_revoked_paths(tenancy):
    expired = tenancy.create_invite("late@example.com", ttl_days=-1)
    with pytest.raises(TenancyError, match="expired"):
        tenancy.accept_invite(
            expired.token, "late", "late@example.com", "google", "google:late@example.com"
        )

    revoked = tenancy.create_invite("gone@example.com")
    tenancy.revoke_invite(revoked.token)
    assert not tenancy.get_invite(revoked.token).live
    with pytest.raises(TenancyError, match="revoked"):
        tenancy.accept_invite(
            revoked.token, "gone", "gone@example.com", "google", "google:gone@example.com"
        )
    with pytest.raises(TenancyError):
        tenancy.revoke_invite("no-such-token")


def test_invite_rejects_existing_account_and_bad_email(tenancy):
    tenancy.bootstrap_owner("hiren", "hir.012612@gmail.com", "github", "github:metalfinger")
    with pytest.raises(TenancyError, match="already has an account"):
        tenancy.create_invite("hir.012612@gmail.com")
    with pytest.raises(TenancyError, match="email"):
        tenancy.create_invite("nope")


# -- uniqueness -------------------------------------------------------------


def test_unique_handle_email_subject(tenancy):
    a = tenancy.create_invite("a@example.com")
    tenancy.accept_invite(a.token, "alpha", "a@example.com", "google", "google:a@example.com")

    b = tenancy.create_invite("b@example.com")
    with pytest.raises(TenancyError, match="already taken"):
        tenancy.accept_invite(b.token, "alpha", "b@example.com", "google", "google:b@example.com")
    with pytest.raises(TenancyError, match="already has an account"):
        tenancy.accept_invite(b.token, "beta", "a@example.com", "google", "google:b2@example.com")
    with pytest.raises(TenancyError, match="already has an account"):
        tenancy.accept_invite(b.token, "beta", "b@example.com", "google", "google:a@example.com")

    # the invite survived every failed redemption and still works
    user = tenancy.accept_invite(b.token, "beta", "b@example.com", "google", "google:b@example.com")
    assert user.handle == "beta"


# -- listings + status ------------------------------------------------------


def test_set_profile(tenancy):
    tenancy.bootstrap_owner("hiren", "hir@example.com", "github", "github:metalfinger")
    u = tenancy.set_profile("hiren", display_name="Hiren K", avatar_url="https://cdn.example/me.png")
    assert u.display_name == "Hiren K"
    assert u.avatar_url == "https://cdn.example/me.png"
    # partial update leaves the other field
    u = tenancy.set_profile("hiren", display_name="H")
    assert u.display_name == "H" and u.avatar_url == "https://cdn.example/me.png"
    # non-https avatar refused
    with pytest.raises(TenancyError, match="https"):
        tenancy.set_profile("hiren", avatar_url="http://insecure/x.png")
    with pytest.raises(TenancyError, match="javascript|https"):
        tenancy.set_profile("hiren", avatar_url="javascript:alert(1)")
    # clearing works (empty string -> None)
    u = tenancy.set_profile("hiren", avatar_url="")
    assert u.avatar_url is None


def test_listings_and_status(tenancy):
    tenancy.bootstrap_owner("hiren", "hir.012612@gmail.com", "github", "github:metalfinger")
    live = tenancy.create_invite("x@example.com")
    tenancy.create_invite("y@example.com", ttl_days=-1)  # expired

    assert {i.token for i in tenancy.list_invites(live_only=True)} == {live.token}
    assert len(tenancy.list_invites()) == 2
    assert [u.handle for u in tenancy.list_users()] == ["hiren"]

    tenancy.set_status("hiren", "suspended")
    assert tenancy.user_by_handle("hiren").status == "suspended"
    with pytest.raises(TenancyError):
        tenancy.set_status("hiren", "banished")
    with pytest.raises(TenancyError):
        tenancy.set_status("ghost", "suspended")
