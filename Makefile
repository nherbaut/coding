PYTHON ?= python3

web-up:
	docker compose up -d --build

web-migrate:
	docker compose run --rm web python manage.py migrate

web-admin:
	docker compose run --rm web python manage.py createsuperuser

web-import:
	docker compose run --rm web python manage.py import_qualcoder /research/data/coding.qda --taxonomy /research/coding/taxonomy.json --registry /research/coding/codebook.json

web-test:
	$(PYTHON) manage.py test corpus

WEB_BACKUP ?= coding-web-backup.sql
web-backup:
	docker compose exec -T db pg_dump -U coding coding > "$(WEB_BACKUP)"

.PHONY: web-up web-migrate web-admin web-import web-test web-backup
