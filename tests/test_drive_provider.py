from __future__ import annotations

from hashlib import sha256

import httpx
import pytest

from epub_news_feeder.drive import (
    DriveConfigurationError,
    DriveCredentials,
    DriveError,
    HttpDriveClient,
    credentials_from_environment,
)


def _credentials() -> DriveCredentials:
    return DriveCredentials(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="the-client-secret-value",
        refresh_token="the-refresh-token-value",
    )


@pytest.mark.contract
def test_find_file_exchanges_refresh_token_and_queries_the_named_folder() -> None:
    requests: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert request.url.path == "/token"
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body == {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "the-client-secret-value",
                "refresh_token": "the-refresh-token-value",
                "grant_type": "refresh_token",
            }
            return httpx.Response(200, json={"access_token": "the-access-token-value"})
        assert request.url.path == "/drive/v3/files"
        assert request.headers["authorization"] == "Bearer the-access-token-value"
        assert (
            request.url.params["q"]
            == "'folder-1' in parents and name = 'morning.epub' and trashed = false"
        )
        return httpx.Response(
            200,
            json={"files": [{"id": "drive-file-1", "properties": {"sha256": "abc123"}}]},
        )

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    found = client.find_file(folder_id="folder-1", filename="morning.epub")

    assert found is not None
    assert found.file_id == "drive-file-1"
    assert found.sha256 == "abc123"
    assert [request.url.host for request in requests] == [
        "oauth2.googleapis.com",
        "www.googleapis.com",
    ]


@pytest.mark.contract
def test_find_file_returns_none_when_no_file_matches() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"files": []})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    assert client.find_file(folder_id="folder-1", filename="morning.epub") is None


@pytest.mark.contract
def test_upload_sends_multipart_metadata_and_epub_bytes() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "the-access-token-value"})
        assert request.url.path == "/upload/drive/v3/files"
        assert request.url.params["uploadType"] == "multipart"
        assert request.headers["authorization"] == "Bearer the-access-token-value"
        body = request.content
        assert b"application/epub+zip" in body
        assert b"morning.epub" in body
        assert b"folder-1" in body
        assert sha256(b"epub bytes").hexdigest().encode() in body
        assert b"epub bytes" in body
        return httpx.Response(200, json={"id": "drive-file-2"})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    file_id = client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    assert file_id == "drive-file-2"


@pytest.mark.security
def test_token_exchange_failure_never_leaks_credentials_in_the_error() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    with pytest.raises(DriveError) as excinfo:
        client.find_file(folder_id="folder-1", filename="morning.epub")

    message = str(excinfo.value)
    assert "the-refresh-token-value" not in message
    assert "the-client-secret-value" not in message


@pytest.mark.security
def test_upload_failure_never_leaks_the_access_token_in_the_error() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "the-access-token-value"})
        return httpx.Response(500, text="internal error")

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    with pytest.raises(DriveError) as excinfo:
        client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    message = str(excinfo.value)
    assert "the-access-token-value" not in message
    assert "the-refresh-token-value" not in message
    assert "the-client-secret-value" not in message


@pytest.mark.security
def test_credentials_from_environment_error_names_missing_variables_never_values() -> None:
    with pytest.raises(DriveConfigurationError) as excinfo:
        credentials_from_environment(
            {
                "GOOGLE_OAUTH_CLIENT_ID": "client-id",
                "GOOGLE_OAUTH_CLIENT_SECRET": "",
                "GOOGLE_OAUTH_REFRESH_TOKEN": "",
            }
        )

    message = str(excinfo.value)
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in message
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" in message
    assert "client-id" not in message


def test_credentials_from_environment_reads_all_three_variables() -> None:
    credentials = credentials_from_environment(
        {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh-token",
        }
    )

    assert credentials.client_id == "client-id"
    assert credentials.client_secret == "client-secret"
    assert credentials.refresh_token == "refresh-token"
