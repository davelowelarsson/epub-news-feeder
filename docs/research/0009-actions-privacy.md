# Research 0009: private scheduled operation in one public repository

Status: recommendation for [issue #9](https://github.com/davelowelarsson/epub-news-feeder/issues/9)

## Decision summary

Keep source, workflow, and a nonsecret Publication configuration in the public
repository. Run the real operation only from a dedicated production job on the
default branch. Give that job individual Actions secrets or, preferably, a
short-lived OIDC identity. Generate on an ephemeral GitHub-hosted runner and
send the Edition plus durable state directly to an external private store.

Nothing private may be committed, cached, uploaded as an Actions artifact or
release, written to a job summary, or printed in logs. GitHub retains only
sanitized status, aggregate counts, and the portable Run ID.

This resolves the Actions privacy boundary. It does **not** select the final
configuration schema, state schema/backend, Google Drive identity, or diagnostic
schema; those remain blocked on issues
[#4](https://github.com/davelowelarsson/epub-news-feeder/issues/4),
[#5](https://github.com/davelowelarsson/epub-news-feeder/issues/5), and
[#11](https://github.com/davelowelarsson/epub-news-feeder/issues/11).
Google Drive ownership and Kobo delivery remain a decision for
[#10](https://github.com/davelowelarsson/epub-news-feeder/issues/10).

## Scope and assumptions

This report answers the one-public-repository question. “Private” includes the
Edition full text, operator interests and feedback, source or article history,
delivery identifiers, credentials, private diagnostics, and any metadata from
which those values can reasonably be inferred.

The operator controls the repository and destination account. Contributors and
fork owners are not trusted with the operator's credentials or private output.
GitHub, the selected cloud provider, and actions pinned by the workflow are
trusted processors. A repository maintainer allowed to merge executable code is
necessarily trusted: merged code can use production credentials.

## Threat model

| Threat | Consequence | Required control |
| --- | --- | --- |
| Public reader inspects Actions | Edition, interests, URLs, state, or diagnostics leak through logs, summaries, artifacts, caches, releases, or commits | Treat every GitHub-hosted output surface as public; external private storage only |
| Fork or pull request runs attacker-controlled code | Credential or state exfiltration | Separate unprivileged PR CI from production; never expose production secrets, environment, or OIDC to PR jobs; do not use pull_request_target to execute PR code |
| Malicious or compromised dependency/action | Reads job secrets or changes generated output | Minimal dependencies, full-SHA action pins, lockfile/hash verification, least-privilege credentials |
| Accidental logging | Secret redaction misses transformed credentials, content, filenames, URLs, or structured data | Safe logger with allowlisted fields; no shell tracing; individual secrets; mask derived tokens before use; test logs on success and failure |
| Overlapping or retried runs | Duplicate Edition or corrupted state | Workflow concurrency plus backend idempotency/compare-and-swap; stable Run ID |
| Compromised maintainer/default branch | Production credential use | Branch protection, review of workflow/dependency/generator changes, narrow and revocable destination permissions |
| Stale fork or inactive public repository | Expected schedule silently stops or runs vulnerable code | Fork enablement checklist, default-branch updates, monitoring, manual dispatch recovery; pin updates reviewed regularly |
| Broad cloud trust | Another repository, fork, branch, or PR obtains an access token | OIDC trust constrained to exact repository identity and production branch/environment; service account scoped to the destination |

Residual risk: GitHub-hosted runner code that can generate the Edition can read
the Edition in memory and on temporary disk. The architecture limits persistence
and visibility; it cannot make malicious production code safe.

## Facts established from official documentation

### Scheduling and forks

- A schedule workflow runs the latest commit on the default branch, and the
  workflow file must exist there. Runs can be delayed under high load. The
  minimum interval is five minutes. [GitHub: events that trigger workflows][1]
- Scheduled workflows in an inactive public repository are automatically
  disabled after 60 days. When a public repository is forked, scheduled
  workflows are disabled by default and must be enabled by the fork owner.
  [GitHub: disabling and enabling workflows][2]
- Workflow files outside the default branch do not receive schedule events.
  [GitHub: troubleshooting workflows][3]
- Fork pull request workflows do not receive Actions secrets. Their
  GITHUB_TOKEN is read-only, and public-repository policy can require maintainer
  approval. Approval permits the untrusted workflow to run; it is not a reason
  to give that workflow production credentials. [GitHub: secret types][4]
  [GitHub: repository Actions settings][5]

### Permissions and credentials

- The permissions key can set every unspecified GITHUB_TOKEN permission to
  none; an empty permissions map disables all of them. Fork PR permissions are
  normally reduced to read-only. [GitHub: workflow syntax][6]
- Repository, organization, and environment secrets are injected only when a
  workflow explicitly references them. Environment secrets are available only
  to jobs that reference that environment and only after its protection rules
  pass. [GitHub: secrets][7] [GitHub: environments][8]
- A missing secret expression evaluates to an empty string. Secrets cannot be
  referenced directly in an if conditional. [GitHub: using secrets][9]
- GitHub secret redaction is not guaranteed for transformed values. GitHub
  specifically advises separate values rather than structured JSON/XML/YAML
  secrets, masking generated sensitive values, least privilege, log review, and
  rotation after exposure. A collaborator with write access can use repository
  secrets through workflows. [GitHub: secure use][10]
- OIDC lets a job exchange its GitHub identity for a short-lived cloud token
  instead of storing a long-lived cloud key. Requesting it requires id-token:
  write; the cloud trust policy must constrain claims such as repository,
  branch, or environment. [GitHub: OIDC][11] [GitHub: OIDC reference][12]

### Public persistence surfaces

- Workflow information and logs for a public repository can be viewed by any
  signed-in GitHub user with read access. Secret masking is not a content
  privacy boundary. [GitHub: workflow logs][13]
- Anyone with read access can download workflow artifacts, and the public
  repository artifact API can be used without authentication. Logs and
  artifacts default to 90-day retention. [GitHub: downloading artifacts][14]
  [GitHub: artifact API][15]
- Actions caches are dependency acceleration, not durable application storage.
  Fork pull requests can restore default-branch caches; caches must contain no
  secrets or sensitive data. They can be evicted after seven days without
  access, and cache contents are not signed or verified. [GitHub: dependency
  caching][16]
- Except for the single-CPU container option, every GitHub-hosted job receives
  a new virtual machine. The runner filesystem is therefore working storage,
  not cross-run state. [GitHub: hosted runners][17]
- Full-length commit SHA pinning is the only immutable way to reference an
  action. GitHub can enforce this repository-wide. [GitHub: secure use][10]
- Concurrency groups limit a group to one running workflow/job. By default only
  one additional run is pending; a newer pending run replaces the prior pending
  run. cancel-in-progress can also cancel the running job. Ordering is not
  guaranteed. [GitHub: concurrency][18]

### Google-specific facts relevant to the likely publisher

- Google Cloud Workload Identity Federation can accept GitHub's OIDC token and
  restrict identities using mapped attributes and conditions. Google warns
  against name-only trust because repository or organization names can be
  reclaimed, and recommends numeric IDs where available. [Google Cloud:
  deployment pipelines][19]
- Google recommends the narrow Drive file scope where it fits. Long-term user
  access otherwise requires a refresh token stored securely. [Google Drive:
  scopes][20]
- Google's shared-drive guide says service accounts have no storage quota and
  cannot own files; they must upload into a shared drive or use OAuth to act on
  behalf of a user. The exact ownership/account arrangement must be resolved
  before choosing OIDC service-account authentication for Drive.
  [Google Drive: shared drives][21]

## Recommendations

### 1. Public configuration, private values

Commit one example/default Publication configuration and allow the operator to
commit their real **nonsecret** structure. The schema from issue #4 should allow
symbolic environment references, not credential values:

    delivery:
      adapter: google_drive
      folder_id_env: EPUB_NEWS_DRIVE_FOLDER_ID
      credential_mode_env: EPUB_NEWS_GOOGLE_AUTH_MODE
    state:
      adapter_env: EPUB_NEWS_STATE_ADAPTER
      location_env: EPUB_NEWS_STATE_LOCATION

This shape is illustrative, not a schema decision. Any sensitive destination
identifier belongs in a secret even if the provider does not classify the ID as
a credential. Section labels, feedback, or interests that the operator considers
private likewise cannot appear in tracked YAML; simple values can use
placeholders, while complex private configuration belongs in the external
private store selected with issue #5. Non-sensitive, fork-specific switches may
be Actions variables. GitHub warns that variables render unmasked, so they must
never carry private values. [GitHub: variables][22]

Use one secret per sensitive value. For the OAuth fallback this is likely a
client ID, client secret, refresh token, and private destination ID as separate
secrets. Do not put a service-account JSON document into one secret merely for
convenience. Prefer OIDC so no cloud private key is stored at all.

The program should fail closed during preflight when a required environment
value is absent. It must print only the missing **variable name**, never its
value. Map secrets into step/job environment variables rather than command-line
arguments.

### 2. Two workflow trust domains

Keep two separate workflow files:

1. **PR CI** runs tests and validates example/config shape. It has no production
   environment, no id-token permission, no Actions secrets, no writable
   GITHUB_TOKEN, and no private network/service access. It may process only
   checked-in fixtures.
2. **Private scheduled operation** is triggered only by schedule and
   workflow_dispatch, runs only on the repository default branch, references a
   dedicated environment such as private-operation, and obtains credentials
   only inside the publish job.

Do not combine these paths behind a conditional in one job. Do not use
pull_request_target for generation or run code checked out from a pull request
in a privileged job. Manual dispatch must reject a non-default ref before
requesting credentials.

Start the production workflow with permissions: {}. Grant the production job
only contents: read and, when OIDC is selected, id-token: write. No contents:
write, actions: write, pull-requests: write, or artifact permission is needed.
Use a GitHub-hosted runner; GitHub advises against self-hosted runners for public
repositories because fork code can threaten the host. [GitHub: self-hosted
runners][23]

### 3. Proposed production workflow boundary

The tracked workflow should remain a thin, auditable adapter:

    on:
      schedule: weekly, away from the top of the hour
      workflow_dispatch:
    permissions: {}
    concurrency:
      group: private-publication-production
      cancel-in-progress: false

    production job:
      require default branch and PRIVATE_OPERATION_ENABLED=true
      environment: private-operation
      permissions:
        contents: read
        id-token: write  # only for OIDC mode
      steps:
        checkout pinned full SHA
        install locked dependencies
        validate required names without printing values
        authenticate using pinned action or in-repository adapter
        load private state from external store
        generate into a runner-only directory with safe logging
        validate the Edition locally
        publish Edition to private destination under stable Run ID
        commit private state using version/compare-and-swap
        emit sanitized status and Run ID

The workflow must not call upload-artifact on production paths, cache state or
content, create a release, commit generated files/state, or echo output paths
containing private names. Cache only package-manager downloads keyed by a
reviewed lockfile; treat restored cache data as untrusted. If caching offers
little benefit, omit it.

Set timeout-minutes. Do not cancel an in-progress stateful run: cancellation can
occur between Edition upload and state commit. GitHub concurrency protects
against normal schedule/manual overlap, but the external adapter still needs an
idempotent Edition key and optimistic concurrency or a lease. A rerun with the
same portable Run ID must converge rather than publish another Edition. The
exact Run ID and transaction semantics depend on issues #5 and #11.

### 4. Private output and durable state

| Location | Private in this public repository? | Durable/authoritative? | Decision |
| --- | --- | --- | --- |
| Runner filesystem | Not publicly browsable, but job code can read it | No; job-scoped | Temporary generation only |
| Git branch/commit/release | No | Durable but public | Forbidden |
| Actions artifact or job summary | No | Retention-limited | Forbidden for Editions, state, and private diagnostics |
| Actions cache | No; fork PR workflows can read reachable caches | Evictable and not integrity-protected | Dependencies only |
| Actions secret | Encrypted configuration value | Size-limited, not a mutable state store | Individual credentials/IDs only |
| External private store | Yes, subject to its ACL and credential design | Can be authoritative | Required |

Issue #5 must select an external backend capable of preserving Article identity,
Edition history, source health, and deduplication state. It can be the delivery
provider only if it offers safe versioning/atomic update semantics; otherwise
use a private object/database store for state and Drive only for Editions.
GitHub Actions is an orchestrator, not the system of record.

On success, publish the Edition privately first, then commit the corresponding
state with a version check. Record enough private transaction metadata to
reconcile a crash between those steps. On failure, write full diagnostics only
to the private store and show GitHub a category, aggregate counts, and Run ID.
Issue #11 decides the final diagnostic fields.

### 5. Authentication choice

Prefer OIDC plus a dedicated service account when the backend and issue #10's
Drive ownership model support it:

- Enable id-token: write only on the production job.
- Constrain cloud trust to this repository's immutable/numeric identity and the
  private-operation environment or default branch; reject pull_request events
  and other repositories/forks.
- Give the service account access only to the state container and Edition
  destination, with no administrative permissions.
- Pin the authentication action to a verified full commit SHA.

If a personal Google Drive arrangement requires user OAuth, use the narrowest
Drive scope that meets issue #10, store the refresh token and client values as
individual environment secrets, and document revocation/rotation. This is a
long-lived-credential fallback, not equivalent to OIDC.

### 6. Logs and diagnostics

Production logging must use an allowlist, not attempted redaction after the
fact. Allowed public fields: Run ID, phase name, success/failure category,
duration, and aggregate counts that issue #11 explicitly declares non-sensitive.
Disallowed fields include Article titles/text/URLs, Section labels if
operator-specific, configuration values, destination IDs, state rows, request
or response bodies, exception locals, authentication output, and generated
filenames derived from Publication data.

Disable shell tracing. Mask every generated token before any command can print
it. Configure HTTP clients not to log headers/bodies. Test failure paths, because
exceptions and subprocess output are common leaks. If exposure occurs, delete
the run/log and rotate or revoke the credential; deletion does not undo prior
disclosure.

### 7. Supply-chain and branch controls

- Pin every nonlocal action and reusable workflow to a reviewed full commit SHA;
  retain a version comment for updates. Enable the repository policy requiring
  SHA pins if practical.
- Keep runtime dependencies locked with hashes and update through reviewed PRs.
- Require review for changes to the production workflow, dependency files,
  publication code, and logging/publisher adapters. A CODEOWNERS rule is useful
  only when branch protection also requires code-owner review.
- Require approval for all external-contributor workflows to limit abuse, while
  remembering that approval does not make PR code trusted.
- Never run production from a contributor branch, tag, or fork using upstream
  credentials.

## Fork operator setup

Document these steps next to the eventual workflow. A new fork is inert until
the operator completes them:

1. Fork the repository and review the current default-branch workflow and pinned
   action SHAs.
2. Keep the fork's default branch current. Configure branch protection/review
   appropriate to the operator's collaborator model.
3. Create the private destination and state backend. Decide the Google identity
   and ownership model through issue #10; decide state semantics through #5.
4. Create the dedicated service account and OIDC federation with exact
   repository/default-branch or environment trust, **or** create narrowly scoped
   OAuth credentials. Grant only destination/state access.
5. Create the private-operation GitHub environment. Add individual secrets and
   non-sensitive variables by the names documented in the sample configuration.
   Do not copy values into tracked YAML.
6. Leave required reviewers off the environment if unattended schedules must
   run. Restrict its deployment branches to the default branch. Set
   PRIVATE_OPERATION_ENABLED only after configuration is complete.
7. Enable Actions and explicitly enable the scheduled workflow in the fork; fork
   schedules start disabled. Run workflow_dispatch on the default branch.
8. Inspect both successful and deliberately failed logs for leakage. Verify no
   artifacts/releases/commits were created, the Edition is private at the
   destination, state survives a second run, and a rerun is idempotent.
9. Enable the schedule. Monitor missed/failed runs externally or via sanitized
   GitHub notification, remembering public schedules can disable after 60 days
   without repository activity.
10. Record credential rotation/revocation, action-pin update, state backup, and
    incident procedures. Re-test after workflow, logger, auth, or publisher
    changes.

## Acceptance checks for implementation

The future implementation should not be considered safe until all are true:

- A fork PR test proves production secrets and id-token are unavailable.
- Manual dispatch on a non-default ref stops before authentication.
- Source inspection finds no upload-artifact, release, state-cache, or git-write
  path in the production job.
- Success, invalid-config, network-failure, auth-failure, and generation-failure
  logs contain only the approved public diagnostic schema.
- A first run and rerun demonstrate private Edition ACLs, preserved state, and
  idempotency.
- Two overlapping dispatches demonstrate serialization and no state loss.
- Every external action reference is a full commit SHA.
- Removing one required secret causes a named, value-free preflight failure.

## Official sources

[1]: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
[2]: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows
[3]: https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#troubleshooting-workflow-triggers
[4]: https://docs.github.com/en/code-security/reference/secret-security/secret-types#actions-secrets
[5]: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository
[6]: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions
[7]: https://docs.github.com/en/actions/concepts/security/secrets
[8]: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
[9]: https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
[10]: https://docs.github.com/en/actions/reference/security/secure-use
[11]: https://docs.github.com/en/actions/concepts/security/openid-connect
[12]: https://docs.github.com/en/actions/reference/security/oidc
[13]: https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs
[14]: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts
[15]: https://docs.github.com/en/rest/actions/artifacts
[16]: https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching
[17]: https://docs.github.com/en/actions/concepts/runners/github-hosted-runners
[18]: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
[19]: https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
[20]: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
[21]: https://developers.google.com/workspace/drive/api/guides/about-shareddrives
[22]: https://docs.github.com/en/actions/concepts/workflows-and-actions/variables
[23]: https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners
