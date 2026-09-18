# Fitness MCP server: one process, published on $PORT.
FROM python:3.11-slim

# Pinned rather than :latest so a rebuild of an old commit resolves the same
# way. uv is copied from its own image instead of pip-installed: it lands as a
# single static binary with no Python-level dependencies to conflict with the
# project's own.
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# tool code, so edits to a tool reuse the cached install. --frozen fails the
# build if uv.lock is out of date with pyproject.toml rather than silently
# resolving something different from what was tested.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    FITNESS_MCP_HOST=0.0.0.0 \
    FITNESS_MCP_PORT=8080 \
    PORT=8080

EXPOSE 8080

# Shell form so ${PORT} expands -- Cloud Run injects it and it is not always
# 8080. `exec` keeps python as PID 1 so SIGTERM reaches it directly. No
# entrypoint.sh: a script file here would reintroduce the CRLF-shebang class
# of failure for nothing, since there is only one command to run.
CMD ["sh", "-c", "exec python main.py --host 0.0.0.0 --port ${PORT:-8080}"]
