# Collaborative corpus coding

Self-hosted Django/PostgreSQL application for text coding, shared codebooks and reproducible coding reports. Administrators authenticate through Authelia using the LDAP group `coding-admins`. Coders enter through private links.

## Roles and study access

- **Owner**: the admin who creates a study. Owners can invite and revoke other study admins.
- **Admin**: any authenticated member of `coding-admins`. They can create studies, and manage studies they own or have accepted an invitation to administer.
- **Coder**: a private-link session within one study. Coders see only assigned cases, their annotations and their personal codebook.

LDAP group membership does not grant access to every study. Study access is checked on each administrator request. Invitations name the LDAP username; the invitee must sign in through Authelia and explicitly accept on **My studies**. Optional invitation email contains a sign-in link, not an authentication token. Only owners can grant or revoke study admin rights. Revoking membership removes access on the next request. Losing the LDAP group blocks application admin access independently of study membership.

Each study contains one shared codebook with published revisions, cases and immutable text versions, research questions, coding guidelines, coder sessions, submissions, reviews, reports and archives. Case/document keys are unique within their study. Texts may belong to several cases in that study. Cross-study assignments, corpus links and revisions are rejected.

## Study workflow

1. Sign in and create a study on **My studies**.
2. Add research questions and coding guidelines in **Questions and guidelines**. Both have explicit ordering, and guidelines can be associated with questions. Saving publishes a combined instructions revision.
3. Create cases and add UTF-8 text/Markdown files or pasted text in **Cases and texts**, or import a corpus ZIP from the study overview.
4. Import a YAML codebook in the study overview. Code memos, structured references and researcher rationale remain separate.
5. Create coder sessions and assign cases. An optional coder email queues a private link and the case list. Cases and case selectors are alphabetical, ignoring capitalization.
6. Configure this study's submission-notification email recipients in **Settings and admins**. There is no global recipient list.
7. Owners can invite other admins by LDAP username from the same settings page, with optional email delivery.

A coder starts with the current shared codebook and current instructions. They can inspect the questions and guidelines in the coding screen, develop their personal codebook, select passages, apply multiple codes, and add/edit annotation memos. Overlapping annotations are supported. Markdown and safe embedded HTML are rendered with source-mapped UTF-16 positions; the Reading view control exposes canonical source text. Hashes and source text are not rewritten to render documents.

Submitting locks the case and captures annotations, personal codebook, source document versions and instructions used. Coders can unsubmit to edit again. Admins can explicitly update instructions for an existing coder session; previous submissions remain immutable. Updating instructions does not synchronize codebooks automatically.

Study admins review codebook proposals using the session base, submitted personal book and current shared book. Accept, reject or map changes individually, or use **Accept all proposals** before applying decisions. Merges affect only this study and keep original annotation identities and mappings. Referenced concepts must be archived rather than deleted.

## Corpus ZIP and YAML

Corpus ZIP files contain UTF-8 text files and `manifest.yaml`:

```yaml
version: 1
cases:
  - id: project-a
    name: Project A
    metadata: {repository: 'https://example.org/a'}
documents:
  - id: project-a-policy
    name: AI_POLICY.md
    path: texts/policy.md
    cases: [project-a]
    metadata: {source: 'https://example.org/a/AI_POLICY.md'}
```

Imports always target an explicit study. Reusing the same manifest identifiers in another study creates independent data. Importing a changed text creates a new immutable document version; existing assignments keep their pinned versions. The importer enforces a 100 MB uncompressed limit and reads files without extracting them to the filesystem.

Codebooks use YAML schema version 1 with UUID concepts, `kind` (`code` or `category`), `parent`, `name`, `memo`, `references`, `rationale`, `archived` and optional `color` (`#RRGGBB`). Export an existing study or personal codebook to inspect the complete format. Imported provenance metadata is retained. Corpus references must match a document/hash/passage in the target study.

CLI corpus import:

```sh
python manage.py import_corpus corpus.zip --study STUDY_UUID
make web-import STUDY=STUDY_UUID CORPUS=/path/to/corpus.zip
```

The study UUID is visible in the study URL. The application has no global corpus/codebook bootstrap or QualCoder importer. Original research files remain in the separate research repository.

## Reports and retained records

**Reports** provides codebook evidence (source memos, references, rationale, excerpts and counts) and side-by-side document comparisons. The default uses the latest available submission per coder/case; **Current work** explicitly includes drafts. Different document versions are flagged and never aligned by offsets. Matching versions support synchronized scrolling and passage comparisons at selection boundaries. These compare code identities, not statistical agreement.

One annotation is one coded passage per coder session. Multiple codes on a passage do not multiply category totals. Counts distinguish direct coding and descendant rollups, distinct documents, document versions, cases and coder sessions. Original identities and accepted shared mappings are separate views. Identical names alone never merge concepts.

