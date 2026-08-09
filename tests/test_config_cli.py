from __future__ import annotations

import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from epub_news_feeder.config import ConfigError, load_config

RUN_ID = re.compile(r"\b\d{8}T\d{6}Z-[A-Z2-7]{8}\b")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REALITY_CHECK_CONFIG = REPOSITORY_ROOT / "examples" / "reality-check.yaml"
SCHEDULED_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "daily-edition.yml"

# The rights basis SVT's configuration is upgraded to by this ticket: a published Content-Signal
# permission for AI retrieval, rather than bare operator attestation. See issue #57 / #51 / #44.
SVT_CONTENT_SIGNAL_BASIS = "published_content_signal_ai_input_allowed"

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


def test_local_editorial_configuration_accepts_independent_ollama_models(
    tmp_path: Path,
) -> None:
    config = tmp_path / "publication.yaml"
    config.write_text(
        MINIMAL_CONFIG.replace(
            "    sections:",
            """    editorial:
      enabled: true
      provider: ollama
      model_pair:
        editorial_model: gemma4:12b-mlx
        verifier_model: gemma4:e4b-mlx
        editorial_prompt_version: editorial-v1
        verifier_prompt_version: verifier-v1
        schema_version: 1
      capabilities: [article_summary]
      cost_envelope: {max_calls: 4, max_tokens: 12000}
      ollama_host: http://127.0.0.1:11434
    sections:""",
        ),
        encoding="utf-8",
    )

    result = run_cli("validate", "--config", str(config))

    assert result.returncode == 0, result.stderr
    assert RUN_ID.search(result.stdout)
    assert "code=CONFIG_VALID publications=1" in result.stdout
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


@pytest.mark.security
def test_reality_check_configuration_records_the_recorded_rights_matrix() -> None:
    """The four-Source reality-check configuration is proven in the suite, not only by live
    evidence gathered outside it (issue #57, following #51 and #44). A regression in any recorded
    gate — Ars Technica's local LLM eligibility silently returning to ``allow``, or Ekot's page
    acquisition opening up — must fail this test rather than only be discoverable on a live run.
    """
    configuration = load_config(REALITY_CHECK_CONFIG)
    sources = configuration.sources
    assert set(sources) == {"david", "ars", "svt", "ekot"}

    # Every Source carries both a rights policy and dated eligibility evidence — the two gates
    # `application.py` requires before a Source is ever considered for acquisition.
    for source in sources.values():
        assert source.rights is not None
        assert source.eligibility is not None

    # Ars Technica: the Condé Nast clause denies local AND remote LLM use (#51), while the operator
    # policy still records `local_only`. Policy and evidence are deliberately separate gates, and
    # flattening one into the other would lose the distinction the two-gate design exists to keep.
    ars = sources["ars"]
    assert ars.eligibility is not None
    assert ars.eligibility.local_llm == "deny"
    assert ars.eligibility.remote_llm == "deny"
    assert ars.llm_processing == "local_only"

    # Sveriges Radio Ekot: metadata-only acquisition, with page acquisition, retention, and both
    # LLM routes denied, and the operator policy disabled to match.
    ekot = sources["ekot"]
    assert ekot.acquisition == "metadata_only"
    assert ekot.llm_processing == "disabled"
    assert ekot.eligibility is not None
    assert ekot.eligibility.page_acquisition == "deny"
    assert ekot.eligibility.retention == "deny"
    assert ekot.eligibility.local_llm == "deny"
    assert ekot.eligibility.remote_llm == "deny"

    # Remote LLM allowance reaches exactly one Source: the operator's own site, granted by the
    # rightsholder. Every third-party publisher stays denied, conditional or unknown - "silence is
    # not permission" governs a publisher, not a site the operator owns.
    allowed = {
        source_id
        for source_id, source in sources.items()
        if source.eligibility is not None and source.eligibility.remote_llm == "allow"
    }
    assert allowed == {"david"}
    assert sources["david"].rights is not None
    assert sources["david"].rights.basis == "operator_owned_site_attested_private_use"
    assert sources["svt"].eligibility is not None
    assert sources["svt"].eligibility.remote_llm == "conditional"

    # The two gates must agree before anything is sent. `remote_llm` is the publisher's
    # recorded position; `llm_processing` is the operator's policy. Exactly one Source has
    # both, and it is the site the operator owns — every third-party publisher stays at
    # `local_only` or below, whatever their evidence says.
    remote_routed = {
        source_id
        for source_id, source in sources.items()
        if source.llm_processing == "remote_allowed"
    }
    assert remote_routed == {"david"}


