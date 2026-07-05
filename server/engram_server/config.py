from functools import lru_cache
from pathlib import Path

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