Saved reports capture study identity, the current questions/guidelines, each coder's actual instructions, codebooks, texts/hashes, coding, mappings and filters. Subsequent changes cannot modify them. Exports include standalone HTML (with inline Markdown renderer), excerpts CSV, counts CSV, full-provenance JSON and browser Print/PDF. CSV formula prefixes are escaped; JSON retains source text exactly.

**Delete coder session**, **Remove assignment** and **Delete submitted case** remove active access or review entries while retaining research records in the study's **Archive**. A latest withdrawn submission reopens its case. Removing an older submission does not unlock a newer one. Reassigning a removed case restores its coding and pinned versions. A withdrawal cannot be merged and does not undo previously accepted shared-codebook changes. Existing saved reports never change.

## Deployment and authentication

Docker images run on amd64/arm64 with Python only; the browser bundle is checked in. Configure PostgreSQL, a persistent database volume, HTTPS, Authelia and the trusted proxy headers before starting the deployed app. Administrator paths are `/` and `/admin/*`; private coder paths are `/access/*`, `/work/*` and `/api/*`. Static files and `/healthz/` do not require admin authentication.

Set these through a protected environment file, not Git:

- `DEBUG=0`, `DJANGO_SECRET_KEY`, `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`.
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- `AUTHELIA_AUTH_ENABLED=1`, `AUTHELIA_ADMIN_GROUP=coding-admins`, `AUTHELIA_PROXY_SECRET` (at least 32 characters), `AUTHELIA_TRUSTED_PROXIES` (proxy IP/CIDR list).

The proxy must authenticate admins and supply `Remote-User`, `Remote-Groups`, optional `Remote-Email`, and `X-Coding-Proxy-Secret`. Strip/rewrite incoming identity headers. The app checks the actual connecting proxy address and secret on every admin request. Deployed admin access requires Authelia; Django password cookies cannot bypass it. LDAP identity is normalized to a case-insensitive username. User/email records are refreshed from trusted Authelia headers.

```sh
make web-up
make web-migrate
```

Run migrations before starting the app. The mailer uses the same environment/image and waits for app health. Store TLS/proxy configuration outside the application repository. `deploy/` contains infrastructure templates; keep secrets out of those templates.

This is a pre-production schema replacement. Existing application test data is disposable: recreate the dedicated application database/schema and initialize the current baseline migration. There are no data-conversion migrations, default studies, old global routes, or compatibility imports. LDAP and source research files are not reset.

## Transactional email

Set `CASE_EMAIL_ENABLED=1`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS=1` or `SMTP_TLS=1`, and `MAIL_FROM`. Credentials and a stable `DJANGO_SECRET_KEY` belong in the protected environment. Choose one TLS mode. Configure submission recipients per study in the application.

Coder invitations and admin invitations are queued by explicit administrator actions. Submission notices include study, coder, case and a study-scoped review link. The durable queue uses leases and retry backoff. SMTP failure does not roll back a submission. Pending private-link email bodies are encrypted and cleared after delivery; replacing/revoking a link cancels old invitations. Withdrawing submissions cancels unsent notices. Admin invitation delivery is canceled when an invitation is accepted or revoked. Worker:

```sh
python manage.py deliver_notifications --watch
```

## Backup and restore

A complete PostgreSQL dump includes studies, membership/invitations, instructions/codebook revisions, source text, coding, saved reports and email queues. Keep SMTP secrets and `DJANGO_SECRET_KEY` separately in protected configuration.

```sh
make web-backup WEB_BACKUP=/secure/path/coding.sql
# Stop app and mailer, restore into an empty dedicated database:
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U coding coding < /secure/path/coding.sql
# Restart app and mailer after restore.
```

## Development and verification

For local development only, use `DEBUG=1`, `AUTHELIA_AUTH_ENABLED=0` and SQLite, or a disposable PostgreSQL database. Password-authenticated local staff accounts are for development/testing; deployed users use Authelia.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
make web-test PYTHON=.venv/bin/python
```

Backend checks cover study isolation, invitations/ownership, group removal, cross-study identifiers/relationships, protocol snapshots, imports, merges, reports, private-link isolation, Unicode offsets, concurrency and removal. PostgreSQL concurrency tests require a disposable DB user with permission to create test databases. Browser checks:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/google-chrome python manage.py test \
  corpus.browser_study_tests corpus.browser_tests corpus.browser_markdown_tests \
  corpus.browser_reporting_tests corpus.browser_removal_tests
```

Markdown renderer sources are in `frontend/`. Rebuild its pinned bundle and third-party notices with Node 22+:

```sh
cd frontend
npm ci
npm test
npm run build
```
