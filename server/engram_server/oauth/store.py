from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


@dataclass
class PendingAuth:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    client_state: str | None
    resource: str | None


class InMemoryOAuthStore:
    """OAuth store, optionally persisted to a JSON file so logins survive restarts.

    Durable state (clients, access tokens, refresh tokens) is flushed to ``path`` on every change
    and reloaded on construction. Ephemeral state (auth codes, pending-auth) is never persisted.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, PendingAuth] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}
        if self._path and os.path.exists(self._path):
            self._load()

    # --- persistence (durable dicts only) ---
    def _save(self) -> None:
        if not self._path:
            return
        data = {
            "clients": {k: v.model_dump(mode="json") for k, v in self._clients.items()},
            "access": {k: v.model_dump(mode="json") for k, v in self._access.items()},
            "refresh": {k: v.model_dump(mode="json") for k, v in self._refresh.items()},
        }
        tmp = self._path + ".tmp"
        try:
            # Owner-only (0o600) — this file holds access/refresh tokens + client secrets.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except OSError:
            # best-effort; a later mutation re-flushes. Don't leave a stale temp file behind.
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for key, model in (("clients", OAuthClientInformationFull), ("access", AccessToken), ("refresh", RefreshToken)):
            target = {"clients": self._clients, "access": self._access, "refresh": self._refresh}[key]
            for k, d in (data.get(key) or {}).items():
                try:
                    target[k] = model.model_validate(d)
                except Exception:
                    pass

    # --- clients ---
    def put_client(self, info: OAuthClientInformationFull) -> None:
        self._clients[info.client_id] = info
        self._save()

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    # --- pending (single-use, NOT persisted) ---
    def put_pending(self, nonce: str, pending: PendingAuth) -> None:
        self._pending[nonce] = pending

    def pop_pending(self, nonce: str) -> PendingAuth | None:
        return self._pending.pop(nonce, None)

    # --- auth codes (NOT persisted) ---
    def put_code(self, code: AuthorizationCode) -> None:
        self._codes[code.code] = code

    def get_code(self, code: str) -> AuthorizationCode | None:
        return self._codes.get(code)

    def del_code(self, code: str) -> None:
        self._codes.pop(code, None)

    # --- access tokens ---
    def put_access(self, token: AccessToken) -> None:
        self._access[token.token] = token
        self._save()

    def get_access(self, token: str) -> AccessToken | None:
        return self._access.get(token)

    def del_access(self, token: str) -> None:
        self._access.pop(token, None)
        self._save()

    # --- refresh tokens ---
    def put_refresh(self, token: RefreshToken) -> None:
        self._refresh[token.token] = token
        self._save()

    def get_refresh(self, token: str) -> RefreshToken | None:
        return self._refresh.get(token)

    def del_refresh(self, token: str) -> None:
        self._refresh.pop(token, None)
        self._save()
