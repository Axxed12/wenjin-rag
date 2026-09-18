# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv-bin
FROM python:3.12.3-slim-bookworm

COPY --from=uv-bin /uv /uvx /usr/local/bin/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    PATH=/app/.venv/bin:$PATH \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/huggingface \
    LLAMA_INDEX_CACHE_DIR=/tmp/llama_index

RUN groupadd --gid 10001 wenjin && useradd --uid 10001 --gid 10001 --create-home wenjin
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
COPY ingest.py ./ingest.py
COPY corpora ./corpora
COPY configs ./configs
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev && chown -R wenjin:wenjin /app

USER wenjin
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',8765),3); s.close()"

CMD ["python", "-m", "wenjin.mcp_server"]