@pytest.mark.security
def test_reality_check_svt_basis_cites_its_published_content_signal() -> None:
    """SVT's recorded rights basis is upgraded from bare operator attestation to a citation of the
    Content-Signal SVT itself publishes (``ai-train=no, search=yes, ai-input=yes``, with an
    explicit "ALLOWED: AI retrieval (not training)" section) — a stronger and more honest basis
    (issue #51's SVT follow-on)."""
    configuration = load_config(REALITY_CHECK_CONFIG)
    svt_rights = configuration.sources["svt"].rights
    assert svt_rights is not None
    assert svt_rights.basis == SVT_CONTENT_SIGNAL_BASIS


@pytest.mark.property
def test_reality_check_svt_basis_upgrade_changes_no_gate_or_eligibility_value(
    tmp_path: Path,
) -> None:
    """`rights.basis` is a free-text evidence citation, not a gate. Upgrading SVT's basis to cite
    its published content signal must change no eligibility value and nothing a generated Edition
    depends on. Verified by loading the configuration both ways and diffing every field, rather
    than merely asserting the two values chosen by hand look compatible."""
    upgraded_text = REALITY_CHECK_CONFIG.read_text(encoding="utf-8")
    previous_basis_text = upgraded_text.replace(
        f"basis: {SVT_CONTENT_SIGNAL_BASIS}", "basis: operator_attested_private_use"
    )
    assert previous_basis_text != upgraded_text  # sanity: the substitution changed something

    previous_config_path = tmp_path / "reality-check-previous-basis.yaml"
    previous_config_path.write_text(previous_basis_text, encoding="utf-8")

    upgraded_configuration = load_config(REALITY_CHECK_CONFIG)
    previous_configuration = load_config(previous_config_path)

    upgraded_dump = upgraded_configuration.model_dump(mode="json")
    previous_dump = previous_configuration.model_dump(mode="json")

    assert upgraded_dump["sources"]["svt"]["rights"]["basis"] == SVT_CONTENT_SIGNAL_BASIS
    assert previous_dump["sources"]["svt"]["rights"]["basis"] == "operator_attested_private_use"

    # The only difference the basis edit may introduce is the free-text basis string itself. Patch
    # it back to the previous value and assert every other field of the loaded configuration —
    # every Source's eligibility matrix, every Publication, every Edition input — is unchanged.
    upgraded_dump["sources"]["svt"]["rights"]["basis"] = "operator_attested_private_use"
    assert upgraded_dump == previous_dump


def _scheduled_generate_command() -> list[str]:
    """The generate invocation the scheduled workflow actually runs, as a token list."""

    workflow = yaml.safe_load(SCHEDULED_WORKFLOW.read_text(encoding="utf-8"))
    script: str = next(
        step["run"]
        for step in workflow["jobs"]["edition"]["steps"]
        if "epub-news-feeder generate" in step.get("run", "")
    )
    return script.replace("\\\n", " ").split()


