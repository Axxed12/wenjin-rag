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
    LLAMA_INDEX_CACHE_DIR=/tmp/llama_index \
    TMPDIR=/tmp

# 中文分词的 jieba 由 `uv sync` 从 pyproject/uv.lock 装上（jieba>=0.42.1），不单独装。
# 它会在 `tempfile.gettempdir()` 下写 `jieba.cache`；这里显式钉住 TMPDIR=/tmp，
# 该目录在本镜像里对 uid 10001 可写（HF_HOME/LLAMA_INDEX_CACHE_DIR 本来就依赖它）。
# 若临时目录不可写，jieba 只记一条 "Dump cache file failed." 后继续，不会崩，
# 但**每次启动都要重建前缀词典**（约 0.3–0.5 s），启动慢且日志有噪声。

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
