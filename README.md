# Collaborative corpus coding

The Raspberry Pi deployment with Traefik, Authelia and an existing PostgreSQL service is documented in [deploy/README.md](deploy/README.md).

Django serves the admin dashboard and the independent coder workspaces. PostgreSQL stores corpus versions, personal codebooks, annotations, immutable submissions, and merge decisions. Codebook YAML files are available through import/export. The database is the live application store.

## Deployment

Run commands from `~/workspace/coding`. Create a `.env` file with unique values. Set `RESEARCH_DIR` to the absolute path of the research repository for the initial QualCoder import. Generate secrets with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.

```dotenv
RESEARCH_DIR=/path/to/oss-ai-policies-sustainability
DJANGO_SECRET_KEY=replace-with-generated-secret
POSTGRES_PASSWORD=replace-with-another-generated-secret
ALLOWED_HOSTS=coding.example.org
CSRF_TRUSTED_ORIGINS=https://coding.example.org
```

```sh
make web-up
make web-migrate
make web-admin
make web-import
```

Put an HTTPS reverse proxy in front of `127.0.0.1:8000`. Preserve the Host header and set `X-Forwarded-Proto: https`. Trust that header only from your proxy. The Compose port binds to loopback. Production cookies require HTTPS. Disable proxy request-body logging and redact private-link notification responses. Gunicorn access logging is disabled. The private token travels in a URL fragment and a POST body, then is replaced with an HttpOnly cookie. Treat the private URL as a credential. Revoking or rotating it also invalidates existing coder cookies on their next request.

Visit `/admin/login/`, then `/` for the research dashboard. Create a coder session and select its cases. Copy the private URL displayed once and deliver it to that coder. Link rotation generates a replacement. Multiple coder sessions can receive the same case. Later assignment additions share the session’s personal codebook. Corpus versions are pinned when a case is assigned.

The coding screen uses canonical plain text. Select a passage, choose one or more codes, optionally enter a memo, and save. Overlapping passages are supported. Saves display confirmation and reject stale edits from other tabs. Code changes have the same revision protection. The concept editor supports memo, rationale, parent, archive flag and a reference to the selected corpus passage. YAML editing exposes the full metadata. Referenced concepts must be archived instead of deleted.

Submit each case when done. Submission freezes its annotations, document provenance and codebook. The admin can reopen the assignment, producing a new submission on the next completion. Codebook edits made after submission affect the personal workspace while earlier submission snapshots remain fixed.

Review a submission from the dashboard. Each proposed addition, edit, move or deletion requires a decision. Concurrent shared edits are identified. Accept the incoming version, keep the shared version, or map a proposal to a shared concept. Hierarchy errors and duplicate sibling names block publication atomically. An already reviewed submission cannot be applied twice. The export includes original concepts and the mapping accepted for that submission. Personal sessions retain their starting shared version throughout their lifetime.

## Imports and exports

Close QualCoder before the initial import. `make web-import` reads the existing QualCoder database through SQLite read-only mode and imports text documents, case membership, metadata, code hierarchy and memos. Existing annotations are omitted. It reuses code UUIDs from the registry when present. Taxonomy source metadata is retained in imported concept provenance. It creates the initial shared book only when none exists. Repeating corpus import reuses matching document hashes and IDs. Updated text creates a new immutable version. Existing assignments retain their pinned versions.

The admin can upload a corpus ZIP containing `manifest.yaml` and UTF-8 text files. Files are read directly from the archive, without extraction. The uncompressed archive limit is 100 MB.

```yaml
version: 1
cases:
  - id: project-one
    name: Project one
    metadata:
      repository: https://example.org/project-one
documents:
  - id: project-one-policy
    name: AI policy
    path: texts/policy.txt
    cases: [project-one]
    metadata:
      source_path: AI_POLICY.md
```

A codebook import/export uses the following structure. Keep UUIDs stable during edits. The parent may be a code or a category. A category is a grouping and cannot be applied directly to a passage. Sibling names are unique, with Unicode case-insensitive comparison. Unknown metadata fields survive YAML import/export. Published shared YAML revisions retain existing identities, allowing archival. Coder changes are reviewed through the three-way merge screen.

