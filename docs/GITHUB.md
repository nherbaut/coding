# GitHub studies

GitHub integration creates **new studies only**. Existing studies cannot be attached. The server imports UTF-8 documents at a fixed commit and automatically publishes submitted coding and shared codebook revisions to the configured branch. Existing ordinary studies continue to work independently.

## Register the GitHub App

An administrator of the GitHub account or organization must register and install a GitHub App. For this instance configure:

- Homepage: `https://coding.home.nextnet.top/`
- User authorization callback: `https://coding.home.nextnet.top/admin/github/callback/`
- Enable expiring user tokens. The site asks for authorization again when the setup token expires; background publishing uses separately generated installation tokens.
- Repository permissions: **Contents: read and write**, **Metadata: read**. No account permissions or pull request permissions are needed.
- Disable **Active** under Webhooks. No inbound webhook or public webhook endpoint is required.
- Install the App only on the repositories that should be available to Coding. The person creating a study must have repository write access themselves; installation access alone does not grant that permission.
- Generate a private key and keep the PEM file outside the source checkout and Docker build context. Do not commit it.

On application server `.61`, create `/home/nherbaut/server/coding-github-secrets/`, restricted to the server owner, and place the PEM at `app.pem` with mode `600`. The web and GitHub worker containers mount that directory read-only at `/run/coding-github`.

Add these settings to `/home/nherbaut/server/coding.env` (mode `600`):

```dotenv
GITHUB_APP_ID=<numeric App ID>
GITHUB_CLIENT_ID=<Client ID>
GITHUB_CLIENT_SECRET=<Client secret>
GITHUB_APP_SLUG=<App URL slug>
GITHUB_PRIVATE_KEY_FILE=/run/coding-github/app.pem
```

Recreate `coding` and `coding-github-sync` after changing their environment. Local Compose uses `github-secrets/` and the same container mount path; this directory is excluded from Git and Docker builds. The App credentials stay server-side. The short-lived user authorization token is encrypted in the application session and is never exported.

The callback is under `/admin/`, so the existing Traefik/Authelia routing already protects it. Returning from GitHub must identify the same application admin who started authorization. Log out and log back in if you changed LDAP/GitHub accounts during setup.

## Prepare the initial repository

Initial preview and import can take several minutes for a large corpus. The web process uses a 300-second request timeout; GitHub API rate limits are reported without partially creating a study.

The branch must already exist. Coding never creates an empty repository or modifies branch protection. Allow the App to push to the selected branch through the repository's rules, or choose another branch.

Minimal layout:

```text
codebook/
  codebook.yaml
corpus/
  Project A/
    POLICY.md
  Project B/
    README.txt
```

The codebook uses the application's YAML format (`version: 1`, UUID concepts in `codes`). A starting empty book is `version: 1` with `codes: []`. Texts may contain Markdown. Binary files, symlinks, submodules and NUL bytes are rejected. The combined data import limit is 100 MB. Texts retain their original UTF-8 content and line endings, including CRLF; annotation offsets are UTF-16 into that source text.

Each first-level corpus directory is a case. Nested document paths are supported. Existing `.coding` directories are metadata and are not imported as documents or annotations. A document's final extension is replaced with `.coding`; extensionless documents receive that suffix. `policy.md` and `policy.txt` in the same directory therefore collide and must be renamed before import.

An optional `codebook/study.yaml` supplies descriptive metadata:

```yaml
version: 1
study:
  description: Empirical study
cases:
  - key: Project A
    name: Display name for Project A
    metadata:
      url: https://example.org/project-a
documents:
  - id: project-a-policy
    path: corpus/Project A/POLICY.md
    name: AI policy
    metadata:
      source: https://example.org/project-a/POLICY.md
```

Case keys are the original directory names. Document paths must exist, IDs must be unique, and optional `hash` fields use SHA-256 of the exact UTF-8 content. Corpus references in codebooks use `document: corpus/Project A/POLICY.md`, the exact `hash`, `start`, `end` and `quote`. The importer resolves those portable references to internal document versions; exports convert them back to paths. Bibliographic references, quoted memos, researcher rationale and unedited provenance fields are preserved.

The exported manifest also records the current research questions/guidelines under `instructions.content`; these can be supplied in the initial manifest. Guideline `questions` refer to the IDs in `research_questions`.

