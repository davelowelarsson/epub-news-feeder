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
WEEKLY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "weekly-edition.yml"
SCHEDULED_WORKFLOWS = (SCHEDULED_WORKFLOW, WEEKLY_WORKFLOW)

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
    # The original four keep their recorded gates whatever else joins them (issue #73's Wave 1).
    assert {"david", "ars", "svt", "ekot"} <= set(sources)

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
    # Remote allowance is not a list to append to — it is a consequence of the recorded basis.
    # Assert the *reason* rather than the names, so adding a Source can only widen remote
    # eligibility by citing one of these bases, and never by being added to an allowlist.
    permissive_bases = {
        "operator_owned_site_attested_private_use",
        "united_states_government_work_public_domain",
        "published_creative_commons_attribution_4_0",
    }
    for source_id in allowed:
        rights = sources[source_id].rights
        assert rights is not None, source_id
        assert rights.basis in permissive_bases, (
            f"{source_id} allows remote processing without a rightsholder grant or a "
            f"permissive licence: {rights.basis}"
        )
    assert "david" in allowed
    assert sources["david"].rights is not None
    assert sources["david"].rights.basis == "operator_owned_site_attested_private_use"
    assert sources["svt"].eligibility is not None
    assert sources["svt"].eligibility.remote_llm == "conditional"

    # The operator's rule of 2026-08-09: a publisher blocking AI crawlers has stated an AI
    # position, not a reproduction position. Such a Source is reproduced in full with both
    # LLM limbs denied — so no Source may ever deny reproduction while permitting a model.
    for source_id, source in sources.items():
        evidence = source.eligibility
        assert evidence is not None, source_id
        if evidence.page_acquisition == "deny":
            assert evidence.local_llm != "allow", source_id
            assert evidence.remote_llm != "allow", source_id

    # The two gates must agree before anything is sent. `remote_llm` is the publisher's
    # recorded position; `llm_processing` is the operator's policy. Exactly one Source has
    # both, and it is the site the operator owns — every third-party publisher stays at
    # `local_only` or below, whatever their evidence says.
    remote_routed = {
        source_id
        for source_id, source in sources.items()
        if source.llm_processing == "remote_allowed"
    }
    # The operator policy limb may never reach further than the publisher limb, and both are
    # held to the same short list of bases. A Source cannot be routed remotely by policy while
    # its evidence withholds permission.
    assert remote_routed <= allowed
    for source_id in remote_routed:
        rights = sources[source_id].rights
        assert rights is not None and rights.basis in permissive_bases, source_id
    assert "david" in remote_routed


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
    # Several SVT Sources now share the basis, so every one of them is patched back, not just the
    # first: the point of the test is that basis is inert, whichever Sources cite it.
    patched = 0
    for source in upgraded_dump["sources"].values():
        if source["rights"] and source["rights"]["basis"] == SVT_CONTENT_SIGNAL_BASIS:
            source["rights"]["basis"] = "operator_attested_private_use"
            patched += 1
    assert patched >= 1
    assert upgraded_dump == previous_dump


def _scheduled_generate_command(workflow_path: Path = SCHEDULED_WORKFLOW) -> list[str]:
    """The generate invocation a scheduled workflow actually runs, as a token list."""

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    script: str = next(
        step["run"]
        for step in workflow["jobs"]["edition"]["steps"]
        if "epub-news-feeder generate" in step.get("run", "")
    )
    return script.replace("\\\n", " ").split()


@pytest.mark.security
@pytest.mark.parametrize("workflow_path", SCHEDULED_WORKFLOWS, ids=lambda path: path.stem)
def test_scheduled_workflow_names_a_publication_that_exists_and_can_run_unattended(
    workflow_path: Path,
) -> None:
    """No run nobody watches may drift away from the configuration it names.

    Renaming the Publication or moving the configuration file would surface only as a dawn
    failure. So would enabling the local editorial route: a GitHub-hosted runner has no
    Ollama, so a scheduled Publication that asks for it degrades silently to no summaries
    every morning. Both fail here instead.

    Parametrised over every scheduled Edition rather than written once for the daily, so that
    adding a third does not quietly leave it unguarded.
    """

    command = _scheduled_generate_command(workflow_path)
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


