# 2026-10-17 GitHub Actions CI development log

## Scope

Add a single lightweight GitHub Actions workflow for a fixed allowlist of 12 local/synthetic test modules. Do not run the frozen evaluation set, start local/network services, deploy, change the dependency lock, or use credentials. This pass does not edit `code.md` or the development-log index.

## Dependency and test audit

- `pyproject.toml` requires Python `>=3.12,<3.13` (line 7). Main runtime dependencies are lines 8-21; `dev` contains pytest and Ruff, while `ml` contains the Hugging Face embedding stack, sentence-transformers, torch, and transformers (lines 35-50). The default groups include both dev and ml, so CI explicitly uses `uv sync --locked --no-default-groups --group dev` to avoid installing ML dependencies.
- `uv.lock` pins the project resolution to Python 3.12 (lines 1-3) and records the project runtime, `dev`, and `ml` groups under `wenjin-rag` (around lines 2523-2549). The local uv executable reports 0.11.3.
- The workflow invokes pytest with these explicit modules only: `test_audit`, `test_bm25`, `test_chunking`, `test_content_hashing`, `test_evidence`, `test_fusion`, `test_query_expansion`, `test_query_format`, `test_evidence_threshold`, `test_index_registry`, `test_context_selection_contract`, and `test_long_line_evidence_contract`.
- `test_query_expansion.py` uses literal queries only; `test_evidence_threshold.py` uses inline confusion-matrix rows; `test_index_registry.py` builds manifest/SQLite/registry fixtures under `tmp_path` and injects a fake Qdrant client; `test_context_selection_contract.py` uses in-memory candidate dictionaries; `test_long_line_evidence_contract.py` uses inline text and in-memory chunks. These tests do not load repository corpora, models, credentials, or live services.
- The other selected modules in this fixed allowlist use temporary SQLite/files, literal objects, or injected fake clients. The separately reviewed source-context/PDF and jieba subprocess modules are not in the selected 12-module list.
- Do not use `pytest -m quick`: `tests/conftest.py` applies markers by module name (lines 8-66), and marker filtering would still collect modules outside this allowlist. The workflow passes the 12 module paths directly.
- Explicit exclusions: `test_access_and_paths.py` is marked quick but reads the checkout corpus and index configuration in later tests; `test_frozen_dataset.py` reads frozen/dev evaluation data; `test_model_api_admission_contract.py` starts loopback TCP/HTTP listeners and includes service/MCP construction.

## Workflow

- `.github/workflows/ci.yml` runs on all pull requests, pushes to `main`, and manual dispatch. It uses `ubuntu-24.04`, Python 3.12, uv 0.11.3, `contents: read`, a 15-minute job timeout, and same-ref cancellation.
- Pinned action references are `actions/checkout` v5.0.0 (`08c6903cd8c0fde910a37f88322edcfb5dd907a8`) and `astral-sh/setup-uv` v7.0.0 (`eb1897b8dc4b5d5bfe39a428a8f2304605e0983c`); their action metadata was reviewed by the parent agent. Checkout does not persist credentials and uv caching is disabled.
- CI sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONNOUSERSITE=1`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `UV_NO_ENV_FILE=1`; it does not configure secrets or access `.env`.

## Validation status

- Workflow and test command were inspected statically. No pytest command, dependency sync, service, model, or workflow run was executed in this implementation pass, per scope.
- Test results are tracked separately in the independent tester's log; the first remote workflow validation is pending.
