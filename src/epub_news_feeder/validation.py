"""External EPUB conformance validation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path


class EpubValidationError(Exception):
    """A safe EPUBCheck failure."""


_EPUBCHECK_JAR_SHA256 = "f7f96617c929371821609b88c8484d6dc9f24fe916499863c46094c5fb778a65"


def default_epubcheck_jar() -> Path:
    configured = os.environ.get("EPUBCHECK_JAR")
    if configured:
        return Path(configured)
    return Path(".local/tools/epubcheck-5.3.0/epubcheck.jar")


def validate_epub(epub_bytes: bytes, *, jar_path: Path | None = None) -> None:
    """Require EPUBCheck to accept an in-memory Delivery Copy without warnings."""

    jar = jar_path or default_epubcheck_jar()
    if not jar.is_file():
        raise EpubValidationError("EPUBCheck is unavailable; set EPUBCHECK_JAR")
    if sha256(jar.read_bytes()).hexdigest() != _EPUBCHECK_JAR_SHA256:
        raise EpubValidationError("The reviewed EPUBCheck 5.3.0 binary is required")
    descriptor, temporary_name = tempfile.mkstemp(suffix=".epub")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(epub_bytes)
        try:
            result = subprocess.run(
                ["java", "-jar", str(jar), "--failonwarnings", str(temporary)],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EpubValidationError("EPUBCheck could not run") from error
        if result.returncode != 0:
            raise EpubValidationError("EPUBCheck rejected the generated Edition")
    finally:
        temporary.unlink(missing_ok=True)
