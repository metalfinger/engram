from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from engram_server.config import Settings


@dataclass(frozen=True)
class UpstreamUser:
    id: str
    login: str


class UpstreamIdP:
    name: str = ""
    authorize_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    scope: str = ""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scope,
                "state": state,
                "response_type": "code",
            }
        )
        return f"{self.authorize_endpoint}?{query}"

    async def exchange_code(self, code: str, redirect_uri: str, http: httpx.AsyncClient) -> str:
        resp = await http.post(
            self.token_endpoint,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    async def fetch_user(self, upstream_token: str, http: httpx.AsyncClient) -> UpstreamUser:
        raise NotImplementedError


class GitHubIdP(UpstreamIdP):
    name = "github"
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"
    userinfo_endpoint = "https://api.github.com/user"
    scope = "read:user"

    async def fetch_user(self, upstream_token: str, http: httpx.AsyncClient) -> UpstreamUser:
        resp = await http.get(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {upstream_token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return UpstreamUser(id=str(data["id"]), login=data.get("login", str(data["id"])))


class GoogleIdP(UpstreamIdP):
    name = "google"
    authorize_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    scope = "openid email profile"

    async def fetch_user(self, upstream_token: str, http: httpx.AsyncClient) -> UpstreamUser:
        resp = await http.get(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {upstream_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return UpstreamUser(id=str(data["sub"]), login=data.get("email", str(data["sub"])))


def get_idp(name: str, settings: Settings) -> UpstreamIdP:
    if name == "github":
        return GitHubIdP(settings.github_client_id, settings.github_client_secret)
    if name == "google":
        return GoogleIdP(settings.google_client_id, settings.google_client_secret)
    raise ValueError(f"unknown ENGRAM_OAUTH_PROVIDER: {name}")
