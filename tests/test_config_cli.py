from __future__ import annotations

import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

RUN_ID = re.compile(r"\b\d{8}T\d{6}Z-[A-Z2-7]{8}\b")

MINIMAL_CONFIG = """
version: 1
sources:
  source-one:
    title: Source one
    feed_url: https://example.com/feed.xml
publications:
  - id: publication-one
    title: Publication one
    policies:
      coverage:
        type: coverage
    sections:
      - id: section-one
        title: Section one
        policy: coverage
        sources:
          - source-one
""".lstrip()


def run_cli(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        ["epub-news-feeder", *arguments],
        check=False,
        capture_output=True,
        env=process_environment,
        text=True,
    )


@pytest.mark.acceptance
def test_ticket_03_validates_complete_nested_configuration(tmp_path: Path) -> None:
    config = tmp_path / "publication.yaml"
    config.write_text(
        """
version: 1
remote_providers:
  openai-editorial:
    training_opt_in: false
    store: false
    application_state_retention_days: 30
    max_abuse_retention_days: 30
    region: eu
    tools: none
    subprocessors: []
sources:
  david:
    title: David Lowe Larsson
    feed_url: https://davidlowelarsson.com/rss.xml
    acquisition: feed
    weight: 5
    llm_processing: local_only
    rights:
      basis: operator_attested_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: david-20260809
publications:
  - id: daily
    title: ${PUBLICATION_TITLE}
    policies:
      general:
        type: coverage
      technology:
        type: interest
    budget:
      max_articles: 12
      min_articles: 3
      weight: 5
    editorial:
      enabled: false
      influence: none
      remote_processing: false
      provider: openai-editorial
      model_pair:
        editorial_model: gpt-editor
        verifier_model: gpt-verifier
        editorial_prompt_version: editorial-v1
        verifier_prompt_version: verifier-v1
        schema_version: 1
      cost_envelope:
        max_calls: 4
        max_tokens: 40000
        max_cost: 0.25
    sections:
      - id: home
        title: Home
        policy: general
        sections:
          - id: technology
            title: Technology
            policy: technology
            budget:
              max_articles: 6
            sources:
              - david
  - id: weekly
    title: Weekly
    sections:
      - id: all-news
        title: All news
        sources:
          - david
""".lstrip(),
        encoding="utf-8",
    )

    result = run_cli(
        "validate",
        "--config",
        str(config),
        environment={"PUBLICATION_TITLE": "Private Daily"},
    )

    assert result.returncode == 0
    assert RUN_ID.search(result.stdout)
    assert "code=CONFIG_VALID publications=2" in result.stdout
    assert result.stderr == ""


@pytest.mark.acceptance
@pytest.mark.parametrize(
    "invalid_config",
    [
        MINIMAL_CONFIG.replace("version: 1", "version: 2"),
        MINIMAL_CONFIG.replace("version: 1", "version: 1\nunknown: do-not-echo"),
        MINIMAL_CONFIG.replace(
            "sources:\n  source-one:",
            "sources:\n  source-one:\n    title: First\n    feed_url: https://example.com/one\n"
            "  source-one:",
        ),
        MINIMAL_CONFIG.replace(
            "  - id: publication-one",
            "  - id: publication-one\n    title: Duplicate\n    sections: []\n"
            "  - id: publication-one",
        ),
        MINIMAL_CONFIG.replace(
            "        sources:\n          - source-one",
            "        sections:\n"
            "          - id: section-one\n"
            "            title: Duplicate nested ID",
        ),
        MINIMAL_CONFIG.replace("          - source-one", "          - missing-source"),
        MINIMAL_CONFIG.replace("        policy: coverage", "        policy: missing-policy"),
        MINIMAL_CONFIG.replace(
            "        sources:\n          - source-one",
            "        sources:\n          - source-one\n"
            "        sections:\n          - id: child\n            title: Child",
        ),
        MINIMAL_CONFIG.replace(
            "    feed_url: https://example.com/feed.xml",
            "    feed_url: https://example.com/feed.xml\n    acquisition: scrape",
        ),
    ],
    ids=[
        "unsupported-version",
        "unknown-key",
        "duplicate-yaml-key",
        "duplicate-publication-id",
        "duplicate-section-id",
        "unknown-source-reference",
        "unknown-policy-reference",
        "sources-on-non-leaf",
        "invalid-source-value",
    ],
)
def test_ticket_03_rejects_invalid_configuration_without_echoing_values(
    tmp_path: Path, invalid_config: str
) -> None:
    config = tmp_path / "do-not-echo.yaml"
    config.write_text(invalid_config, encoding="utf-8")

    result = run_cli("validate", "--config", str(config))

    assert result.returncode == 2
    assert RUN_ID.search(result.stderr)
    assert "code=CONFIG_INVALID message=Configuration is invalid" in result.stderr
    assert "do-not-echo" not in result.stderr
    assert result.stdout == ""


@pytest.mark.acceptance
def test_ticket_03_resolves_only_complete_environment_placeholders(tmp_path: Path) -> None:
    config = tmp_path / "publication.yaml"
    config.write_text(
        MINIMAL_CONFIG.replace("https://example.com/feed.xml", "${PRIVATE_FEED_URL}"),
        encoding="utf-8",
    )

    valid = run_cli(
        "validate",
        "--config",
        str(config),
        environment={"PRIVATE_FEED_URL": "https://secret.example/feed.xml"},
    )
    missing = run_cli("validate", "--config", str(config))

    assert valid.returncode == 0
    assert "CONFIG_VALID" in valid.stdout
    assert "secret.example" not in valid.stdout
    assert missing.returncode == 2
    assert "CONFIG_ENV_MISSING" in missing.stderr
    assert "PRIVATE_FEED_URL" not in missing.stderr


class _CountingHandler(BaseHTTPRequestHandler):
    request_count = 0

    def do_GET(self) -> None:
        type(self).request_count += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.mark.acceptance
def test_ticket_03_invalid_generation_has_no_network_or_filesystem_side_effects(
    tmp_path: Path,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    _CountingHandler.request_count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = "127.0.0.1"
        port = server.server_port
        config = tmp_path / "publication.yaml"
        invalid_config = MINIMAL_CONFIG.replace(
            "https://example.com/feed.xml", f"http://{host}:{port}/feed.xml"
        ).replace("version: 1", "version: 1\nunknown: invalid")
        config.write_text(invalid_config, encoding="utf-8")
        state = tmp_path / "state.sqlite3"
        output = tmp_path / "output"

        result = run_cli(
            "generate",
            "--config",
            str(config),
            "--state",
            str(state),
            "--output",
            str(output),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result.returncode == 2
    assert "CONFIG_INVALID" in result.stderr
    assert _CountingHandler.request_count == 0
    assert not state.exists()
    assert not output.exists()
