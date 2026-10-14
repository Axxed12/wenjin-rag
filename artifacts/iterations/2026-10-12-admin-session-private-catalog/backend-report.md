# Backend report: private shared catalog and identity authorization

## Result

Private shared indexes now enter the administrator catalog only through an explicit, unambiguous schema-v1 private route declaration. Bare private target files remain visible as non-searchable candidates; they are not automatically activated or granted. A selected private target must also pass quick structural readiness before it is marked active/searchable. Retrieval and source reads retain the full admission and identity checks.

The catalog response labels published route-selected sources as `private_config` / `published`, and unresolved candidates as `private_candidate` with a candidate, ambiguous, or needs-review status. Admin discovery can show library metadata even before a read grant exists; `readable` and `searchable` stay false until the caller has a library grant. Admin role alone does not authorize search or source content.

Existing release activation remains authoritative. A previously activated release whose active pointer disappears fails closed instead of falling back to private configuration. Catalog construction reuses one private-config view for target selection and candidate presentation so a single response does not mix two route snapshots.

## Validation

Ran the following isolated regression command:

```text
uv run pytest -q tests/test_private_catalog_identity.py tests/test_legacy_search.py tests/test_identity_http.py tests/test_mcp_identity_http.py tests/test_mcp_release_identity_tcp.py tests/test_library_release_domain.py
24 passed, 38 warnings in 13.72s
```

The new synthetic integration coverage exercises a real temporary BM25/Qdrant index and temporary IdentityStore: explicit route selection, bare-config and invalid-route candidates, ambiguity, broken-index fail-closed behavior, release-pointer fail-closed behavior, admin discovery without implicit grant, admin and reader search/source denial before grant, reader and delegated MCP access after grant, and immediate denial after revoke.

Ruff passed for the changed backend and focused regression files:

```text
uv run ruff check src/wenjin/retrieval/legacy_catalog.py src/wenjin/management_http/identity_http.py tests/test_private_catalog_identity.py tests/test_legacy_search.py tests/test_identity_http.py
All checks passed!
```

No real corpus, configuration, identity database, live service, or authorization database was changed by these tests. The repository's existing real-catalog read-only evidence is tracked separately in `private-readonly-before.json`; post-change real read-only confirmation remains with the parent integration pass.

## Changed paths

- `src/wenjin/retrieval/legacy_catalog.py`
- `src/wenjin/management_http/identity_http.py`
- `tests/test_private_catalog_identity.py`
- `tests/test_legacy_search.py`
- `tests/test_identity_http.py`

## Remaining boundary

`published` here denotes an explicit legacy private-route selection. It does not mean the formal release prepare/activate workflow ran. Full retrieval admission remains a runtime check and can still reject a source that passed quick catalog readiness.
