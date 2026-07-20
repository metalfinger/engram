"""M3.1 — CapabilityStore: share requests + capability grants (multi-user share layer)."""

import sqlite3

import pytest

from engram_server.capabilities import CapabilityError, CapabilityStore, covers_path


def _seed_users(db_path, n: int) -> None:
    """Insert minimal users rows directly so capabilities.py's FKs (declared but
    not enforced-by-creation in SQLite) are satisfiable by real ids 1..n."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            idp TEXT NOT NULL,
            idp_subject TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            created TEXT NOT NULL
        );
        """
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO users (handle, email, idp, idp_subject, status, created) "
            "VALUES (?, ?, 'github', ?, 'active', '2026-01-01T00:00:00Z')",
            (f"user{i}", f"user{i}@example.com", f"github:user{i}"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def caps(tmp_path):
    db_path = tmp_path / "engram.db"
    _seed_users(db_path, 6)
    store = CapabilityStore(db_path)
    yield store
    store.close()


def _force_expire(tmp_path, capability_id: int) -> None:
    """Age a capability past its expiry via a second short-lived connection
    (WAL-safe) — there is no time-travel in the public API, so tests reach
    around it this one way rather than poking the store's private connection."""
    conn = sqlite3.connect(str(tmp_path / "engram.db"))
    conn.execute(
        "UPDATE capabilities SET expires = '2020-01-01T00:00:00Z' WHERE id = ?",
        (capability_id,),
    )
    conn.commit()
    conn.close()


# -- covers_path() unit tests (independent of the store) ----------------------


def test_covers_path_exact_match():
    assert covers_path(["projects/alt"], "projects/alt") is True


def test_covers_path_child_file():
    assert covers_path(["projects/alt"], "projects/alt/x.md") is True


def test_covers_path_nested_child():
    assert covers_path(["projects/alt"], "projects/alt/sub/deep.md") is True


def test_covers_path_rejects_sibling_prefix_collision():
    # "projects/alt-secret" is NOT covered by "projects/alt" — no separator boundary.
    assert covers_path(["projects/alt"], "projects/alt-secret") is False


def test_covers_path_rejects_alt2_collision():
    assert covers_path(["projects/alt"], "projects/alt2") is False


def test_covers_path_rejects_parent():
    # A grant on a child does not cover its parent.
    assert covers_path(["projects/alt"], "projects") is False


def test_covers_path_rejects_empty_path():
    assert covers_path(["projects/alt"], "") is False


def test_covers_path_rejects_traversal():
    assert covers_path(["projects/alt"], "projects/alt/../../secret") is False
    assert covers_path(["projects"], "..") is False


def test_covers_path_rejects_absolute():
    assert covers_path(["projects/alt"], "/etc/passwd") is False


def test_covers_path_rejects_backslash():
    assert covers_path(["projects/alt"], "projects\\alt") is False


def test_covers_path_normalizes_slashes_on_both_sides():
    assert covers_path(["/projects/alt/"], "projects/alt/x.md") is True


def test_covers_path_multiple_prefixes():
    prefixes = ["projects/alt", "library/notes"]
    assert covers_path(prefixes, "library/notes/a.md") is True
    assert covers_path(prefixes, "projects/alt/b.md") is True
    assert covers_path(prefixes, "projects/other") is False


# -- share requests -------------------------------------------------------------


def test_create_request_pending(caps):
    r = caps.create_request(2, 1, ["projects/alt"], reason="need context")
    assert r.status == "pending"
    assert r.requester_id == 2
    assert r.owner_id == 1
    assert r.paths == ["projects/alt"]
    assert r.reason == "need context"
    assert r.resolved_at is None


def test_create_request_duplicate_pending_is_idempotent(caps):
    first = caps.create_request(2, 1, ["projects/alt"])
    second = caps.create_request(2, 1, ["projects/other"])
    assert second.id == first.id
    assert second.paths == ["projects/alt"]  # unchanged — first one wins


def test_create_request_rejects_self_request(caps):
    with pytest.raises(CapabilityError):
        caps.create_request(1, 1, ["projects/alt"])


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "../secret",
        "projects/../secret",
        "projects\\alt",
        "",
    ],
)
def test_create_request_rejects_traversal_paths(caps, bad_path):
    with pytest.raises(CapabilityError):
        caps.create_request(2, 1, [bad_path])


def test_create_request_rejects_empty_paths_list(caps):
    with pytest.raises(CapabilityError):
        caps.create_request(2, 1, [])


def test_list_incoming_and_outgoing_requests(caps):
    caps.create_request(2, 1, ["projects/alt"])
    caps.create_request(3, 1, ["projects/beta"])
    incoming = caps.list_incoming_requests(1)
    assert {r.requester_id for r in incoming} == {2, 3}
    outgoing = caps.list_outgoing_requests(2)
    assert len(outgoing) == 1
    assert outgoing[0].owner_id == 1


def test_list_requests_pending_only_filter(caps):
    r = caps.create_request(2, 1, ["projects/alt"])
    caps.resolve_request(r.id, 1, approve=True)
    assert caps.list_incoming_requests(1, pending_only=True) == []
    all_incoming = caps.list_incoming_requests(1, pending_only=False)
    assert len(all_incoming) == 1
    assert all_incoming[0].status == "approved"


def test_resolve_request_approve(caps):
    r = caps.create_request(2, 1, ["projects/alt"])
    resolved = caps.resolve_request(r.id, 1, approve=True)
    assert resolved.status == "approved"
    assert resolved.resolved_at is not None


