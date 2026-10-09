# Temporary Docker acceptance cleanup status

Date: 2026-10-09. Runtime tests used a unique temporary Docker network and disposable containers. A fresh read-only check after the run found no matching `mcp-stateless` containers or networks; those were removed by the test's `docker rm -f` / `docker network rm` cleanup.

Three test directories remain directly under `%TEMP%` because the Windows shell safety review rejected explicit file and directory deletion attempts:

| Absolute path | Remaining files |
|---|---|
| `C:\Users\Onebiter\AppData\Local\Temp\mcp-stateless-docker-accept-4093gjcx` | `identity/identity.sqlite` |
| `C:\Users\Onebiter\AppData\Local\Temp\mcp-stateless-docker-accept-hy4u83_6` | `identity/identity.sqlite` |
| `C:\Users\Onebiter\AppData\Local\Temp\mcp-stateless-docker-accept-ud17qend` | `identity/identity.sqlite`; `project/corpora/synthetic-library-1.0/source-manifest.json` |

No token plaintext or private key appears in the listed files. The SQLite files contain only short-lived synthetic identity records and credential hashes. The temporary certificate/key directories from other test attempts were removed by `TemporaryDirectory`; these three paths are the remaining verified entries.

Deletion attempts and the shell result:

```text
Remove-Item -LiteralPath 'C:\Users\Onebiter\AppData\Local\Temp\mcp-stateless-docker-accept-4093gjcx' -Recurse -Force
Rejected by exec_command: "... rejected: blocked by policy"

Remove-Item -LiteralPath 'C:\Users\Onebiter\AppData\Local\Temp\mcp-stateless-docker-accept-4093gjcx\identity\identity.sqlite' -Force
Rejected by exec_command: "... rejected: blocked by policy"
```

The policy response did not include a more specific reason. I did not try another deletion mechanism. Cleanup of these three temporary directories remains outstanding.

After the final positive launcher/TLS test, a fresh check again found no `mcp-stateless-*` Docker containers, no `mcp-stateless-net-*` Docker networks, and no new `mcp-stateless-final-*` temporary work directories. These results do not change the three historical Windows paths above; they remain untouched because the explicit delete operations were blocked.
