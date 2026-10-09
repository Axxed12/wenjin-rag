# Separate runtime and ML dependency layers: backend stays small and source edits do
# not invalidate the model-node's expensive PyTorch/transformers installation.
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv-bin

FROM python:3.12.3-slim-bookworm AS runtime-deps
COPY --from=uv-bin /uv /uvx /usr/local/bin/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    PATH=/app/.venv/bin:$PATH \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/huggingface \
    LLAMA_INDEX_CACHE_DIR=/tmp/llama_index \
    TMPDIR=/tmp \
    WENJIN_PROJECT_ROOT=/app

RUN groupadd --gid 10001 wenjin \
    && useradd --uid 10001 --gid 10001 --create-home wenjin \
    && install -d -o wenjin -g wenjin /app/data /app/raw_data /app/artifacts /var/lib/wenjin \
    && install -d -o root -g root -m 1777 /tmp
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-default-groups --no-install-project

FROM runtime-deps AS ml-deps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-default-groups --group ml --no-install-project

FROM runtime-deps AS backend
COPY src ./src
COPY scripts ./scripts
COPY ingest.py ./ingest.py
COPY corpora ./corpora
COPY configs ./configs
COPY deploy/with-secrets.sh /usr/local/bin/with-secrets
COPY deploy/health_model_api.py /app/deploy/health_model_api.py
RUN chmod 0755 /usr/local/bin/with-secrets \
    && uv sync --locked --no-dev --no-default-groups \
    && chown -R wenjin:wenjin /app
USER wenjin
ENTRYPOINT ["/usr/local/bin/with-secrets"]
CMD ["python", "-m", "wenjin.mcp_server"]

FROM ml-deps AS model-node
COPY src ./src
COPY deploy/with-secrets.sh /usr/local/bin/with-secrets
COPY deploy/health_model_api.py /app/deploy/health_model_api.py
RUN chmod 0755 /usr/local/bin/with-secrets \
    && uv sync --locked --no-dev --no-default-groups --group ml \
    && chown -R wenjin:wenjin /app
USER wenjin
ENTRYPOINT ["/usr/local/bin/with-secrets"]
CMD ["python", "-m", "wenjin.model_api", "serve", "--root", "/app", "--host", "0.0.0.0", "--port", "8765"]
