"""M0.6 — ADVERSARIAL cross-tenant isolation harness.

Mindset: every test here is an ATTACK, not a demonstration. Two (or more) tenants
share one process/registry; each test tries to make tenant B see, write, corrupt,
or otherwise touch tenant A's data (or reach outside the knowledge base entirely)
and asserts the attempt fails. A green suite is the gate before any real second
user is invited.

Pattern mirrors test_authz.py: patch app_module.registry with a multiuser
StoreRegistry over tmp roots, and patch app_module.get_access_token to hand back
a SimpleNamespace(subject=...) standing in for the validated OAuth token. Zero
network throughout — the `settings` fixture (conftest.py) points at a file://
bare remote.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.errors import KBError
from engram_server.provisioning import ProvisioningError, ensure_user_brain
from engram_server.registry import StoreRegistry
from engram_server.tenancy import TenancyError


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    """A multiuser registry over tmp roots, patched into the app module, plus
    the raw settings/paths a test needs to assert against the filesystem
    directly (e.g. 'no stray directory was created outside users_root')."""
    users_root = tmp_path / "users"
    mu_settings = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(users_root),
            "tenancy_db_path": str(tmp_path / "engram.db"),
        }
    )
    registry = StoreRegistry(mu_settings)
    monkeypatch.setattr(app_module, "registry", registry)
    return SimpleNamespace(registry=registry, users_root=users_root, settings=mu_settings)


def _login(monkeypatch, subject):
    token = None if subject is None else SimpleNamespace(subject=subject)
    monkeypatch.setattr(app_module, "get_access_token", lambda: token)


def _invited_user(registry, handle, email, idp="google"):
    invite = registry.tenancy.create_invite(email)
    return registry.tenancy.accept_invite(invite.token, handle, email, idp, f"{idp}:{email}")


ALICE_CONTENT = (
    "---\ntype: project\ndescription: alice-private\n---\n\n"
    "# About\n\nAlice's secret sauce — nobody else should ever read this.\n"
)


async def _seed_alice_secret(monkeypatch):
    """Log in as alice and write her private concept. Returns her KBStore."""
    _login(monkeypatch, "google:alice@example.com")
    await app_module.kb_write(
        path="projects/secret/context.md",
        content=ALICE_CONTENT,
        message="feat: alice secret",
    )
    return await app_module.current_store()


# ==================================================================
# 1. Cross-tenant READ
# ==================================================================


@pytest.mark.asyncio
async def test_kb_read_cannot_read_another_tenants_exact_path(mu, monkeypatch):
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")
    await _seed_alice_secret(monkeypatch)

    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError, match="No such file"):
        await app_module.kb_read("projects/secret/context.md")


@pytest.mark.asyncio
async def test_kb_projects_never_lists_another_tenants_project(mu, monkeypatch):
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")
    await _seed_alice_secret(monkeypatch)

    _login(monkeypatch, "google:bob@example.com")
    ids = [p["id"] for p in await app_module.kb_projects()]
    assert "secret" not in ids


@pytest.mark.asyncio
async def test_kb_load_cannot_load_another_tenants_project(mu, monkeypatch):
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")
    await _seed_alice_secret(monkeypatch)

    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_load("secret")


# ==================================================================
# 2. Cross-tenant WRITE / overwrite (+ path tricks)
# ==================================================================


@pytest.mark.asyncio
async def test_kb_write_same_path_never_touches_another_tenants_file(mu, monkeypatch):
    """Both tenants write the SAME repo-relative path. Each must land only in
    their own checkout — bob's write must not corrupt or overwrite alice's."""
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")
    alice_store = await _seed_alice_secret(monkeypatch)

    _login(monkeypatch, "google:bob@example.com")
    await app_module.kb_write(
        path="projects/secret/context.md",
        content="---\ntype: project\ndescription: bob-private\n---\n\n# About\n\nBob's own secret.\n",
        message="feat: bob secret",
    )
    bob_store = await app_module.current_store()

    assert alice_store.root != bob_store.root
    alice_text = (alice_store.root / "projects" / "secret" / "context.md").read_text(encoding="utf-8")
    assert "Alice's secret sauce" in alice_text
    assert "Bob's own secret" not in alice_text
    bob_text = (bob_store.root / "projects" / "secret" / "context.md").read_text(encoding="utf-8")
    assert "Bob's own secret" in bob_text


TRAVERSAL_PAYLOADS = [
    "../alice/brain/projects/secret/context.md",
    "..\\alice\\brain\\projects\\secret\\context.md",
    "projects/../../../alice/brain/projects/secret/context.md",
    "projects/../../..",
    "../../../../../../etc/passwd.md",
]


@pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
@pytest.mark.asyncio
async def test_kb_write_rejects_dotdot_traversal(mu, monkeypatch, payload):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_write(path=payload, content="x", message="attempt")


@pytest.mark.parametrize(
    "payload",
    ["/etc/passwd.md", "C:\\Windows\\System32\\evil.md", "C:/evil/evil.md"],
)
@pytest.mark.asyncio
async def test_kb_write_rejects_absolute_paths(mu, monkeypatch, payload):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError, match="Absolute paths"):
        await app_module.kb_write(path=payload, content="x", message="attempt")


@pytest.mark.asyncio
async def test_kb_write_url_encoded_traversal_stays_inside_own_root(mu, monkeypatch):
    """No literal '/' in the payload means the '..'-segment guard never even
    fires — but that's fine: with no real separator the whole string is just
    one (oddly named) filename INSIDE the caller's own root. Prove it never
    reaches alice's checkout."""
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")
    alice_store = await _seed_alice_secret(monkeypatch)

    _login(monkeypatch, "google:bob@example.com")
    payload = "..%2f..%2fusers%2falice%2fbrain%2fprojects%2fsecret%2fcontext.md"
    result = await app_module.kb_write(
        path=payload,
        content="---\ntype: project\ndescription: bob-attempt\n---\n\n# About\n\nBob's traversal attempt.\n",
        message="feat: bob traversal attempt",
    )
    bob_store = await app_module.current_store()
    written_abs = (bob_store.root / result["path"]).resolve()
    assert written_abs.is_relative_to(bob_store.root.resolve())
    assert not written_abs.is_relative_to(alice_store.root.resolve())

    alice_text = (alice_store.root / "projects" / "secret" / "context.md").read_text(encoding="utf-8")
    assert "Bob's traversal attempt" not in alice_text


