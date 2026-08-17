"""Bounded retry and permanent-rejection behavior at the Drive boundary.

The scheduled Edition has a delivery deadline, so a Drive request retries only what a retry
can actually fix, only a handful of times, and never long enough to spend the morning.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

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


@pytest.mark.contract
def test_retrying_stops_at_the_deadline_even_with_attempts_left() -> None:
    """Backoff alone does not bound this: an attempt can hang for the whole request timeout."""

    attempts: list[int] = []
    elapsed = [0.0]

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        elapsed[0] += 20  # each attempt burns 20s of the 30s deadline
        return httpx.Response(503, text="backend error")

    slept: list[float] = []
    client = HttpDriveClient(
        credentials=_credentials(),
        transport=httpx.MockTransport(google),
        retry=RetryPolicy(attempts=6, initial_backoff_seconds=1, deadline_seconds=30),
        sleep=slept.append,
        clock=lambda: elapsed[0],
    )

    with pytest.raises(DriveError):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    # Two attempts, not six: the third would start past the 30s deadline.
    assert len(attempts) == 2
    assert slept == [1]


@pytest.mark.contract
def test_a_whole_runs_drive_work_stays_inside_the_workflow_timeout() -> None:
    """The bound that matters is the job's, not one request's.

    A run performs one token exchange plus six Drive operations (find+download to restore,
    find+upload to deliver, find+update to save). If each could spend attempts times the request
    timeout, the worst case would pass the workflow's own ceiling and the job would be killed
    mid-save rather than raising something a rerun can reconcile.
    """

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/daily-edition.yml").read_text(
            encoding="utf-8"
        )
    )
    job_timeout_seconds = workflow["jobs"]["edition"]["timeout-minutes"] * 60
    request_timeout_seconds = 60.0
    operations_per_run = 7

    worst_case = operations_per_run * RetryPolicy().worst_case_seconds(
        request_timeout_seconds=request_timeout_seconds
    )
    epub_and_acquisition_budget = 10 * 60

    assert worst_case + epub_and_acquisition_budget <= job_timeout_seconds


@pytest.mark.contract
@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"attempts": 0},
        {"attempts": -1},
        {"initial_backoff_seconds": -1},
        {"maximum_backoff_seconds": -1},
        {"multiplier": 0},
        {"deadline_seconds": -1},
    ],
)
def test_an_unusable_retry_policy_is_rejected_on_construction(
    policy_kwargs: dict[str, float],
) -> None:
    """Otherwise attempts=0 reaches the loop's AssertionError and a negative delay reaches sleep."""

    with pytest.raises(ValueError):
        RetryPolicy(**policy_kwargs)  # type: ignore[arg-type]


# --- Drive reports rate limiting as 403 with a reason, not as 429 --------------------


@pytest.mark.contract
def test_a_rate_limit_403_is_transient_because_its_reason_says_so() -> None:
    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(200, json={"files": []})

    client, slept = _client(google)

    assert client.find_file(folder_id="folder-1", filename="state.tar.gz") is None
    assert len(attempts) == 2
    assert slept == [1]


@pytest.mark.contract
def test_a_permission_403_is_settled_and_is_not_retried() -> None:
    """The same status, the opposite answer: waiting never grants a permission."""

    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "errors": [{"domain": "global", "reason": "insufficientFilePermissions"}],
                }
            },
        )

    client, slept = _client(google)

    with pytest.raises(DriveError):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(attempts) == 1
    assert slept == []


@pytest.mark.contract
def test_a_numeric_retry_after_is_honoured_in_place_of_backoff() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        if not slept:
            return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
        return httpx.Response(200, json={"files": []})

    slept: list[float] = []
    client = HttpDriveClient(
        credentials=_credentials(),
        transport=httpx.MockTransport(google),
        retry=RetryPolicy(attempts=4, initial_backoff_seconds=1, deadline_seconds=60),
        sleep=slept.append,
    )

    assert client.find_file(folder_id="folder-1", filename="state.tar.gz") is None
    assert slept == [5]


@pytest.mark.contract
def test_an_absurd_retry_after_is_capped_rather_than_obeyed() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        return httpx.Response(429, headers={"Retry-After": "600"}, text="slow down")

    client, slept = _client(google, policy=RetryPolicy(attempts=2, deadline_seconds=600))

    with pytest.raises(DriveError):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert slept == [30]  # the ten minutes Drive asked for, capped to thirty seconds


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
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_an_upload_is_not_replayed_after_a_transient_status_either(status: int) -> None:
    """A 503 also means the request arrived. Retrying it risks a second Delivery Copy."""

    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        return httpx.Response(status, text="try again")

    client, slept = _client(google)

    with pytest.raises(DriveError, match="Drive upload failed"):
        client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    assert len(attempts) == 1
    assert slept == []


@pytest.mark.contract
@pytest.mark.parametrize(
    "error_class", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
def test_an_upload_is_retried_when_the_request_never_left_the_machine(
    error_class: type[httpx.TransportError],
) -> None:
    """ConnectTimeout and PoolTimeout are TimeoutException, not ConnectError — both must count."""

    attempts: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        attempts.append(1)
        if len(attempts) < 2:
            raise error_class("never connected", request=request)
        return httpx.Response(200, json={"id": "drive-file-1"})

    client, _slept = _client(google)

    file_id = client.upload(folder_id="folder-1", filename="morning.epub", content=b"epub bytes")

    assert file_id == "drive-file-1"
    assert len(attempts) == 2


@pytest.mark.contract
@pytest.mark.parametrize(
    "error_class",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ReadTimeout],
)
def test_an_exhausted_transport_failure_is_a_drive_error_not_a_raw_httpx_error(
    error_class: type[httpx.TransportError],
) -> None:
    """The final attempt raises DriveError: never a raw httpx error, never the AssertionError."""

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        raise error_class("no route", request=request)

    client, slept = _client(google, policy=RetryPolicy(attempts=2, initial_backoff_seconds=1))

    with pytest.raises(DriveError, match="Drive file lookup failed") as excinfo:
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert type(excinfo.value) is DriveError
    assert isinstance(excinfo.value.__cause__, error_class)
    assert slept == [1]  # one wait between the two attempts, none after the last


@pytest.mark.contract
def test_an_in_place_state_update_replays_the_same_patch_to_the_same_file() -> None:
    """Asserting the method, the file id and the body — a replay that created a file would pass
    a test that only checked the returned id."""

    seen: list[tuple[str, str, bytes]] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        seen.append((request.method, request.url.path, request.content))
        if len(seen) < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": "drive-file-1"})

    client, _slept = _client(google)

    returned = client.update(
        file_id="drive-file-1", content=b"archive bytes", content_type="application/gzip"
    )

    assert returned == "drive-file-1"
    assert len(seen) == 2
    assert seen[0] == seen[1], "the replay must be the identical request"
    method, path, body = seen[0]
    assert method == "PATCH"
    assert path == "/upload/drive/v3/files/drive-file-1"
    assert b"archive bytes" in body


# --- one token exchange per run, renewed once if Drive refuses the access token ------


@pytest.mark.contract
def test_the_access_token_is_exchanged_once_and_reused_across_operations() -> None:
    """Six operations paying for six token exchanges is both wasteful and doubles the worst case."""

    exchanges: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TOKEN_HOST:
            exchanges.append(1)
            return httpx.Response(
                200, json={"access_token": "the-access-token-value", "expires_in": 3599}
            )
        return httpx.Response(200, json={"files": []})

    client, _slept = _client(google)

    for _ in range(3):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(exchanges) == 1


@pytest.mark.contract
def test_a_cached_token_refused_by_drive_is_renewed_once_and_the_call_succeeds() -> None:
    """A token held across a run can expire mid-run; the fix is a new token, not a new request."""

    exchanges: list[int] = []
    lookups: list[str] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TOKEN_HOST:
            exchanges.append(1)
            return httpx.Response(
                200, json={"access_token": f"token-{len(exchanges)}", "expires_in": 3599}
            )
        lookups.append(request.headers["authorization"])
        if len(lookups) == 1:
            return httpx.Response(401, json={"error": {"code": 401}})
        return httpx.Response(200, json={"files": []})

    client, _slept = _client(google)

    assert client.find_file(folder_id="folder-1", filename="state.tar.gz") is None
    assert len(exchanges) == 2
    assert lookups == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.contract
def test_a_401_that_survives_a_fresh_token_is_a_rejected_credential() -> None:
    exchanges: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TOKEN_HOST:
            exchanges.append(1)
            return httpx.Response(
                200, json={"access_token": f"token-{len(exchanges)}", "expires_in": 3599}
            )
        return httpx.Response(401, json={"error": {"code": 401}})

    client, _slept = _client(google)

    with pytest.raises(DriveAuthError):
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(exchanges) == 2


@pytest.mark.contract
def test_a_token_response_without_a_lifetime_is_not_cached() -> None:
    """An unknown lifetime is not trusted; it falls back to exchanging per operation."""

    exchanges: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TOKEN_HOST:
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": "the-access-token-value"})
        return httpx.Response(200, json={"files": []})

    client, _slept = _client(google)

    client.find_file(folder_id="folder-1", filename="state.tar.gz")
    client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert len(exchanges) == 2


# --- a settled non-credential answer is not reported as a dead refresh token --------


@pytest.mark.contract
@pytest.mark.parametrize("status", [404, 413])
def test_a_proxy_error_from_the_token_endpoint_is_not_called_a_dead_token(status: int) -> None:
    """Telling the operator to renew a refresh token they cannot fix wastes the whole morning."""

    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="gateway says no")

    client, _slept = _client(google)

    with pytest.raises(DriveError) as excinfo:
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert not isinstance(excinfo.value, DriveAuthError)


@pytest.mark.contract
def test_a_400_from_drive_itself_is_not_a_credential_rejection() -> None:
    """400 means a dead token only from the token endpoint; from Drive it is a bad request."""

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad query"}})

    client, _slept = _client(google)

    with pytest.raises(DriveError) as excinfo:
        client.find_file(folder_id="folder-1", filename="state.tar.gz")

    assert not isinstance(excinfo.value, DriveAuthError)


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
