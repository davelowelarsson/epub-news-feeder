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
import re
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

# Google's documented "come back later" answers. Everything else in the 4xx range is a settled
# answer that a second identical request will only earn again.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Failures raised before any byte reached Google, so replaying them cannot duplicate a write.
_CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Google's OAuth error identifiers are a small fixed vocabulary (``invalid_grant`` and friends).
# Matching against this shape is what guarantees a rejection message can never echo a credential.
_OAUTH_REASON = re.compile(r"[a-z_]{1,40}")


class DriveError(Exception):
    """Safe Drive Delivery Target failure; never carries a credential value."""


class DriveAuthError(DriveError):
    """Google rejected the credentials outright: only re-authorizing fixes it, never a retry."""


class DriveConfigurationError(Exception):
    """Safe Drive Delivery Target configuration failure; never carries a credential value."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard one Drive request tries before giving up.

    Deliberately bounded. The Edition has to be on the device by 07:00 Stockholm, and a Drive
    outage that outlasts a few seconds is not something a scheduled run can usefully wait out —
    failing loudly leaves the whole workflow's remaining slack for a rerun, which sitting in a
    backoff loop does not.
    """

    attempts: int = 4
    initial_backoff_seconds: float = 1.0
    multiplier: float = 2.0
    maximum_backoff_seconds: float = 8.0

    def backoff_seconds(self, attempt: int) -> float:
        """Seconds to wait after *attempt* (1-based) before trying again."""

        growth = self.initial_backoff_seconds * self.multiplier ** (attempt - 1)
        return min(growth, self.maximum_backoff_seconds)


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
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        token = self._access_token()
        query = (
            f"'{_escape(folder_id)}' in parents and name = '{_escape(filename)}' "
            "and trashed = false"
        )
        response = self._send(
            lambda client: client.get(
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
        token = self._access_token()
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
        response = self._send(
            lambda client: client.post(
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

        token = self._access_token()
        metadata = {"properties": {"sha256": sha256(content).hexdigest()}}
        body, content_type_header = _multipart_body(metadata, content, content_type)
        # Replayable, unlike an upload: this overwrites one known file with the same bytes, so a
        # repeated request lands on the same content rather than creating a second file.
        response = self._send(
            lambda client: client.patch(
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
        token = self._access_token()
        response = self._send(
            lambda client: client.get(
                f"/drive/v3/files/{file_id}",
                params={"alt": "media"},
                headers=_bearer(token),
            ),
            failure="Drive download failed",
            replayable=True,
        )
        return response.content

    def _access_token(self) -> str:
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
            rejected=_credentials_rejected,
        )
        payload = _decode(response, failure="Drive token exchange failed")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DriveError("Drive token exchange returned no access token")
        return token

    def _send(
        self,
        build: Callable[[httpx.Client], httpx.Response],
        *,
        failure: str,
        replayable: bool,
        rejected: Callable[[httpx.Response], DriveError] | None = None,
    ) -> httpx.Response:
        """Send one request, retrying only what a retry can actually fix.

        A settled answer — any status outside ``_TRANSIENT_STATUS`` — is raised on the first
        attempt. When *replayable* is false, only a failure to reach Google at all is retried,
        because a request that was sent may have been acted on.
        """

        for attempt in range(1, self._retry.attempts + 1):
            last = attempt == self._retry.attempts
            try:
                with self._client() as client:
                    response = build(client)
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as error:
                settled = error.response.status_code not in _TRANSIENT_STATUS
                if settled and rejected is not None:
                    raise rejected(error.response) from error
                if settled or last or not replayable:
                    raise DriveError(failure) from error
            except _CONNECT_PHASE_ERRORS as error:
                if last:
                    raise DriveError(failure) from error
            except httpx.TransportError as error:
                if last or not replayable:
                    raise DriveError(failure) from error
            except httpx.HTTPError as error:
                raise DriveError(failure) from error
            self._sleep(self._retry.backoff_seconds(attempt))
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


def _credentials_rejected(response: httpx.Response) -> DriveError:
    """Name Google's own OAuth error identifier, which is a fixed word, never a credential.

    ``invalid_grant`` is what an expired or revoked refresh token looks like, and it is the one
    word that turns an opaque failure into an instruction: re-run ``authorize-drive``. The
    accompanying ``error_description`` is deliberately not read — Google quotes the token in it.
    """

    reason: str | None = None
    with suppress(json.JSONDecodeError, ValueError):
        payload = response.json()
        if isinstance(payload, dict):
            candidate = payload.get("error")
            if isinstance(candidate, str) and _OAUTH_REASON.fullmatch(candidate):
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
