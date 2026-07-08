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


@lru_cache
def get_settings() -> Settings:
    return Settings()
