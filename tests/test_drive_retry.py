"""Bounded retry and permanent-rejection behavior at the Drive boundary.

The scheduled Edition has a delivery deadline, so a Drive request retries only what a retry
can actually fix, only a handful of times, and never long enough to spend the morning.
"""

from __future__ import annotations

import httpx
import pytest

from epub_news_feeder.drive import (
    DriveAuthError,
    DriveCredentials,
    DriveError,
    HttpDriveClient,
    RetryPolicy,
)

_TOKEN_HOST = "oauth2.googleapis.com"


def _credentials() -> DriveCredentials:
    return DriveCredentials(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="the-client-secret-value",
        refresh_token="the-refresh-token-value",
    )


def _client(
    handler: object, *, policy: RetryPolicy | None = None
) -> tuple[HttpDriveClient, list[float]]:
    """A client whose backoff is recorded rather than slept, so tests stay instant."""

    slept: list[float] = []
    client = HttpDriveClient(
        credentials=_credentials(),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        retry=policy
        or RetryPolicy(attempts=4, initial_backoff_seconds=1, maximum_backoff_seconds=8),
        sleep=slept.append,
    )
    return client, slept


def _token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.host == _TOKEN_HOST:
        return httpx.Response(200, json={"access_token": "the-access-token-value"})
    return None


# --- transient failures are retried, with bounded exponential backoff ---------------


@pytest.mark.contract
def test_a_transient_status_is_retried_until_it_succeeds() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="backend error")
        return httpx.Response(200, json={"files": [{"id": "drive-file-1", "properties": {}}]})

    client, slept = _client(google)

    found = client.find_file(folder_id="folder-1", filename="state-production.tar.gz")

    assert found is not None
    assert len(attempts) == 3
    assert slept == [1, 2]


@pytest.mark.contract
def test_a_connection_failure_is_retried() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"files": []})

    client, slept = _client(google)

    assert client.find_file(folder_id="folder-1", filename="state.tar.gz") is None
    assert len(attempts) == 2
    assert slept == [1]


@pytest.mark.contract
def test_retries_stop_at_the_configured_attempt_ceiling() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        return httpx.Response(503, text="backend error")

    client, slept = _client(google, policy=RetryPolicy(attempts=3, initial_backoff_seconds=1))

    with pytest.raises(DriveError, match="Drive file lookup failed"):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(attempts) == 3
    assert slept == [1, 2]


@pytest.mark.contract
def test_backoff_grows_exponentially_and_is_capped() -> None:
    policy = RetryPolicy(
        attempts=6, initial_backoff_seconds=1, multiplier=2, maximum_backoff_seconds=4
    )

    assert [policy.backoff_seconds(attempt) for attempt in range(1, 6)] == [1, 2, 4, 4, 4]


@pytest.mark.contract
def test_the_default_policy_cannot_spend_the_morning_waiting() -> None:
    policy = RetryPolicy()

    assert 1 < policy.attempts <= 5
    total = sum(policy.backoff_seconds(attempt) for attempt in range(1, policy.attempts))
    assert total <= 30


# --- permanent failures are not retried --------------------------------------------


@pytest.mark.contract
def test_a_client_error_is_permanent_and_is_not_retried() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        return httpx.Response(404, text="not found")

    client, slept = _client(google)

    with pytest.raises(DriveError):
        client.download(file_id="drive-file-1")

    assert len(attempts) == 1
    assert slept == []


@pytest.mark.contract
def test_a_rejected_refresh_token_is_not_retried_and_names_googles_own_reason() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": "invalid_grant"})

    client, slept = _client(google)

    with pytest.raises(DriveAuthError, match="invalid_grant"):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(attempts) == 1
    assert slept == []


@pytest.mark.contract
def test_a_rejected_refresh_token_is_a_drive_error_too() -> None:
    """Existing fail-closed handling catches ``DriveError``; the narrower type refines it."""

    assert issubclass(DriveAuthError, DriveError)


@pytest.mark.contract
def test_a_token_endpoint_outage_is_still_retried() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TOKEN_HOST:
            attempts.append(1)
            if len(attempts) < 2:
                return httpx.Response(503, text="backend error")
            return httpx.Response(200, json={"access_token": "the-access-token-value"})
        return httpx.Response(200, json={"files": []})

    client, slept = _client(google)

    assert client.find_file(folder_id="folder-1", filename="state.tar.gz") is None
    assert len(attempts) == 2
    assert slept == [1]


# --- a create-new upload is never replayed after the request left the machine -------


@pytest.mark.contract
def test_an_upload_is_not_replayed_once_the_request_has_been_sent() -> None:
    """A read timeout may mean Google accepted it; replaying would deliver a second copy."""

    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    client, _slept = _client(google)

    with pytest.raises(DriveError, match="Drive upload failed"):
        client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    assert len(attempts) == 1


@pytest.mark.contract
def test_an_upload_is_retried_when_the_request_never_left_the_machine() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"id": "drive-file-1"})

    client, _slept = _client(google)

    file_id = client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    assert file_id == "drive-file-1"
    assert len(attempts) == 2


@pytest.mark.contract
def test_an_in_place_state_update_is_replayable_because_it_overwrites() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": "drive-file-1"})

    client, _slept = _client(google)

    returned = client.update(
        file_id="drive-file-1", content=b"archive bytes", content_type="application/gzip"
    )

    assert returned == "drive-file-1"
    assert len(attempts) == 2


# --- security ----------------------------------------------------------------------


@pytest.mark.security
def test_a_rejected_credential_error_never_carries_a_credential_value() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token the-refresh-token-value has been expired or revoked.",
            },
        )

    client, _slept = _client(google)

    with pytest.raises(DriveAuthError) as excinfo:
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    message = str(excinfo.value)
    assert "the-refresh-token-value" not in message
    assert "the-client-secret-value" not in message
    assert "expired or revoked" not in message


@pytest.mark.security
def test_an_unparsable_rejection_still_fails_closed_without_echoing_the_body() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="refresh_token=the-refresh-token-value is invalid")

    client, _slept = _client(google)

    with pytest.raises(DriveAuthError) as excinfo:
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert "the-refresh-token-value" not in str(excinfo.value)
