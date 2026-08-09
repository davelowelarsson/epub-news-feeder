"""Google Drive Delivery Target: narrow httpx boundary and immutable upload.

Drive is a Delivery Target, never the State Store: identity and idempotency for a Delivery
Copy are derived from the folder contents themselves (name plus a stored ``sha256`` property),
exactly as ``deliver_local`` derives them from the local filesystem.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import httpx

from epub_news_feeder.delivery import DeliveryReceipt

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DRIVE_API = "https://www.googleapis.com"


class DriveError(Exception):
    """Safe Drive Delivery Target failure; never carries a credential value."""


class DriveConfigurationError(Exception):
    """Safe Drive Delivery Target configuration failure; never carries a credential value."""


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

    def upload(self, *, folder_id: str, filename: str, content: bytes) -> str: ...


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
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._transport = transport

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        token = self._access_token()
        query = (
            f"'{_escape(folder_id)}' in parents and name = '{_escape(filename)}' "
            "and trashed = false"
        )
        try:
            with self._client() as client:
                response = client.get(
                    "/drive/v3/files",
                    params={"q": query, "fields": "files(id,properties)", "spaces": "drive"},
                    headers=_bearer(token),
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise DriveError("Drive file lookup failed") from error
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

    def upload(self, *, folder_id: str, filename: str, content: bytes) -> str:
        token = self._access_token()
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "application/epub+zip",
            "properties": {"sha256": sha256(content).hexdigest()},
        }
        boundary = "epub-news-feeder-boundary"
        body = (
            (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                "Content-Type: application/epub+zip\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--".encode()
        )
        try:
            with self._client() as client:
                response = client.post(
                    "/upload/drive/v3/files",
                    params={"uploadType": "multipart", "fields": "id"},
                    headers={
                        **_bearer(token),
                        "Content-Type": f"multipart/related; boundary={boundary}",
                    },
                    content=body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise DriveError("Drive upload failed") from error
        file_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise DriveError("Drive upload returned no file id")
        return file_id

    def _access_token(self) -> str:
        try:
            with self._client() as client:
                response = client.post(
                    _TOKEN_URI,
                    data={
                        "client_id": self._credentials.client_id,
                        "client_secret": self._credentials.client_secret,
                        "refresh_token": self._credentials.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise DriveError("Drive token exchange failed") from error
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DriveError("Drive token exchange returned no access token")
        return token

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_DRIVE_API, timeout=self._timeout, transport=self._transport, trust_env=False
        )


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
