from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ENGRAM_", extra="ignore"
    )

    # brain checkout (the server's working surface for ALL reads/writes)
    brain_path: Path = Path.home() / ".engram" / "brain"
    brain_remote: str = "git@github.com:metalfinger/brain.git"
    brain_branch: str = "main"
    deploy_key_path: Path = Path.home() / ".engram" / "id_engram"
    git_author_name: str = "helix-bot"
    git_author_email: str = "helix@metalfinger.xyz"
    git_timeout: float = 60.0
    pull_ttl: float = 60.0  # read-path pull throttle, seconds

    # http
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9210
    public_url: str = "https://engram.metalfinger.xyz"  # MCP + OAuth issuer host
    explorer_url: str = (
        "https://brain.metalfinger.xyz"  # explorer host (Cloudflare Access)
    )

    # oauth (ProxyOAuthProvider -> upstream IdP)
    oauth_provider: str = "github"  # github | google
    oauth_callback_path: str = "/oauth/callback"
    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_store_path: str = ""  # e.g. C:\Users\Admin\.engram\oauth_store.json
    allowed_logins: str = "metalfinger"  # comma-separated GitHub logins / Google emails

    # explorer auth (Cloudflare Access JWT verification)
    cf_access_team_domain: str = ""  # e.g. metalfinger.cloudflareaccess.com
    cf_access_aud: str = ""  # Access application AUD tag
    dev_no_access: bool = (
        False  # NEVER enable in production (tunnel origin = 127.0.0.1)
    )

    # Brain Navigator MCP App widget (SEP-1865). OFF by default: when false the
    # kb_* tools stay plain (conversational) and the ui:// resource is unregistered
    # — zero behavior change. ENGRAM_WIDGET=1 opts in.
    widget: bool = False

    # observability
    log_level: str = "INFO"  # root/uvicorn log level (DEBUG|INFO|WARNING|ERROR)

    # semantic search (Qdrant + local fastembed ONNX embeddings). Falls back to
    # the pure-Python text scorer whenever qdrant_url is empty or any call fails.
    semantic_search: bool = True
    qdrant_url: str = ""  # e.g. https://xxxx.cloud.qdrant.io:6333 — empty = text-only
    qdrant_api_key: str = ""
    qdrant_collection: str = "engram-brain"
    embed_model: str = "BAAI/bge-small-en-v1.5"

    # Semantic near-duplicate detection: kb_write warning + the nightly
    # similar-pairs sweep. Cosine floor — bge-small near-dupes usually score > 0.8.
    dupe_threshold: float = 0.80

    # Artifact rebuild-guard (hallucination check for living documents): on a
    # type: artifact write that lists sources, kb_write compares the artifact body's
    # embedding centroid to its sources' centroid and warns (never blocks) when the
    # cosine falls below this floor — a low score means the document drifted from
    # what it claims to be built on.
    artifact_drift_threshold: float = 0.5

    # Cross-encoder rerank of kb_search results (opt-in; OFF by default — it adds
    # latency and a one-time model download). When on and the semantic backend is
    # live, the fused top-k is reranked and the blended score is
    # 0.7*fused_norm + 0.3*sigmoid(ce) so an absolute magnitude survives for abstention.
    rerank_enabled: bool = False
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    # in-process scheduler (nightly reconcile + morning briefing). ENGRAM_SCHEDULER=0
    # kills both; times are local-clock HH:MM.
    scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENGRAM_SCHEDULER", "ENGRAM_SCHEDULER_ENABLED"),
    )
    reconcile_at: str = "03:30"
    briefing_at: str = "08:00"

    # Auto-presence spool ingest. Claude Code hooks drop plain JSON intent files
    # into presence_spool_dir (NO git — a second checkout writer would race the
    # server's lock); a scheduler tick ingests them THROUGH the server lock every
    # presence_ingest_seconds, batched into one commit, deduped so a session only
    # re-commits when a meaningful field changed OR its `updated` is older than
    # presence_refresh_minutes (heartbeat throttle that keeps it inside the 15-min
    # roster window without a commit per prompt). Empty dir => the default below.
    presence_spool_dir: str = ""  # empty -> Path.home()/".engram"/"presence-spool"
    presence_ingest_seconds: int = 30
    presence_refresh_minutes: int = 5

    # v2 multi-tenancy (M0). Single-user behavior is untouched while multiuser is
    # False. tenancy_db_path holds accounts/invites (SQLite, WAL); users_root is
    # where per-user brains live (users_root/<handle>/brain.git bare + /brain
    # checkout). Empty strings resolve to the ~/.engram defaults below.
    multiuser: bool = False
    tenancy_db_path: str = ""  # empty -> Path.home()/".engram"/"engram.db"
    users_root: str = ""  # empty -> Path.home()/".engram"/"users"

    # The operator: OAuth subjects that map to the ORIGINAL brain (settings.brain_path
    # + its GitHub remote) instead of a provisioned users_root brain. Comma-separated,
    # matching oauth/provider.subject_for's "<idp>:<login>" format.
    owner_handle: str = "hiren"
    owner_subjects: str = "github:metalfinger"

    # Search isolation (M0.5): every Qdrant point is stamped with this tenant id
    # and every query/delete/scroll carries it as a mandatory filter. The shared
    # collection is the ONE cross-tenant surface — per-user stores override this
    # with the user's handle (see provisioning.user_settings).
    tenant_id: str = "hiren"

    # Per-tenant limits (M0.7). Applied at the current_store() seam for NON-owner
    # callers only. Quota is the tenant checkout's on-disk ceiling (checked with a
    # short TTL cache); the rate limit is tool calls per minute per token subject.
    tenant_quota_mb: int = 200
    tenant_rate_per_min: int = 120

    # Off-site backups (M0.8): nightly mirror of every user bare repo to ONE
    # private git remote, each user on branch users/<handle> (no per-user repo
    # creation, one deploy key). Empty = mirror disabled. The operator brain is
    # excluded — it already pushes to its own GitHub remote on every write.
    backup_remote: str = ""  # e.g. git@github.com:metalfinger/brains-mirror.git
    backup_at: str = "04:30"


@lru_cache
def get_settings() -> Settings:
    return Settings()
