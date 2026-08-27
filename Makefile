.PHONY: up down test crawl stats reset psql

# Builds and starts db + fake-api, waits for both healthchecks, then runs
# the crawl. Exits with the crawler's own exit code once it finishes --
# --abort-on-container-exit tears the rest down instead of leaving db and
# fake-api running forever, since neither of those ever exits on its own.
up:
	docker compose up --build --abort-on-container-exit --exit-code-from crawler

down:
	docker compose down

test:
	uv run pytest

# One-off runs against whatever's already up (starting db/fake-api first,
# and waiting for their healthchecks, if they aren't) -- same image `up`
# built, a fresh command appended to its ENTRYPOINT instead of a fresh crawl.
crawl:
	docker compose run --build --rm crawler crawl http://fixture.local/

stats:
	docker compose run --build --rm crawler stats

# Opens psql inside the db container. The same database is also published
# on localhost:55433 (postgres / crawler) for an external client -- see the
# ports mapping in docker-compose.yml.
psql:
	docker compose exec db psql -U postgres -d crawler

# Down, plus the volumes -- a clean slate, not just a stop. Safe to run
# against nothing already up: compose down is a no-op then, not an error.
reset:
	docker compose down --volumes --remove-orphans
