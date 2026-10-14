"""Extract exact logical payloads from frozen generators and verify captured hashes."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

base = Path(__file__).resolve().parent
checks = []
for run in sorted((base / 'runs').iterdir()):
    requests = run / 'client-requests.jsonl'
    identity_file = run / 'model-identity.json'
    generator = run / 'frozen/harness/model_http_stage.py'
    if not requests.exists() or not identity_file.exists():
        continue
    name = 'frozen_input_' + run.name.replace('-', '_')
    spec = importlib.util.spec_from_file_location(name, generator)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    identity = json.loads(identity_file.read_text())
    payloads = {key: module._request_payload(group, identity) for key, group in module._workload().items()}
    digests = {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for key, value in payloads.items()}
    rows = [json.loads(line) for line in requests.read_text().splitlines() if line.strip()]
    verified = all(row['input_sha256'] == digests[row['group']] for row in rows)
    if not verified:
        raise RuntimeError('frozen_payload_hash_mismatch:' + run.name)
    (run / 'inputs-and-model-payloads.json').write_text(json.dumps({'provenance': 'exact logical payloads extracted from frozen generator; captured request hashes verified', 'generator_sha256': hashlib.sha256(generator.read_bytes()).hexdigest(), 'request_count_verified': len(rows), 'payloads': payloads, 'input_sha256': digests}, indent=2), encoding='utf-8')
    checks.append({'run': run.name, 'request_count': len(rows), 'verified': verified})
(base / 'input-preservation-check.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
print(json.dumps(checks))