@pytest.mark.security
def test_scheduled_workflow_names_a_publication_that_exists_and_can_run_unattended() -> None:
    """The one run nobody watches must not drift away from the configuration it names.

    Renaming the Publication or moving the configuration file would surface only as a 04:00
    failure. So would enabling the local editorial route: a GitHub-hosted runner has no
    Ollama, so a scheduled Publication that asks for it degrades silently to no summaries
    every morning. Both fail here instead.
    """

    command = _scheduled_generate_command()
    config_path = REPOSITORY_ROOT / command[command.index("--config") + 1]
    publication_id = command[command.index("--publication") + 1]

    configuration = load_config(config_path)
    publication = next(
        (item for item in configuration.publications if item.id == publication_id), None
    )
    assert publication is not None, f"{config_path} defines no Publication {publication_id!r}"
    if publication.editorial is not None and publication.editorial.enabled:
        assert publication.editorial.remote_processing, "a hosted runner has no local model"
        assert publication.editorial.provider in configuration.remote_providers

    # State persistence is what keeps a daily run from re-delivering yesterday's reading. A
    # scheduled run without it is worse than no scheduled run at all.
    assert "--state-environment" in command


REMOTE_EDITORIAL_CONFIG = """
version: 1
remote_providers:
  openai:
    training_opt_in: {training_opt_in}
    store: {store}
    max_abuse_retention_days: 30
    tools: none
sources:
  source-one:
    title: Source one
    feed_url: https://example.com/feed.xml
publications:
  - id: publication-one
    title: Publication one
    editorial:
      enabled: true
      remote_processing: {remote_processing}
      provider: {provider}
      model_pair:
        editorial_model: gpt-5.4-2026-03-05
        verifier_model: gpt-5.4-mini-2026-03-17
        editorial_prompt_version: article-summary-v1
        verifier_prompt_version: evidence-check-v1
        schema_version: 1
      capabilities: [article_summary]
      cost_envelope: {{max_calls: 8, max_tokens: 12000}}
    sections:
      - id: section-one
        title: Section one
        sources: [source-one]
"""


def _remote_editorial_config(
    tmp_path: Path,
    *,
    training_opt_in: str = "false",
    store: str = "false",
    provider: str = "openai",
    remote_processing: str = "true",
) -> Path:
    path = tmp_path / "remote-editorial.yaml"
    path.write_text(
        REMOTE_EDITORIAL_CONFIG.format(
            training_opt_in=training_opt_in,
            store=store,
            provider=provider,
            remote_processing=remote_processing,
        ).lstrip(),
        encoding="utf-8",
    )
    return path


@pytest.mark.security
def test_editorial_preflight_accepts_a_private_remote_provider_profile(tmp_path: Path) -> None:
    configuration = load_config(_remote_editorial_config(tmp_path))
    editorial = configuration.publications[0].editorial

    assert editorial is not None
    assert editorial.remote_processing is True
    assert editorial.provider == "openai"


@pytest.mark.security
@pytest.mark.parametrize(
    ("field", "value"),
    [("training_opt_in", "true"), ("store", "true")],
)
def test_editorial_preflight_refuses_a_provider_profile_that_keeps_the_text(
    tmp_path: Path, field: str, value: str
) -> None:
    """The privacy gate is at load, before a Source is fetched — not at call time.

    A profile that opts in to training, or that stores responses on the provider's side,
    cannot be corrected once Article text has been sent, so the configuration never loads.
    """

    path = _remote_editorial_config(tmp_path, **{field: value})

    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.security
@pytest.mark.parametrize(
    ("provider", "remote_processing"),
    [("ollama", "true"), ("openai", "false")],
)
def test_editorial_refuses_a_route_that_contradicts_its_provider(
    tmp_path: Path, provider: str, remote_processing: str
) -> None:
    """`remote_processing` states which route the provider is, and must agree with it.

    A configuration claiming local processing through a remote provider — or the reverse —
    is a configuration that disagrees with itself about whether Article text leaves the
    machine, which is the one ambiguity that must never load.
    """

    path = _remote_editorial_config(
        tmp_path, provider=provider, remote_processing=remote_processing
    )

    with pytest.raises(ConfigError):
        load_config(path)