Use **My studies → Create a study from GitHub → Connect GitHub account**, choose an installed repository and branch, inspect the preview, then create the study. A branch change after preview requires a fresh preview. A repository/branch pair can belong to only one study. The corpus and its metadata are frozen after creation, including imports and case/text edits; coding, instructions and shared codebooks remain editable.

## Export format

```text
codebook/
  study.yaml
  codebook-base.yaml
  codebook.yaml
  history.yaml
  diffs/<UTC timestamp>--<sequence>/change.yaml
  coders/<session UUID>/<personal book hash>.yaml
corpus/<case>/policy.md
corpus/<case>/policy.coding/
  document.yaml
  <session UUID>/
    coder.yaml
    status.yaml
    submissions/<submission UUID>.yaml
    reviews/<review UUID>.yaml
```

Each submitted document stores its selected passages, UUID codes, annotation memos, exact UTF-16 offsets, document hash, personal codebook reference and instructions snapshot. Personal codebooks are content-addressed snapshots, independent of the shared codebook. Unsubmitted annotations and personal edits are not exported. Admin coding sessions are exported like other real coding sessions. Coder identity uses the session UUID and display name, never the email or invitation token.

Submission payloads are immutable. `status.yaml` marks active, withdrawn, superseded and archived work; `reviews` retains decisions and accepted concept mappings. Withdrawals and deletions in the UI never remove historical repository snapshots. Identifiers and document references in generated metadata do not depend on database integer primary keys.

`codebook-base.yaml` is immutable. If absent on first import with no history, it is initialized from the supplied current codebook. Every subsequent shared publication (graph, YAML or proposal review) appends a timestamped change directory. `history.yaml` explicitly orders the changes. Each `change.yaml` records sequence, source, timestamp, revision, before/after canonical SHA-256 hashes and RFC 6902 JSON Patch operations represented as YAML. Concept list order and all provenance metadata participate in the hashes. YAML formatting and comments do not.

An existing history must already reconstruct the supplied current codebook; missing/unlisted patches, broken hashes and inconsistent results block import. Personal proposals enter the shared history only when accepted through normal review.

Verify a local clone without database access:

```sh
python manage.py verify_github_export /path/to/repository
python manage.py verify_github_export /path/to/repository --print-codebook
# Or:
make web-verify-github-export GITHUB_EXPORT=/path/to/repository PYTHON=.venv/bin/python
```

## Publication and recovery

Domain transactions create durable events for submission, withdrawal, session/assignment archival, shared codebook publication and instruction publication. A separate worker coalesces events for ten seconds, captures a repeatable database snapshot, and publishes its files in one Git commit. Events arriving during a run are handled in a following run. Each event is acknowledged individually, so database commit order cannot cause an event to be skipped; only one prepared/retrying/blocked run is permitted per study. No database lock is held across a GitHub request.

The worker checks frozen corpus blobs and previously managed file hashes before writing. Unrelated repository files and commits are preserved. The Git reference update is always non-forced; branch races are retried against the current head. External edits to managed files block the study's synchronization. Restore the expected managed content in GitHub, then use **GitHub → Retry synchronization**. No incoming edits are silently imported or overwritten.

Prepared runs survive crashes. If GitHub accepted the commit but its response was lost, matching the complete exported tree on retry records success without adding a duplicate commit. Expired installation tokens are renewed on retry; network errors and rate limits back off. A bounded lease with heartbeats permits recovery after a worker crash. API errors never include tokens or remote response bodies in the dashboard or logs.

Study admins can inspect status, last commit, pending event count and recent runs, and retry errors. Only the study owner can pause/resume publication or reconnect the installation after an App reinstall/repository rename. Reconnection requires fresh GitHub authorization and preserves the repository ID, branch and frozen corpus. Pausing prevents new runs; an in-flight update may finish. The usual database backup contains integration bindings, events, prepared runs and checkpoints. Back up App credentials separately with the protected server configuration.

Development worker commands:

```sh
python manage.py migrate
python manage.py sync_github --watch
# Process currently eligible work once (still respects pause and debounce):
python manage.py sync_github
```

Local Compose includes `github-sync`; deployment uses `coding-github-sync`. Run migrations before starting the worker. Without App configuration no study can be imported from GitHub, and ordinary studies and email delivery remain operational.

Run backend tests plus `corpus.browser_github_tests`. The browser test simulates provider authorization and GitHub API responses. Before enabling a real research study, install the App and verify import, one submitted case, one shared-codebook change, a conflict and replay on a dedicated test repository.
