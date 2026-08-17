"""Google Drive Delivery Target: narrow httpx boundary and immutable upload.

Drive is a Delivery Target, never the State Store: identity and idempotency for a Delivery
Copy are derived from the folder contents themselves (name plus a stored ``sha256`` property),
exactly as ``deliver_local`` derives them from the local filesystem.

Failures are sorted by what an operator can do about them. A transport error or one of Google's
own "try again" statuses is retried a bounded number of times; a rejected credential is raised
as ``DriveAuthError`` on the first attempt, because no amount of retrying renews a refresh token.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import httpx

from epub_news_feeder.delivery import DeliveryReceipt

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DRIVE_API = "https://www.googleapis.com"

# Google's documented "come back later" answers. Everything else outside the 4xx exceptions
# below is a settled answer that a second identical request will only earn again.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Drive reports rate limiting as 403 with a reason, not as 429, and documents backoff as the
# response. 403 alone is not retryable — it is also how a permission failure arrives — so the
# reason has to be read before a 403 is treated as temporary.
_TRANSIENT_DRIVE_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
        "backendError",
        "internalError",
    }
)

# A credential Google has refused. 400 is what an expired or revoked refresh token earns from the
# token endpoint; 401 is what a rejected access token earns from Drive. Every other settled status
# is some other problem and must not be reported as one the operator fixes by re-authorizing.
_REJECTED_CREDENTIAL_STATUS = frozenset({400, 401})

# Failures raised before any byte reached Google, so replaying them cannot duplicate a write.
_CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# The complete set of OAuth 2 error identifiers, from RFC 6749 §5.2 plus the two Google adds.
# An allowlist rather than a shape test: a pattern like ``[a-z_]+`` would happily copy through
# any lowercase word a response put in the ``error`` field, and this value reaches a
# world-readable Actions log.
_OAUTH_ERROR_IDENTIFIERS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "admin_policy_enforced",
        "disabled_client",
    }
)

# Beyond this, an operator-supplied Retry-After is ignored: honouring a ten-minute hint would
# spend the Edition's whole delivery margin waiting.
_MAXIMUM_RETRY_AFTER_SECONDS = 30.0


class DriveError(Exception):
    """Safe Drive Delivery Target failure; never carries a credential value."""


class DriveAuthError(DriveError):
    """Google rejected the credentials outright: only re-authorizing fixes it, never a retry."""


class DriveConfigurationError(Exception):
    """Safe Drive Delivery Target configuration failure; never carries a credential value."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard one Drive request tries before giving up.

    Deliberately bounded, in two independent ways. ``attempts`` limits how many times a request
    is sent; ``deadline_seconds`` limits how long one operation may keep trying regardless, so a
    run's worst case cannot be computed as attempts times a request timeout. Without the second
    bound, four attempts against a 60-second timeout is four minutes per operation and most of
    an hour across a run — past the workflow's own 30-minute ceiling, which would kill the job
    mid-save rather than raise something a rerun can reconcile.

    The Edition has to be on the device by 07:00 Stockholm. A Drive outage that outlasts half a
    minute is not something a scheduled run can usefully wait out: failing loudly leaves the
    workflow's remaining slack for a rerun, which sitting in a backoff loop does not.
    """

    attempts: int = 4
    initial_backoff_seconds: float = 1.0
    multiplier: float = 2.0
    maximum_backoff_seconds: float = 8.0
    deadline_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("RetryPolicy.attempts must be at least 1")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("RetryPolicy backoff seconds must not be negative")
        if self.multiplier < 1:
            raise ValueError("RetryPolicy.multiplier must be at least 1")
        if self.deadline_seconds < 0:
            raise ValueError("RetryPolicy.deadline_seconds must not be negative")

    def backoff_seconds(self, attempt: int) -> float:
        """Seconds to wait after *attempt* (1-based) before trying again."""

        growth = self.initial_backoff_seconds * self.multiplier ** (attempt - 1)
        return min(growth, self.maximum_backoff_seconds)

    def worst_case_seconds(self, *, request_timeout_seconds: float) -> float:
        """The most one operation can take: the deadline, plus the attempt already in flight.

        A request is only started while the deadline has not passed, so the last one can begin
        just under it and still run its full timeout.
        """

        return self.deadline_seconds + request_timeout_seconds


@dataclass(frozen=True, slots=True)
class DriveCredentials:
    """Narrow, environment-sourced OAuth credentials for the Drive Delivery Target."""

    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class DriveFile:
    """Body-free identity of one existing Drive file."""

    file_id: str
    sha256: str | None


class DriveClient(Protocol):
    """Injected boundary implemented by a concrete Drive HTTP adapter."""

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None: ...

    def upload(
        self,
        *,
        folder_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/epub+zip",
    ) -> str: ...

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str: ...

    def download(self, *, file_id: str) -> bytes: ...


def credentials_from_environment(env: Mapping[str, str] | None = None) -> DriveCredentials:
    """Read Drive OAuth credentials from environment variables, never from config YAML."""

    source = env if env is not None else os.environ
    client_id = source.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = source.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = source.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise DriveConfigurationError(
            "Missing Google Drive OAuth environment variables: " + ", ".join(missing)
        )
    assert client_id and client_secret and refresh_token  # narrowed by the check above
    return DriveCredentials(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )


class HttpDriveClient:
    """Concrete Drive adapter: refresh-token exchange plus Drive v3 multipart upload."""

    def __init__(
        self,
        *,
        credentials: DriveCredentials,
        timeout: float = 60,
        transport: httpx.BaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._cached_token: tuple[str, float] | None = None

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        query = (
            f"'{_escape(folder_id)}' in parents and name = '{_escape(filename)}' "
            "and trashed = false"
        )
        response = self._authorized(
            lambda client, token: client.get(
                "/drive/v3/files",
                params={"q": query, "fields": "files(id,properties)", "spaces": "drive"},
                headers=_bearer(token),
            ),
            failure="Drive file lookup failed",
            replayable=True,
        )
        payload = _decode(response, failure="Drive file lookup failed")
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            return None
        first = files[0]
        if not isinstance(first, dict):
            raise DriveError("Drive file lookup returned an invalid entry")
        file_id = first.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise DriveError("Drive file lookup returned no file id")
        properties = first.get("properties")
        digest = properties.get("sha256") if isinstance(properties, dict) else None
        return DriveFile(file_id=file_id, sha256=digest if isinstance(digest, str) else None)

    def upload(
        self,
        *,
        folder_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/epub+zip",
    ) -> str:
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": content_type,
            "properties": {"sha256": sha256(content).hexdigest()},
        }
        body, content_type_header = _multipart_body(metadata, content, content_type)
        # A create-new upload is the one call that is not replayable: a request that left the
        # machine may have been accepted, and replaying it would put a second Delivery Copy in
        # the reader's folder. Only a failure to connect at all is retried.
        response = self._authorized(
            lambda client, token: client.post(
                "/upload/drive/v3/files",
                params={"uploadType": "multipart", "fields": "id"},
                headers={**_bearer(token), "Content-Type": content_type_header},
                content=body,
            ),
            failure="Drive upload failed",
            replayable=False,
        )
        payload = _decode(response, failure="Drive upload failed")
        file_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise DriveError("Drive upload returned no file id")
        return file_id

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        """Overwrite an existing file's content in place, using Drive's own revision history."""

        metadata = {"properties": {"sha256": sha256(content).hexdigest()}}
        body, content_type_header = _multipart_body(metadata, content, content_type)
        # Replayable, unlike an upload: this overwrites one known file with the same bytes, so a
        # repeated request lands on the same content rather than creating a second file.
        response = self._authorized(
            lambda client, token: client.patch(
                f"/upload/drive/v3/files/{file_id}",
                params={"uploadType": "multipart", "fields": "id"},
                headers={**_bearer(token), "Content-Type": content_type_header},
                content=body,
            ),
            failure="Drive update failed",
            replayable=True,
        )
        payload = _decode(response, failure="Drive update failed")
        returned_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(returned_id, str) or not returned_id:
            raise DriveError("Drive update returned no file id")
        return returned_id

    def download(self, *, file_id: str) -> bytes:
        response = self._authorized(
            lambda client, token: client.get(
                f"/drive/v3/files/{file_id}",
                params={"alt": "media"},
                headers=_bearer(token),
            ),
            failure="Drive download failed",
            replayable=True,
        )
        return response.content

    def _authorized(
        self,
        build: Callable[[httpx.Client, str], httpx.Response],
        *,
        failure: str,
        replayable: bool,
    ) -> httpx.Response:
        """Send a Drive request with an access token, renewing it once if Drive refuses it.

        The token is cached for its stated lifetime, so one run performs one exchange rather
        than one per operation. That cache is the reason a 401 is worth a second look: a token
        held across a run can expire mid-run, and the fix is a fresh token, not a fresh request.
        A 401 that survives a genuinely new token is a rejected credential and says so.
        """

        for renewed in (False, True):
            token = self._access_token(renew=renewed)
            try:
                return self._send(
                    # B023: the lambda is called inside this iteration, never stored.
                    lambda client: build(client, token),  # noqa: B023
                    failure=failure,
                    replayable=replayable,
                )
            except DriveAuthError:
                if renewed:
                    raise
        raise AssertionError("The renewal loop returns or raises on its second pass")

    def _access_token(self, *, renew: bool = False) -> str:
        cached = self._cached_token
        if not renew and cached is not None and self._clock() < cached[1]:
            return cached[0]
        requested_at = self._clock()
        response = self._send(
            lambda client: client.post(
                _TOKEN_URI,
                data={
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "refresh_token": self._credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            ),
            failure="Drive token exchange failed",
            replayable=True,
            credential_endpoint=True,
        )
        payload = _decode(response, failure="Drive token exchange failed")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DriveError("Drive token exchange returned no access token")
        self._cached_token = (token, requested_at + _token_lifetime_seconds(payload))
        return token

    def _send(
        self,
        build: Callable[[httpx.Client], httpx.Response],
        *,
        failure: str,
        replayable: bool,
        credential_endpoint: bool = False,
    ) -> httpx.Response:
        """Send one request, retrying only what a retry can actually fix, within a deadline.

        A settled answer is raised on the first attempt. When *replayable* is false, only a
        failure to reach Google at all is retried, because a request that was sent may have been
        acted on. Retrying also stops once the policy's deadline has passed, so this cannot spend
        attempts times the request timeout.
        """

        started = self._clock()
        for attempt in range(1, self._retry.attempts + 1):
            retry_after: float | None = None
            try:
                with self._client() as client:
                    response = build(client)
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as error:
                if _is_rejected_credential(error.response, credential_endpoint):
                    raise _credentials_rejected(error.response) from error
                if not _is_transient(error.response.status_code, error.response):
                    raise DriveError(failure) from error
                if not replayable:
                    raise DriveError(failure) from error
                retry_after = _retry_after_seconds(error.response)
                fatal: Exception = error
            except _CONNECT_PHASE_ERRORS as error:
                fatal = error
            except httpx.TransportError as error:
                if not replayable:
                    raise DriveError(failure) from error
                fatal = error
            except httpx.HTTPError as error:
                raise DriveError(failure) from error
            delay = retry_after if retry_after is not None else self._retry.backoff_seconds(attempt)
            if attempt == self._retry.attempts:
                raise DriveError(failure) from fatal
            if self._clock() - started + delay > self._retry.deadline_seconds:
                raise DriveError(failure) from fatal
            self._sleep(delay)
        raise AssertionError("The retry loop returns or raises on its final attempt")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_DRIVE_API, timeout=self._timeout, transport=self._transport, trust_env=False
        )


def _decode(response: httpx.Response, *, failure: str) -> Mapping[str, object]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise DriveError(failure) from error
    return payload if isinstance(payload, dict) else {}


def _is_rejected_credential(response: httpx.Response, credential_endpoint: bool) -> bool:
    """Whether this settled response means the credential itself was refused.

    Only 400 and 401 qualify, and 400 only from the token endpoint — a 400 from Drive is a
    malformed request, not a dead token. A 404 or a 413 from a proxy in front of either must not
    be reported as something re-authorizing fixes.
    """

    status = response.status_code
    if status in _TRANSIENT_STATUS:
        return False
    if status == 401:
        return True
    return status == 400 and credential_endpoint


def _is_transient(status: int, response: httpx.Response) -> bool:
    """Whether waiting could plausibly change the answer.

    Drive reports rate limiting as 403 with a reason rather than as 429, so a 403 is temporary
    only when it says so. Every other 403 — a permission failure, a storage quota — is settled.
    """

    if status in _TRANSIENT_STATUS:
        return True
    if status != 403:
        return False
    return _drive_error_reason(response) in _TRANSIENT_DRIVE_REASONS


def _drive_error_reason(response: httpx.Response) -> str | None:
    """Read ``error.errors[0].reason`` from a Drive error body, tolerating any other shape."""

    with suppress(json.JSONDecodeError, ValueError, AttributeError, IndexError, TypeError):
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None
        errors = error.get("errors")
        if not isinstance(errors, list) or not errors:
            return None
        first = errors[0]
        if not isinstance(first, dict):
            return None
        reason = first.get("reason")
        return reason if isinstance(reason, str) else None
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Honour a numeric ``Retry-After`` when Drive sends one, capped so it cannot eat the margin.

    Only the delta-seconds form is read. An HTTP-date form would need a clock comparison to be
    meaningful and Google does not send one here; ignoring it falls back to ordinary backoff.
    """

    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, _MAXIMUM_RETRY_AFTER_SECONDS)


