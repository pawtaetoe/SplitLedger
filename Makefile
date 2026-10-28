.PHONY: setup dev migrate test check record-fixtures seed-dev

setup:
	uv sync
	uv run pre-commit install

migrate:
	uv run alembic upgrade head

dev:
	docker compose up -d db
	uv run alembic upgrade head
	uv run uvicorn splitledger.main:app --reload --app-dir src

test:
	uv run pytest -q

check:
	uv run ruff check src tests
	uv run mypy --strict src
	! grep -rPzo ',\s*\n\s*[)\]}]' src tests --include='*.py'
	uv run pytest -q

record-fixtures:
	uv run python scripts/record_fixtures.py

seed-dev:
	uv run python scripts/seed_dev.py $(PHONE) $(if $(RESET),--reset)
