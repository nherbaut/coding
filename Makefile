PYTHON ?= python3

web-up:
	docker compose up -d --build

web-migrate:
	docker compose run --rm web python manage.py migrate

web-local-admin:
	docker compose run --rm web python manage.py createsuperuser

STUDY ?=
CORPUS ?=
web-import:
	@test -n "$(STUDY)" -a -n "$(CORPUS)" || (echo 'Use make web-import STUDY=<study-id> CORPUS=<zip-path>'; exit 2)
	docker compose run --rm -v "$(abspath $(CORPUS)):/tmp/corpus.zip:ro" web python manage.py import_corpus /tmp/corpus.zip --study "$(STUDY)"

web-test:
	$(PYTHON) manage.py test corpus

WEB_BACKUP ?= coding-web-backup.sql
web-backup:
	docker compose exec -T db pg_dump -U coding coding > "$(WEB_BACKUP)"

.PHONY: web-up web-migrate web-local-admin web-import web-test web-backup

web-github-sync:
	docker compose run --rm github-sync python manage.py sync_github

GITHUB_EXPORT ?=
web-verify-github-export:
	@test -n "$(GITHUB_EXPORT)" || (echo 'Use make web-verify-github-export GITHUB_EXPORT=/path/to/repo'; exit 2)
	$(PYTHON) manage.py verify_github_export "$(GITHUB_EXPORT)"

.PHONY: web-github-sync web-verify-github-export