@pytest.mark.parametrize("workflow_path", SCHEDULED_WORKFLOWS, ids=lambda path: path.stem)
def test_scheduled_workflow_avoids_the_queue_that_delayed_delivery(workflow_path: Path) -> None:
    """Delivery has to beat 07:00 Stockholm, and the schedule is the only reason it ever did not.

    Scheduled at "0 4" the daily spent 67 to 96 minutes in GitHub's queue and delivered at 07:11,
    07:29 and 07:41, against a job finishing in under five minutes. GitHub documents the start of
    every hour as its own worst case. So: never the top of the hour, and early enough that the
    worst delay actually observed still lands before the deadline - 05:00 UTC, which is 07:00 in
    Stockholm through the summer, the tighter half of the DST year.
    """

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # `on` is parsed as the boolean True by YAML 1.1, which is why this is not workflow["on"].
    schedules = workflow[True]["schedule"]
    assert schedules, f"{workflow_path.name} defines no schedule"

    # 96.4 minutes is the worst queue this repository has actually been dealt; 97 rounds it up.
    # The job's own runtime counts too - the deadline is delivery, not the start of the attempt.
    worst_observed_queue_minutes = 97
    observed_job_minutes = 5
    deadline_minutes = 5 * 60

    for entry in schedules:
        minute, hour = (int(field) for field in entry["cron"].split()[:2])
        assert minute != 0, "the top of the hour is GitHub's documented worst case"
        start_minutes = hour * 60 + minute
        delivered_by = start_minutes + worst_observed_queue_minutes + observed_job_minutes
        assert delivered_by <= deadline_minutes, (
            f"{entry['cron']!r} would deliver {delivered_by - deadline_minutes} minutes after "
            f"05:00 UTC if it met the {worst_observed_queue_minutes} minute queue already "
            f"observed, missing 07:00 Stockholm"
        )


def test_the_weekly_completes_the_week_instead_of_reprinting_it() -> None:
    """The weekly's whole identity is a configuration reference, so assert it rather than trust it.

    Without `reads_history_from` the Saturday Edition is a sixth daily built from the same feeds:
    per-Publication suppression means it would carry precisely the reading the weekdays already
    delivered. The reference is mutual: the first delivered week showed Monday through Wednesday
    reprinting what Saturday had carried, because the daily had no idea it was delivered. One
    reader reads both Editions, so each suppresses against the other.
    """

    configuration = load_config(REALITY_CHECK_CONFIG)
    publications = {publication.id: publication for publication in configuration.publications}
    weekly = publications["weekly"]
    daily = publications["daily"]

    assert weekly.reads_history_from == ["daily"]
    assert daily.reads_history_from == ["weekly"], "one reader reads both Editions"

    # A weekly at the daily's Budget would report the week in fifteen Articles it is forbidden
    # from carrying. Whatever the numbers become, the weekly's has to be the larger.
    assert weekly.budget is not None and daily.budget is not None
    weekly_max, daily_max = weekly.budget.max_articles, daily.budget.max_articles
    assert weekly_max is not None and daily_max is not None
    assert weekly_max > daily_max
    assert weekly.max_briefs > daily.max_briefs

    # Same Sources, per the decision this Publication was built to. A weekly quietly reading
    # different feeds would be a different Edition wearing this one's name.
    def sources(publication: object) -> set[str]:
        collected: set[str] = set()
        sections = getattr(publication, "sections", [])
        for section in sections:
            collected.update(section.sources)
            collected.update(sources(section))
        return collected

    assert sources(weekly) == sources(daily)


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
