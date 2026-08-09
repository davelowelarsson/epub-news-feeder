"""Filesystem Delivery Target for immutable, validated Delivery Copies."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class DeliveryReceipt:
    """The acknowledgement returned after a Delivery Copy is durable and verified."""

    path: Path
    sha256: str
    size_bytes: int


def deliver_local(epub_bytes: bytes, *, output_directory: Path, filename: str) -> DeliveryReceipt:
    """Atomically place *epub_bytes* and acknowledge only a digest-verified copy.

    Existing identical copies are acknowledged idempotently.  A different existing
    file is never overwritten because a Delivery Copy is immutable.
    """

    target = _target_path(output_directory, filename)
    expected_digest = sha256(epub_bytes).hexdigest()
    expected_size = len(epub_bytes)
    if target.exists():
        return _verify_existing(target, expected_digest, expected_size)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=output_directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(epub_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(output_directory)
        return _verify_existing(target, expected_digest, expected_size)
    finally:
        if temporary.exists():
            temporary.unlink()


def _target_path(output_directory: Path, filename: str) -> Path:
    if not output_directory.is_dir():
        raise ValueError("Local Delivery Target directory does not exist")
    candidate = Path(filename)
    if candidate.name != filename or candidate.suffix != ".epub":
        raise ValueError("Delivery Copy filename must be one .epub filename")
    return output_directory / candidate


def _verify_existing(target: Path, expected_digest: str, expected_size: int) -> DeliveryReceipt:
    delivered = target.read_bytes()
    digest = sha256(delivered).hexdigest()
    if len(delivered) != expected_size or digest != expected_digest:
        raise FileExistsError("A different immutable Delivery Copy already exists")
    return DeliveryReceipt(path=target, sha256=digest, size_bytes=len(delivered))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