# ==================================================================
# 3. Path traversal in kb_read / kb_edit / kb_move / kb_mark_read
#    (asserted on a NON-owner tenant store)
# ==================================================================


@pytest.mark.asyncio
async def test_kb_read_rejects_traversal_on_tenant_store(mu, monkeypatch):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_read("../../etc/passwd")
    with pytest.raises(KBError):
        await app_module.kb_read("/etc/passwd")


@pytest.mark.asyncio
async def test_kb_edit_rejects_traversal_on_tenant_store(mu, monkeypatch):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_edit("../../etc/passwd.md", "append", content="x")


@pytest.mark.asyncio
async def test_kb_move_rejects_traversal_on_either_side_on_tenant_store(mu, monkeypatch):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_move("../../etc/passwd.md", "projects/whatever/y.md")
    with pytest.raises(KBError):
        await app_module.kb_move("../../etc/passwd.md", "../../evil.md")


@pytest.mark.asyncio
async def test_kb_mark_read_rejects_traversal_on_tenant_store(mu, monkeypatch):
    _invited_user(mu.registry, "bob", "bob@example.com")
    _login(monkeypatch, "google:bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_mark_read("../../etc/passwd.md")


# ==================================================================
# 4. Identity spoofing at the seam
# ==================================================================


@pytest.mark.asyncio
async def test_unknown_subject_is_refused(mu, monkeypatch):
    _login(monkeypatch, "google:stranger@example.com")
    with pytest.raises(KBError, match="invite"):
        await app_module.current_store()


@pytest.mark.parametrize(
    "spoofed_subject",
    [
        "github:metalfingerX",
        "github:metalfinger-evil",
        "github:metalfinger ",  # trailing space — must not fuzzy-match
        " github:metalfinger",  # leading space
        "github:Metalfinger",  # case variant
        "github:metalfing",  # prefix-of substring
    ],
)
@pytest.mark.asyncio
async def test_subject_prefix_confusion_never_resolves_to_owner(mu, monkeypatch, spoofed_subject):
    """An owner subject is 'github:metalfinger' exactly — the check is a set
    membership test, not startswith/contains, so near-miss strings must be
    treated as an unknown (unregistered) identity, never silently granted the
    owner's brain."""
    _login(monkeypatch, spoofed_subject)
    with pytest.raises(KBError, match="invite"):
        await app_module.current_store()


@pytest.mark.asyncio
async def test_none_subject_resolves_to_owner(mu, monkeypatch):
    # Safe ONLY because main() refuses to start multiuser without OAuth configured
    # (settings.multiuser and not _AUTH_ENABLED -> SystemExit) — this path exists
    # for localhost/no-auth dev, not as a multiuser production surface.
    _login(monkeypatch, None)
    assert await app_module.current_store() is mu.registry.owner


@pytest.mark.asyncio
async def test_empty_subject_resolves_to_owner(mu, monkeypatch):
    _login(monkeypatch, "")
    assert await app_module.current_store() is mu.registry.owner


# ==================================================================
# 5. Suspended account
# ==================================================================


@pytest.mark.asyncio
async def test_suspended_tenant_is_refused_even_though_provisioned(mu, monkeypatch):
    _invited_user(mu.registry, "gamma", "gamma@example.com")
    _login(monkeypatch, "google:gamma@example.com")
    # Provision the brain first (as if gamma had used the service already).
    await app_module.kb_projects()

    mu.registry.tenancy.set_status("gamma", "suspended")
    with pytest.raises(KBError, match="suspended"):
        await app_module.current_store()


# ==================================================================
# 6. Reserved / malicious handles never reach the filesystem
# ==================================================================


MALICIOUS_HANDLES = [
    "../evil",
    # Windows reserved device names — soft-bricked accounts before the M0.6 harness
    # caught it; validate_handle now rejects the whole family (case-insensitive,
    # with or without extension). See tenancy._WIN_RESERVED.
    "con",
    "CON",
    "nul",
    "com1",
    "lpt9",
    "a/b",
    "",
    "..",
    "hiren",
    "metalfinger",
    "a" * 40,
]


@pytest.mark.parametrize("bad_handle", MALICIOUS_HANDLES)
def test_accept_invite_rejects_malicious_or_reserved_handles(mu, bad_handle):
    invite = mu.registry.tenancy.create_invite(f"victim-{abs(hash(bad_handle))}@example.com")
    with pytest.raises(TenancyError):
        mu.registry.tenancy.accept_invite(
            invite.token, bad_handle, invite.email, "google", f"google:{invite.email}"
        )
    # No directory escaped users_root (which itself shouldn't even exist yet —
    # provisioning is a separate, later step from accept_invite).
    assert not mu.users_root.exists()
    # The invite itself was never consumed by the failed attempt.
    still_live = mu.registry.tenancy.get_invite(invite.token)
    assert still_live is not None and still_live.accepted_by is None


def test_provisioning_rejects_malicious_handle_defense_in_depth(mu):
    """Even if some future caller reached ensure_user_brain directly with an
    unvalidated handle (bypassing tenancy), the path-safety re-check must still
    refuse it — belt AND suspenders."""
    with pytest.raises(ProvisioningError):
        ensure_user_brain(mu.settings, "../evil")
    # '../evil' relative to users_root would land as a SIBLING directory —
    # prove that sibling was never created.
    assert not (mu.users_root.parent / "evil").exists()


# ==================================================================
# 7. Search isolation end-to-end (offline text-scorer path — no live Qdrant)
# ==================================================================


@pytest.mark.asyncio
async def test_kb_search_never_returns_another_tenants_concept(mu, monkeypatch):
    """Regression guard: with no Qdrant configured (the default test settings),
    kb_search runs the pure-text scorer over `self.root` — each tenant's OWN
    checkout — so this holds by construction. Assert it explicitly so a future
    change (e.g. a shared index) can't silently reintroduce a leak."""
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")

    _login(monkeypatch, "google:alice@example.com")
    await app_module.kb_write(
        path="projects/secret/context.md",
        content=(
            "---\ntype: project\ndescription: alice-private\n---\n\n"
            "# About\n\nxanthoglyphic-covenant-artifact is alice's unique term.\n"
        ),
        message="feat: alice distinctive term",
    )

    _login(monkeypatch, "google:bob@example.com")
    results = await app_module.kb_search("xanthoglyphic-covenant-artifact", expand=False)
    assert results == []


# ==================================================================
# 8. Threads / claims cross-tenant
# ==================================================================


@pytest.mark.asyncio
async def test_thread_posted_by_one_tenant_is_invisible_to_another(mu, monkeypatch):
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")

    _login(monkeypatch, "google:alice@example.com")
    await app_module.kb_thread_post(
        thread="alice-private-room", sender="alice", message="top secret plan", topic="private"
    )

    _login(monkeypatch, "google:bob@example.com")
    threads = await app_module.kb_threads()
    assert all(t["thread"] != "alice-private-room" for t in threads)

    read = await app_module.kb_thread_read("alice-private-room")
    assert read["status"] == "none"
    assert read["turns"] == []


@pytest.mark.asyncio
async def test_claim_held_by_one_tenant_is_invisible_to_another(mu, monkeypatch):
    _invited_user(mu.registry, "alice", "alice@example.com")
    _invited_user(mu.registry, "bob", "bob@example.com")

    _login(monkeypatch, "google:alice@example.com")
    await app_module.kb_claim("alice-session", "projects/secret/context.md", note="working on it")

    _login(monkeypatch, "google:bob@example.com")
    claims = await app_module.kb_claims()
    assert all(c["path"] != "projects/secret/context.md" for c in claims)

    # Bob claiming the SAME path in his own brain must not surface alice's claim
    # (they are disjoint stores — there is no foreign claim to warn about).
    result = await app_module.kb_claim("bob-session", "projects/secret/context.md")
    assert "already_claimed_by" not in result


# ==================================================================
# 9. Concurrent multi-tenant writes don't cross streams
# ==================================================================


@pytest.mark.asyncio
async def test_concurrent_writes_from_two_tenants_stay_in_their_own_checkouts(mu):
    """Bypass the shared mutable app-module token (which can't safely represent
    two identities 'at once') and drive both stores directly through the
    registry, exactly as two concurrent requests would resolve to two distinct
    KBStore instances with their own locks."""
    _invited_user(mu.registry, "alpha", "alpha@example.com")
    _invited_user(mu.registry, "beta", "beta@example.com")

    alpha_store = await mu.registry.store_for_subject("google:alpha@example.com")
    beta_store = await mu.registry.store_for_subject("google:beta@example.com")
    assert alpha_store.root != beta_store.root

    async def _write(store, tag):
        return await store.kb_write(
            "projects/race/context.md",
            f"---\ntype: project\ndescription: {tag}\n---\n\n# About\n\n{tag} content.\n",
            f"feat: {tag} race write",
        )

    results = await asyncio.gather(
        *[_write(alpha_store, "alpha") for _ in range(5)],
        *[_write(beta_store, "beta") for _ in range(5)],
    )
    assert all(r["pushed"] for r in results)

    alpha_text = (alpha_store.root / "projects" / "race" / "context.md").read_text(encoding="utf-8")
    beta_text = (beta_store.root / "projects" / "race" / "context.md").read_text(encoding="utf-8")
    assert "alpha content" in alpha_text and "beta content" not in alpha_text
    assert "beta content" in beta_text and "alpha content" not in beta_text
