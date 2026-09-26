FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && \
    groupadd --gid 1000 spy && useradd --uid 1000 --gid 1000 --no-create-home spy && \
    mkdir -p /state /run/fomo-spy && chown spy:spy /state /run/fomo-spy
ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD fomo-spy healthcheck --socket /run/fomo-spy/daemon.sock
ENTRYPOINT ["fomo-spy"]
CMD ["run", "--config", "/etc/fomo-spy/config.toml"]
