from __future__ import annotations

from datetime import date
from hashlib import sha256

import httpx
import pytest

from epub_news_feeder.drive import (
    DriveCredentials,
    DriveError,
    DriveFile,
    DriveFolderEntry,
    HttpDriveClient,
    archive_due_editions,
    deliver_drive,
)


def _credentials() -> DriveCredentials:
    return DriveCredentials(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="the-client-secret-value",
        refresh_token="the-refresh-token-value",
    )


def _token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "the-access-token-value"})
    return None


class FakeDriveClient:
    """In-memory DriveClient double: no network, mirrors the local filesystem's semantics."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self._next_id = 0
        self.upload_calls = 0
        self.folder_entries: dict[str, list[DriveFolderEntry]] = {}
        self.moves: list[tuple[str, str, str]] = []

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        del folder_id
        entry = self.files.get(filename)
        if entry is None:
            return None
        file_id, content = entry
        return DriveFile(file_id=file_id, sha256=sha256(content).hexdigest())

    def upload(
        self,
        *,
        folder_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/epub+zip",
    ) -> str:
        del folder_id, content_type
        self.upload_calls += 1
        self._next_id += 1
        file_id = f"drive-file-{self._next_id}"
        self.files[filename] = (file_id, content)
        return file_id

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        del content_type
        for filename, (existing_id, _content) in self.files.items():
            if existing_id == file_id:
                self.files[filename] = (existing_id, content)
                return existing_id
        raise KeyError(file_id)

    def download(self, *, file_id: str) -> bytes:
        for _filename, (existing_id, content) in self.files.items():
            if existing_id == file_id:
                return content
        raise KeyError(file_id)

    def list_folder(self, *, folder_id: str) -> tuple[DriveFolderEntry, ...]:
        return tuple(self.folder_entries.get(folder_id, []))

    def move(self, *, file_id: str, from_folder_id: str, to_folder_id: str) -> str:
        self.moves.append((file_id, from_folder_id, to_folder_id))
        source = self.folder_entries.get(from_folder_id, [])
        entry = next((candidate for candidate in source if candidate.file_id == file_id), None)
        if entry is not None:
            self.folder_entries[from_folder_id] = [
                candidate for candidate in source if candidate.file_id != file_id
            ]
            self.folder_entries.setdefault(to_folder_id, []).append(entry)
        return file_id


def test_uploads_a_new_delivery_copy_and_acknowledges_a_verified_digest() -> None:
    client = FakeDriveClient()

    receipt = deliver_drive(
        b"epub bytes", client=client, folder_id="folder-1", filename="morning.epub"
    )

    assert receipt.path.name == "drive-file-1"
    assert receipt.sha256 == sha256(b"epub bytes").hexdigest()
    assert receipt.size_bytes == len(b"epub bytes")
    assert client.upload_calls == 1


def test_identical_existing_copy_is_acknowledged_idempotently_without_reuploading() -> None:
    client = FakeDriveClient()
    first = deliver_drive(
        b"epub bytes", client=client, folder_id="folder-1", filename="morning.epub"
    )

    second = deliver_drive(
        b"epub bytes", client=client, folder_id="folder-1", filename="morning.epub"
    )

    assert second == first
    assert client.upload_calls == 1


def test_never_overwrites_an_immutable_delivery_copy() -> None:
    client = FakeDriveClient()
    deliver_drive(b"epub bytes", client=client, folder_id="folder-1", filename="morning.epub")

    with pytest.raises(FileExistsError):
        deliver_drive(
            b"different bytes", client=client, folder_id="folder-1", filename="morning.epub"
        )

    assert client.upload_calls == 1


@pytest.mark.parametrize(
    "filename",
    ["morning", "morning.txt", "sub/morning.epub", "../morning.epub"],
)
def test_delivery_copy_filename_must_be_one_epub_filename(filename: str) -> None:
    client = FakeDriveClient()

    with pytest.raises(ValueError, match=r"one \.epub filename"):
        deliver_drive(b"epub bytes", client=client, folder_id="folder-1", filename=filename)


# --- list_folder: listing and pagination of the delivery/archive folders ------------


@pytest.mark.contract
def test_list_folder_returns_entries_from_a_single_page() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        assert request.url.path == "/drive/v3/files"
        assert request.url.params["q"] == "'folder-1' in parents and trashed = false"
        assert request.url.params["fields"] == "nextPageToken,files(id,name)"
        assert request.url.params["orderBy"] == "name"
        assert request.url.params["pageSize"] == "100"
        assert "pageToken" not in request.url.params
        return httpx.Response(
            200,
            json={
                "files": [
                    {"id": "drive-file-1", "name": "b.epub"},
                    {"id": "drive-file-2", "name": "a.epub"},
                ]
            },
        )

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    entries = client.list_folder(folder_id="folder-1")

    assert entries == (
        DriveFolderEntry(file_id="drive-file-1", name="b.epub"),
        DriveFolderEntry(file_id="drive-file-2", name="a.epub"),
    )


@pytest.mark.contract
def test_list_folder_follows_pagination_until_no_next_page_token() -> None:
    pages = [
        {"nextPageToken": "page-2", "files": [{"id": "drive-file-1", "name": "a.epub"}]},
        {"files": [{"id": "drive-file-2", "name": "b.epub"}]},
    ]
    seen_tokens: list[str | None] = []

    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        seen_tokens.append(request.url.params.get("pageToken"))
        return httpx.Response(200, json=pages[len(seen_tokens) - 1])

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    entries = client.list_folder(folder_id="folder-1")

    assert [entry.file_id for entry in entries] == ["drive-file-1", "drive-file-2"]
    assert seen_tokens == [None, "page-2"]


@pytest.mark.contract
def test_list_folder_rejects_an_entry_missing_a_name() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        return httpx.Response(200, json={"files": [{"id": "drive-file-1"}]})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    with pytest.raises(DriveError):
        client.list_folder(folder_id="folder-1")


@pytest.mark.contract
def test_list_folder_rejects_an_entry_missing_an_id() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        return httpx.Response(200, json={"files": [{"name": "morning.epub"}]})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    with pytest.raises(DriveError):
        client.list_folder(folder_id="folder-1")


# --- move: relocating a Delivery Copy from the delivery folder to the archive folder ---


@pytest.mark.contract
def test_move_sends_add_and_remove_parents_and_returns_the_file_id() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        token = _token_ok(request)
        if token is not None:
            return token
        assert request.method == "PATCH"
        assert request.url.path == "/drive/v3/files/drive-file-1"
        assert request.url.params["addParents"] == "archive"
        assert request.url.params["removeParents"] == "delivery"
        assert request.headers["content-type"] == "application/json"
        assert request.content == b"{}"
        return httpx.Response(200, json={"id": "drive-file-1"})

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    returned = client.move(
        file_id="drive-file-1", from_folder_id="delivery", to_folder_id="archive"
    )

    assert returned == "drive-file-1"


@pytest.mark.contract
def test_move_permission_403_is_settled_and_is_not_retried() -> None:
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

    client = HttpDriveClient(credentials=_credentials(), transport=httpx.MockTransport(google))

    with pytest.raises(DriveError):
        client.move(file_id="drive-file-1", from_folder_id="delivery", to_folder_id="archive")

    assert len(attempts) == 1


# --- archive_due_editions: delivery-folder housekeeping, moving only what it named ----


def test_archive_due_editions_moves_only_matching_editions_older_than_the_retention_window() -> (
    None
):
    client = FakeDriveClient()
    client.folder_entries["delivery"] = [
        DriveFolderEntry(file_id="f-due", name="2026-08-10-daily-ABCDEFGH.epub"),
        DriveFolderEntry(file_id="f-boundary", name="2026-08-14-daily-ABCDEFGH.epub"),
        DriveFolderEntry(file_id="f-fresh", name="2026-08-20-daily-ABCDEFGH.epub"),
        DriveFolderEntry(
            file_id="f-legacy",
            name="epub-news--2026-08-09T175307Z--20260809T175307Z-CSYDQEHQ.epub",
        ),
        DriveFolderEntry(file_id="f-state", name="state-production.tar.gz"),
        DriveFolderEntry(file_id="f-invalid-date", name="2026-13-40-daily-ABCDEFGH.epub"),
    ]

    archived = archive_due_editions(
        client=client,
        delivery_folder_id="delivery",
        archive_folder_id="archive",
        generated_at_date=date(2026, 8, 21),
    )

    assert archived == ("2026-08-10-daily-ABCDEFGH.epub",)
    assert client.moves == [("f-due", "delivery", "archive")]
    assert [entry.name for entry in client.folder_entries["archive"]] == [
        "2026-08-10-daily-ABCDEFGH.epub"
    ]
    remaining = {entry.name for entry in client.folder_entries["delivery"]}
    assert remaining == {
        "2026-08-14-daily-ABCDEFGH.epub",
        "2026-08-20-daily-ABCDEFGH.epub",
        "epub-news--2026-08-09T175307Z--20260809T175307Z-CSYDQEHQ.epub",
        "state-production.tar.gz",
        "2026-13-40-daily-ABCDEFGH.epub",
    }


def test_archive_due_editions_returns_names_sorted_ascending() -> None:
    client = FakeDriveClient()
    client.folder_entries["delivery"] = [
        DriveFolderEntry(file_id="f-b", name="2026-08-02-daily-ABCDEFGH.epub"),
        DriveFolderEntry(file_id="f-a", name="2026-08-01-daily-ABCDEFGH.epub"),
    ]

    archived = archive_due_editions(
        client=client,
        delivery_folder_id="delivery",
        archive_folder_id="archive",
        generated_at_date=date(2026, 8, 21),
    )

    assert archived == (
        "2026-08-01-daily-ABCDEFGH.epub",
        "2026-08-02-daily-ABCDEFGH.epub",
    )


def test_archive_due_editions_lets_a_drive_error_propagate() -> None:
    class FailingClient(FakeDriveClient):
        def move(self, *, file_id: str, from_folder_id: str, to_folder_id: str) -> str:
            raise DriveError("Drive move failed")

    client = FailingClient()
    client.folder_entries["delivery"] = [
        DriveFolderEntry(file_id="f-due", name="2026-08-01-daily-ABCDEFGH.epub"),
    ]

    with pytest.raises(DriveError):
        archive_due_editions(
            client=client,
            delivery_folder_id="delivery",
            archive_folder_id="archive",
            generated_at_date=date(2026, 8, 21),
        )