```yaml
version: 1
codes:
  - id: 14f28a65-1138-4389-a581-55db25aa146a
    kind: code
    name: Review capacity
    parent: null
    memo: '"Verified quotation" (source, exact location).'
    rationale: Research interpretation belongs here.
    archived: false
    references:
      - type: bibliography
        key: becker2015sustainability
        location: page 468
```

Corpus references use `type: corpus`, `document` (the application's document-version integer ID), `hash` (SHA-256 of UTF-8 text), UTF-16 `start`/`end`, and `quote`. The server checks quoted content against assigned document versions. Follow AGENTS.md when refining codebook content. Existing literature memos are preserved verbatim by the initial importer.

Download shared or personal codebooks as YAML. Submission review provides JSON and CSV results with coder, case, document identity/hash, selected text, UTF-16 offsets, code definitions, memos, submission time and accepted shared mappings. JSON also contains metadata and the complete submitted book. CSV encodes list fields as JSON strings.

## Backup and restore

```sh
make web-backup WEB_BACKUP=backup.sql
# Stop web writes during restore. Restore into an empty database.
docker compose stop web
docker compose exec -T db dropdb -U coding coding
docker compose exec -T db createdb -U coding coding
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U coding coding < backup.sql
docker compose start web
```

Back up `.env` separately in secure storage. Database dumps contain corpus text, annotations and token hashes. Retain them according to the research data policy. The repository mount in the application is read-only. Uploaded corpus text is stored in the database, so the dump includes it.

## Local development and tests

```sh
python3 -m venv .venv-web
.venv-web/bin/pip install -r requirements.txt
.venv-web/bin/python manage.py migrate
.venv-web/bin/python manage.py createsuperuser
.venv-web/bin/python manage.py runserver
.venv-web/bin/python manage.py test corpus
```

Development defaults to SQLite and HTTP cookies on localhost. Production uses the PostgreSQL service with row-level transaction locks. All annotation writes lock the coder session and assignment, validate document content, and compare revisions. The API uses same-origin CSRF protection. Corpus text, memo text and metadata are rendered as text, preventing embedded corpus HTML from executing.

For the optional real-browser check, install `requirements-test.txt`, run `python -m playwright install chromium`, then `python manage.py test corpus.browser_tests`. To use an installed Chrome instead, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/google-chrome`. This check creates a temporary database and exercises private-link access, code creation with corpus evidence, passage coding, memo autosave, submission and admin merge. PostgreSQL tests also exercise simultaneous writes when `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_DB` and `POSTGRES_PASSWORD` point to a disposable database server whose user can create test databases.

## Code colors

Selectable taxonomy terms have a stable default color derived from their UUID. The concept editor includes a Highlight color picker, and YAML supports an optional `color: '#RRGGBB'` field. Code lists show matching swatches. Coded passages and annotation labels use a readable tint of the selected color. Overlapping passages show horizontal bands for all assigned code colors, with code names on hover. Submitted review pages use the submission's frozen codebook colors. Existing codebooks display colors immediately, and YAML downloads include the effective colors.

## Submission email notifications

Set `CASE_EMAIL_ENABLED=1`, `PUBLIC_BASE_URL`, `CASE_SUBMISSION_EMAIL_TO` (comma-separated administrator addresses), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `MAIL_FROM` in the protected environment. Use `SMTP_STARTTLS=1` and `SMTP_TLS=0` for STARTTLS, or the reverse for implicit TLS. Keep credentials outside Git.

Submitting a case stores an email in the same database transaction as its immutable submission. The separate mailer service sends the notification with coder, case, submission time, counts and an authenticated review link. Delivery errors retain the queued notification and retry with increasing delays up to one hour. The dashboard shows Queued, Retrying or Sent. Sent means that SMTP accepted the message. Code excerpts and private coder links are excluded from the email.

The Compose stack includes the mailer worker. For a manual delivery pass, run `python manage.py deliver_notifications`; use `--watch` for continuous delivery. Disabling `CASE_EMAIL_ENABLED` pauses delivery and creation of new notifications. Existing pending messages remain stored. Notifications are created for new submissions after email is enabled, including resubmissions after an admin reopens a case. They are not generated retrospectively for existing submissions. A stable Message-ID is retained across retries, though an interrupted SMTP acknowledgement can still produce a duplicate delivery.
