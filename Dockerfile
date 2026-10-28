FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY *.py ./

ENTRYPOINT ["uv", "run", "--no-sync", "main.py"]
# Default for Railway; Cloud Run jobs override it with --args=triage / --args=debrief.
CMD ["run"]
