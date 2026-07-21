# syntax=docker/dockerfile:1
#
# The control plane only. The hook bridge is not in here and cannot be — it runs
# inside Claude Code's process tree on the host, and reaches this container over
# HALYARD_URL. See the README.

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Installed from the committed lockfile, so an image built today and one built
# next month contain the same dependencies.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

# Not root. This process decides whether commands are allowed to run on the
# machine it is wired to; it has no business having more privilege than it needs.
RUN useradd --system --uid 10001 --create-home halyard

COPY --from=builder --chown=halyard:halyard /app/.venv /app/.venv

# Owned by the runtime user before the volume is created, so a fresh named
# volume inherits that ownership instead of arriving root-owned and unwritable.
RUN mkdir -p /data && chown halyard:halyard /data

ENV PATH="/app/.venv/bin:$PATH" \
    HALYARD_DB_PATH=/data/halyard.db \
    HALYARD_AUDIT_LOG=/data/audit.jsonl \
    # Binding to loopback inside a container would make the service reachable
    # only from inside it. Exposure is controlled by how the port is published
    # — see docker-compose.yml, which publishes to the host's loopback only.
    HALYARD_BIND=0.0.0.0:8787

USER halyard
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).status == 200 else 1)"]

CMD ["halyard"]
