from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from epub_news_feeder.drive import DRIVE_FILE_SCOPE
from epub_news_feeder.drive_oauth import (
    ClientSecret,
    DriveAuthorizationError,
    authorize,
    find_client_secret,
    load_client_secret,
)


def _client_secret() -> ClientSecret:
    return ClientSecret(
        client_id="client-id.apps.googleusercontent.com", client_secret="the-client-secret-value"
    )


def _fake_browser(
    *, code: str | None = None, error: str | None = None
) -> tuple[list[str], Callable[[str], None]]:
    """Return a spy list of opened URLs and an ``open_url`` that hits the loopback redirect."""

    opened: list[str] = []

    def open_url(url: str) -> None:
        opened.append(url)
        parsed = urllib.parse.urlsplit(url)
        redirect_uri = urllib.parse.parse_qs(parsed.query)["redirect_uri"][0]
        params = {"code": code} if code is not None else {"error": error}
        httpx.get(redirect_uri, params=params)

    return opened, open_url


@pytest.mark.contract
def test_authorize_requests_exactly_the_drive_file_scope_with_pkce_and_offline_access() -> None:
    opened, open_url = _fake_browser(code="the-authorization-code")

    def google(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["code"] == "the-authorization-code"
        assert body["client_id"] == "client-id.apps.googleusercontent.com"
        assert body["client_secret"] == "the-client-secret-value"
        assert body["grant_type"] == "authorization_code"
        assert "code_verifier" in body
        return httpx.Response(200, json={"refresh_token": "the-refresh-token-value"})

    refresh_token = authorize(
        _client_secret(), open_url=open_url, transport=httpx.MockTransport(google)
    )

    assert refresh_token == "the-refresh-token-value"
    assert len(opened) == 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)
    assert query["scope"] == [DRIVE_FILE_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert "code_challenge" in query
    assert query["redirect_uri"][0].startswith("http://localhost:")


@pytest.mark.contract
def test_authorize_raises_when_google_reports_an_authorization_error() -> None:
    _, open_url = _fake_browser(error="access_denied")

    with pytest.raises(DriveAuthorizationError, match="authorization error"):
        authorize(
            _client_secret(),
            open_url=open_url,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )


@pytest.mark.security
def test_authorize_raises_a_clear_error_when_no_refresh_token_is_returned() -> None:
    _, open_url = _fake_browser(code="the-authorization-code")

    def google(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"access_token": "the-access-token-value"})

    with pytest.raises(DriveAuthorizationError, match="refresh token"):
        authorize(_client_secret(), open_url=open_url, transport=httpx.MockTransport(google))


@pytest.mark.security
def test_authorize_error_messages_never_contain_the_client_secret_or_any_token() -> None:
    _, open_url = _fake_browser(code="the-authorization-code")

    def google(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(DriveAuthorizationError) as excinfo:
        authorize(_client_secret(), open_url=open_url, transport=httpx.MockTransport(google))

    message = str(excinfo.value)
    assert "the-client-secret-value" not in message
    assert "the-authorization-code" not in message


def test_load_client_secret_reads_the_desktop_installed_client(tmp_path: Path) -> None:
    path = tmp_path / "client_secret_123.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client-id.apps.googleusercontent.com",
                    "client_secret": "the-client-secret-value",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )

    secret = load_client_secret(path)

    assert secret.client_id == "client-id.apps.googleusercontent.com"
    assert secret.client_secret == "the-client-secret-value"


def test_load_client_secret_rejects_a_non_desktop_client(tmp_path: Path) -> None:
    path = tmp_path / "client_secret_web.json"
    path.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")

    with pytest.raises(DriveAuthorizationError, match="Desktop OAuth client"):
        load_client_secret(path)


def test_find_client_secret_locates_the_single_matching_file(tmp_path: Path) -> None:
    (tmp_path / "client_secret_123.json").write_text("{}", encoding="utf-8")

    assert find_client_secret(tmp_path) == tmp_path / "client_secret_123.json"


def test_find_client_secret_requires_exactly_one_match(tmp_path: Path) -> None:
    with pytest.raises(DriveAuthorizationError, match="No client_secret"):
        find_client_secret(tmp_path)

    (tmp_path / "client_secret_a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "client_secret_b.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DriveAuthorizationError, match="Multiple client_secret"):
        find_client_secret(tmp_path)
