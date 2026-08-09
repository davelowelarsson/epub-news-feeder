from __future__ import annotations

from hashlib import sha256

import pytest

from epub_news_feeder.drive import DriveFile, deliver_drive


class FakeDriveClient:
    """In-memory DriveClient double: no network, mirrors the local filesystem's semantics."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self._next_id = 0
        self.upload_calls = 0

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