def _token_lifetime_seconds(payload: Mapping[str, object]) -> float:
    """How long a fresh access token may be cached, shortened so it cannot expire mid-request.

    A missing or unusable ``expires_in`` falls back to nothing cached, which restores the older
    behaviour of exchanging per operation rather than trusting an unknown lifetime.
    """

    candidate = payload.get("expires_in")
    if isinstance(candidate, bool) or not isinstance(candidate, int | float):
        return 0.0
    return max(0.0, float(candidate) - 120.0)


def _credentials_rejected(response: httpx.Response) -> DriveError:
    """Name Google's own OAuth error identifier, which is a fixed word, never a credential.

    ``invalid_grant`` is what an expired or revoked refresh token looks like, and it is the one
    word that turns an opaque failure into an instruction: re-run ``authorize-drive``. The
    identifier is checked against the closed OAuth 2 set rather than a shape, so no value a
    response invents can reach the log. The accompanying ``error_description`` is deliberately
    never read — Google quotes the refresh token inside it.
    """

    reason: str | None = None
    with suppress(json.JSONDecodeError, ValueError):
        payload = response.json()
        if isinstance(payload, dict):
            candidate = payload.get("error")
            if isinstance(candidate, str) and candidate in _OAUTH_ERROR_IDENTIFIERS:
                reason = candidate
    detail = reason or f"HTTP {response.status_code}"
    return DriveAuthError(f"Google rejected the Drive credentials ({detail})")


