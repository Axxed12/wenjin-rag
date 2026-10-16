# Linux HTML source replay admission diagnostic

## Result

The stateless container's `source_text_mismatch` is reproducible from the same mounted raw source and existing BM25/Qdrant assets. It is isolated to six chunks from `22-025r4.html`; the rejected target remains rejected. No source text, index, manifest, or Qdrant record was changed.

## Checks

- Host Windows `admit_index` for target `ogc-3dtiles-1.1-blocks-split-candidate-003` passed with every problem list empty.
- Using the authorized disposable `docker compose run --rm --no-deps` with the profile's read-only asset mounts and current Qdrant secret, Linux `audit_index` returned `FAIL` only for `source_text_mismatch`. The six affected row ranges were 1897-1945, 3689-3741, 9436-9509, 9510-9566, 9570-9674, and 9681-9808 of `22-025r4.html`. No body text was emitted.
- Both sides report the same source byte size: 52,897,656 bytes. The Linux audit had no `source_hash_mismatch` or `source_content_invalid`, so the mounted source passed its manifest hash checks.
- Both environments use BeautifulSoup 4.15.0 and lxml 6.1.3. The native libxml2 versions differ: Windows runtime/compiled `2.11.9`; Linux runtime/compiled `2.14.6`.
- Replaying those exact source line ranges produced different SHA-256 and character counts between the two environments. For example, range 1897-1945 produced 919 characters on Windows and 1,072 on Linux. This comparison reports hashes/counts only, not source text.

## Interpretation and boundary

The evidence points to native HTML parser behavior changing with libxml2, despite identical Python package versions and source bytes. This is consistent with the host's accepted index having been produced under the Windows parser runtime, but the build-time runtime was not independently recorded in the target metadata. The evidence does not support a CRLF or bind-mount path explanation.

Do not suppress `source_text_mismatch` or relax `admit_index`. The narrow compatibility route is to build the Linux lxml runtime against the exact libxml2 version used to create this index, then rerun the full audit. If that parser runtime cannot be reproduced and pinned, the sound alternative is a separately authorized Linux-built candidate with its own manifest/index identity and full acceptance. Neither was performed here.

The container check used a disposable `run --rm --no-deps` diagnostic, not `up`/`start`; it ended automatically. No persistent service was operated.
