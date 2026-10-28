FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev
EXPOSE 8000
# bring the database up to date, then serve on the port the host gives
CMD ["sh", "-c", "uv run --no-dev alembic upgrade head && uv run --no-dev uvicorn splitledger.main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir src"]