def _multipart_body(
    metadata: Mapping[str, object], content: bytes, content_type: str
) -> tuple[bytes, str]:
    boundary = "epub-news-feeder-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--".encode()
    )
    return body, f"multipart/related; boundary={boundary}"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _escape(value: str) -> str:
    """Escape a value for Drive's single-quoted query grammar."""

    return value.replace("\\", "\\\\").replace("'", "\\'")


def deliver_drive(
    epub_bytes: bytes, *, client: DriveClient, folder_id: str, filename: str
) -> DeliveryReceipt:
    """Upload once and acknowledge only a digest-verified copy.

    Existing identical copies are acknowledged idempotently.  A different existing file of the
    same name is never overwritten because a Delivery Copy is immutable.
    """

    candidate = Path(filename)
    if candidate.name != filename or candidate.suffix != ".epub":
        raise ValueError("Delivery Copy filename must be one .epub filename")
    expected_digest = sha256(epub_bytes).hexdigest()
    existing = client.find_file(folder_id=folder_id, filename=filename)
    if existing is not None:
        if existing.sha256 != expected_digest:
            raise FileExistsError("A different immutable Delivery Copy already exists")
        return DeliveryReceipt(
            path=Path(existing.file_id), sha256=expected_digest, size_bytes=len(epub_bytes)
        )
    file_id = client.upload(folder_id=folder_id, filename=filename, content=epub_bytes)
    return DeliveryReceipt(path=Path(file_id), sha256=expected_digest, size_bytes=len(epub_bytes))
