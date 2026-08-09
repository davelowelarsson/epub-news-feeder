"""One-time interactive Google Drive authorization: loopback OAuth flow with PKCE.

Requests exactly the narrowest Drive scope Google offers, ``drive.file``: the app may only
touch files it created. The refresh token this produces is printed to stdout only; it is never
written to a file, never logged, and never reaches diagnostics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import cast

import httpx

from epub_news_feeder.drive import DRIVE_FILE_SCOPE

_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


class DriveAuthorizationError(Exception):
    """Safe authorization failure; never carries a credential or token value."""


@dataclass(frozen=True, slots=True)
class ClientSecret:
    """The Desktop ("installed") OAuth client identity, read once from the operator's file."""

    client_id: str
    client_secret: str


def find_client_secret(directory: Path) -> Path:
    """Locate the single ``client_secret_*.json`` file in *directory*."""

    matches = sorted(directory.glob("client_secret_*.json"))
    if not matches:
        raise DriveAuthorizationError("No client_secret_*.json file was found")
    if len(matches) > 1:
        raise DriveAuthorizationError(
            "Multiple client_secret_*.json files were found; pass --client-secret explicitly"
        )
    return matches[0]


def load_client_secret(path: Path) -> ClientSecret:
    """Read a Google Desktop OAuth client file without ever echoing its contents."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DriveAuthorizationError("Client secret file could not be read") from error
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise DriveAuthorizationError("Client secret file is not a Desktop OAuth client")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if (
        not isinstance(client_id, str)
        or not client_id
        or not isinstance(client_secret, str)
        or not client_secret
    ):
        raise DriveAuthorizationError("Client secret file is missing client credentials")
    return ClientSecret(client_id=client_id, client_secret=client_secret)


def _code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _RedirectServer(HTTPServer):
    """A one-shot loopback HTTP server that captures a single OAuth redirect."""

    result: dict[str, str] | None = None


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        server = cast(_RedirectServer, self.server)
        server.result = dict(urllib.parse.parse_qsl(parsed.query))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body>Authorization complete. You may close this window.</body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:
        return


def build_consent_url(
    client_secret: ClientSecret, *, redirect_uri: str, code_challenge: str, scope: str
) -> str:
    """Build the narrow-scope, PKCE, offline-access consent URL."""

    query = urllib.parse.urlencode(
        {
            "client_id": client_secret.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTH_URI}?{query}"


def authorize(
    client_secret: ClientSecret,
    *,
    open_url: Callable[[str], None],
    scope: str = DRIVE_FILE_SCOPE,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 300,
) -> str:
    """Run the loopback PKCE flow once end-to-end and return the refresh token."""

    verifier = _code_verifier()
    challenge = _code_challenge(verifier)
    server = _RedirectServer(("127.0.0.1", 0), _RedirectHandler)
    server.timeout = timeout
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/"
    consent_url = build_consent_url(
        client_secret, redirect_uri=redirect_uri, code_challenge=challenge, scope=scope
    )
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        open_url(consent_url)
        thread.join(timeout=timeout)
    finally:
        server.server_close()
    result = server.result
    if result is None:
        raise DriveAuthorizationError("No authorization response was received")
    if "error" in result:
        raise DriveAuthorizationError("Google reported an authorization error")
    code = result.get("code")
    if not isinstance(code, str) or not code:
        raise DriveAuthorizationError("No authorization code was received")
    return _exchange_code(
        client_secret, code=code, verifier=verifier, redirect_uri=redirect_uri, transport=transport
    )


def _exchange_code(
    client_secret: ClientSecret,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    transport: httpx.BaseTransport | None,
) -> str:
    try:
        with httpx.Client(transport=transport, timeout=30, trust_env=False) as client:
            response = client.post(
                _TOKEN_URI,
                data={
                    "client_id": client_secret.client_id,
                    "client_secret": client_secret.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as error:
        raise DriveAuthorizationError("Token exchange failed") from error
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise DriveAuthorizationError(
            "Google did not return a refresh token; revoke prior access for this app and retry"
        )
    return refresh_token
