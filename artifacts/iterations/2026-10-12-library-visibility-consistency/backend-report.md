# Backend report: library classification and visibility consistency

## Result

Added an administrator-managed `public` / `private` library classification that is presentation metadata only. The classification is stored in a separate `library_classifications` table keyed by stable `library_id`, with revision CAS and identity audit events. Schema v1 identity databases migrate to v2 without changing users, grants, resources, or source manifests.

The catalog now returns a library-level `classification` object (`classification`, `revision`, `defaulted`, `source_visibility`) alongside the separate top-level normalized `source_visibility`; source rows expose their own `source_visibility` beside `source_scope` and publication status. An unset classification defaults to `public` only when all known sources normalize to public. Private/internal, mixed, or unknown source visibility defaults to private. Classification updates never change grants or activate resources, and resource synchronization does not set the classification.

The admin API is `GET` / `PUT /api/admin/libraries/{library_id}/classification`. PUT accepts `{ "classification": "public" | "private", "expected_revision": n }`. It is admin-only, validates catalog membership (including inactive/candidate catalog entries), and returns 409 for stale revisions. A reader cannot change the label.

Per the latest product decision, active administrators may read active resources without per-resource grants. This role check is centralized in the access snapshot: user status, must-change-password state, and active resource/file status remain mandatory. A personal MCP credential retains its owner's role, so admin credentials use this same rule while reader credentials remain grant-limited. An inactive resource stays unreadable to administrators.

## Validation

```text
uv run pytest -q tests/test_private_catalog_identity.py tests/test_identity_store.py tests/test_identity_http.py tests/test_mcp_identity_http.py tests/test_mcp_release_identity_tcp.py
20 passed, 14 warnings in 22.56s
```

The integration test verifies inferred private classification, admin-only CAS updates and stale-revision conflict, malformed value rejection, classification changes leaving grants and reader access unchanged, active private-corpus search/source access for an admin without a grant (including an admin personal MCP credential), unchanged reader/MCP denial before an individual grant, continued reader access after changing the label back to private, and immediate denial after grant revocation. Identity-store tests cover classification audit/CAS, identity schema migration, administrator access to active resources, and refusal for inactive resources.

```text
uv run ruff check src/wenjin/identity/store.py src/wenjin/identity/audit.py src/wenjin/management_http/identity_http.py src/wenjin/management_http/app.py src/wenjin/retrieval/legacy_catalog.py tests/test_private_catalog_identity.py tests/test_identity_store.py
All checks passed!

git diff --check -- [changed backend and test paths]
passed
```

All exercised services, corpora, Qdrant data, and identity databases were temporary test fixtures. No real source manifest, authorization database, or running service was changed.

An independent identity-boundary contract pass also ran with `tests/test_library_visibility_consistency_contract.py` in the set: **22 passed, 14 warnings**; its test lint and format checks passed. See [validation-report.md](validation-report.md) for that review's exact scope and limits.

## Changed paths

- `src/wenjin/identity/store.py`
- `src/wenjin/identity/audit.py`
- `src/wenjin/management_http/identity_http.py`
- `src/wenjin/management_http/app.py`
- `src/wenjin/retrieval/legacy_catalog.py`
- `tests/test_private_catalog_identity.py`
- `tests/test_identity_store.py`

## Boundary

The classification label does not publish source data, grant reader access, change MCP grants, or affect retrieval/source authorization. `source_visibility` and `source_scope` continue to describe source provenance independently.