def test_resolve_request_deny(caps):
    r = caps.create_request(2, 1, ["projects/alt"])
    resolved = caps.resolve_request(r.id, 1, approve=False)
    assert resolved.status == "denied"


def test_resolve_request_only_owner_may_resolve(caps):
    r = caps.create_request(2, 1, ["projects/alt"])
    with pytest.raises(CapabilityError):
        caps.resolve_request(r.id, 3, approve=True)


def test_resolve_request_rejects_non_pending(caps):
    r = caps.create_request(2, 1, ["projects/alt"])
    caps.resolve_request(r.id, 1, approve=True)
    with pytest.raises(CapabilityError):
        caps.resolve_request(r.id, 1, approve=True)


def test_resolve_request_unknown_id(caps):
    with pytest.raises(CapabilityError):
        caps.resolve_request(999, 1, approve=True)


# -- grant / revoke --------------------------------------------------------------


def test_grant_mints_capability(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read", "search"], days=30, note="for M3")
    assert cap.owner_id == 1
    assert cap.grantee_id == 2
    assert cap.paths == ["projects/alt"]
    assert set(cap.verbs) == {"read", "search"}
    assert cap.note == "for M3"
    assert cap.token
    assert cap.revoked is False
    assert cap.live is True


def test_grant_sets_expiry_in_future(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"], days=1)
    assert cap.expires > cap.created


def test_grant_rejects_self_grant(caps):
    with pytest.raises(CapabilityError):
        caps.grant(1, 1, ["projects/alt"], ["read"])


def test_grant_rejects_empty_verbs(caps):
    with pytest.raises(CapabilityError):
        caps.grant(1, 2, ["projects/alt"], [])


def test_grant_rejects_unknown_verb(caps):
    with pytest.raises(CapabilityError):
        caps.grant(1, 2, ["projects/alt"], ["read", "write"])


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "../secret", "projects/../secret", "projects\\alt"],
)
def test_grant_rejects_traversal_paths(caps, bad_path):
    with pytest.raises(CapabilityError):
        caps.grant(1, 2, [bad_path], ["read"])


def test_grant_rejects_empty_paths_list(caps):
    with pytest.raises(CapabilityError):
        caps.grant(1, 2, [], ["read"])


def test_revoke_by_owner(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"])
    caps.revoke(cap.id, 1)
    live = caps.list_granted_by(1, live_only=True)
    assert live == []


def test_revoke_by_non_owner_rejected(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"])
    with pytest.raises(CapabilityError):
        caps.revoke(cap.id, 2)


def test_revoke_unknown_capability(caps):
    with pytest.raises(CapabilityError):
        caps.revoke(999, 1)


def test_list_granted_by_and_to(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    caps.grant(1, 3, ["projects/beta"], ["read"])
    by_owner = caps.list_granted_by(1)
    assert {c.grantee_id for c in by_owner} == {2, 3}
    to_grantee = caps.list_granted_to(2)
    assert len(to_grantee) == 1
    assert to_grantee[0].owner_id == 1


def test_list_granted_by_live_only_excludes_revoked(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"])
    caps.revoke(cap.id, 1)
    assert caps.list_granted_by(1, live_only=True) == []
    assert len(caps.list_granted_by(1, live_only=False)) == 1


def test_list_granted_by_live_only_excludes_expired(caps, tmp_path):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"], days=30)
    _force_expire(tmp_path, cap.id)
    assert caps.list_granted_by(1, live_only=True) == []
    assert len(caps.list_granted_by(1, live_only=False)) == 1


# -- check() — the enforcement primitive, boundary-safe coverage --------------


def test_check_true_for_exact_and_child_path(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "projects/alt", "read") is True
    assert caps.check(1, 2, "projects/alt/context.md", "read") is True


def test_check_false_for_sibling_prefix_collision(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "projects/alt-secret", "read") is False


def test_check_false_for_alt2_collision(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "projects/alt2", "read") is False


def test_check_false_for_parent_path(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "projects", "read") is False


def test_check_false_for_empty_path(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "", "read") is False


def test_check_false_for_wrong_verb(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 2, "projects/alt/x.md", "browse") is False


def test_check_false_for_different_grantee(caps):
    caps.grant(1, 2, ["projects/alt"], ["read"])
    assert caps.check(1, 3, "projects/alt/x.md", "read") is False


def test_check_false_when_revoked(caps):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"])
    caps.revoke(cap.id, 1)
    assert caps.check(1, 2, "projects/alt/x.md", "read") is False


def test_check_false_when_expired(caps, tmp_path):
    cap = caps.grant(1, 2, ["projects/alt"], ["read"])
    _force_expire(tmp_path, cap.id)
    assert caps.check(1, 2, "projects/alt/x.md", "read") is False


def test_check_false_for_traversal_path_even_with_broad_grant(caps):
    caps.grant(1, 2, ["projects"], ["read", "search", "browse"])
    assert caps.check(1, 2, "../secret", "read") is False
    assert caps.check(1, 2, "/etc/passwd", "read") is False


def test_check_true_with_multiple_prefixes_in_one_grant(caps):
    caps.grant(1, 2, ["projects/alt", "library/notes"], ["read"])
    assert caps.check(1, 2, "projects/alt/a.md", "read") is True
    assert caps.check(1, 2, "library/notes/b.md", "read") is True
    assert caps.check(1, 2, "library/other", "read") is False


def test_check_true_only_after_verb_present(caps):
    caps.grant(1, 2, ["projects/alt"], ["search"])
    assert caps.check(1, 2, "projects/alt/x.md", "search") is True
    assert caps.check(1, 2, "projects/alt/x.md", "read") is False
