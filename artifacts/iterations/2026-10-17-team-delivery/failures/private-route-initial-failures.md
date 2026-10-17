# Initial synthetic contract failures

All cases used temporary roots, generated index targets, synthetic source bytes, in-memory Qdrant, and loopback-only HTTP. The bearer was a synthetic test token and is intentionally omitted.

## ACL denial leaks corpus identity

Command: `python -m pytest -q tests/test_mcp_private_route_delivery_contract.py -k private_route_discovery --tb=short`

```text
__ test_private_route_discovery_search_source_acl_revoke_and_version_restart __
tests\test_mcp_private_route_delivery_contract.py:301: in test_private_route_discovery_search_source_acl_revoke_and_version_restart
    assert denied_query["corpus_id"] is None
E   AssertionError: assert 'private-wire-1.0' is None
1 failed, 1 deselected
```

The denied response had `error="forbidden"`, an empty evidence list, but returned `corpus_id="private-wire-1.0"`. The synthetic query marker was absent from the response. The contract requires the denied result not to disclose the corpus identity.

## Target-bound raw snapshot is not used for MCP download

Command: `python -m pytest -q tests/test_mcp_private_route_delivery_contract.py -k private_raw_snapshot --tb=short`

```text
_________ test_private_raw_snapshot_source_matches_the_routed_target __________
tests\test_mcp_private_route_delivery_contract.py:522: in test_private_raw_snapshot_source_matches_the_routed_target
    assert downloaded.status_code == 200, downloaded.text
E   AssertionError: {"error":"source_modified","detail":"文件字节与manifest登记的Hash不符：private-guide.txt"}
E   assert 409 == 200
1 failed, 2 deselected
```

The selected synthetic target was `snapshot-source-target-7-4`, corpus `snapshot-source-7.4`. Its manifest-bound bytes were `Synthetic private source snapshot-source 7.4.\nUnique marker: private_marker_7_4.\n`, SHA-256 `83084f2145f816bb46a0be5a34d8158e22ad259727a92183123dd19bbe746710`. The same relative path in mutable `raw_data` held different synthetic bytes. Search evidence identified the expected corpus and target, while GET of its emitted `download_path` returned 409 `source_modified` instead of the manifest-bound bytes.

## Running MCP retains route A after live route changes to B

Command: `python -m pytest -q tests/test_mcp_private_route_delivery_contract.py -k live_management --tb=short`

```text
___ test_private_route_switch_is_consistent_between_live_management_and_mcp ___
tests\test_mcp_private_route_delivery_contract.py:591: in test_private_route_switch_is_consistent_between_live_management_and_mcp
    assert target_a.target_id not in current_sources[corpus_a]["index_usable_targets"]
E   AssertionError: assert 'live-route-target-1-0' not in ['live-route-target-1-0']
1 failed, 3 deselected
```

The initial run used a low-level MCP server helper. The test was then upgraded to inject the production `create_release_resolver`, sharing the router's embedder, reranker, and search lock. After route JSON changed from synthetic target `live-route-target-1-0` to `live-route-target-2-0`:

- a fresh `LegacySearchService` catalog/search selected B and returned B's unique marker;
- `resolver.begin_request()` returned a router containing B and excluding A;
- a new MCP query returned B evidence and the source endpoint returned the expected B bytes/SHA;
- a new MCP query explicitly requesting A returned `invalid_target` with empty evidence;
- MCP `list_corpora` still reported A as usable:

```text
___ test_private_route_switch_is_consistent_between_live_management_and_mcp ___
tests\test_mcp_private_route_delivery_contract.py:648: in test_private_route_switch_is_consistent_between_live_management_and_mcp
    assert target_a.target_id not in current_sources[corpus_a]["index_usable_targets"]
E   AssertionError: assert 'live-route-target-1-0' not in ['live-route-target-1-0']
1 failed, 3 deselected
```

This remains a real directory-state mismatch, rather than stale route execution. The contract permits an authorized historical corpus row to remain listed, but requires that it have no usable target after the route moved to B. Core is correcting the request-snapshot state branch.
