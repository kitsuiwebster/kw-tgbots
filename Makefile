.PHONY: start stop restart build rebuild logs

start:
	docker compose up -d

stop:
	docker compose down

restart: stop start

build:
	docker compose build
	docker compose up -d

rebuild:
	docker compose build --no-cache
	docker compose up -d

logs-italiano:
	docker compose logs -f italian-bot

logs-wherebased:
	docker compose logs -f where-based-bot
