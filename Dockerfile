FROM ghcr.io/astral-sh/uv@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && mkdir -p /data \
    && chown 10001:10001 /data

ENV DSM_TAORAN_ENVIRONMENT=production
ENV DSM_TAORAN_DATABASE_PATH=/data/taoran_agent.db
ENV PATH="/app/.venv/bin:$PATH"
LABEL top.yudaozhijian.taoran.release="0.27.7rc2-ui-clean-20260908"
USER 10001:10001
EXPOSE 8030
CMD ["uvicorn", "taoran_agent.api:app", "--host", "0.0.0.0", "--port", "8030", "--workers", "1", "--limit-concurrency", "48", "--timeout-graceful-shutdown", "75", "--no-access-log"]

ARG GIT_COMMIT
LABEL org.opencontainers.image.revision=$GIT_COMMIT
