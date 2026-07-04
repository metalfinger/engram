"""OAuth for the Engram MCP server: ProxyOAuthProvider (upstream IdP + allowlist), IdPs, store."""

from engram_server.oauth.idp import GitHubIdP, GoogleIdP, UpstreamIdP, UpstreamUser, get_idp
from engram_server.oauth.provider import (
    ACCESS_TTL,
    CODE_TTL,
    LoginNotAllowedError,
    ProxyOAuthProvider,
    handle_callback,
)
from engram_server.oauth.store import InMemoryOAuthStore, PendingAuth

__all__ = [
    "ACCESS_TTL",
    "CODE_TTL",
    "GitHubIdP",
    "GoogleIdP",
    "InMemoryOAuthStore",
    "LoginNotAllowedError",
    "PendingAuth",
    "ProxyOAuthProvider",
    "UpstreamIdP",
    "UpstreamUser",
    "get_idp",
    "handle_callback",
]
