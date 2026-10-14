"""Validated cleanup of an interrupted isolated run, retaining redacted evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

base = Path(__file__).resolve().parent
path = Path(sys.argv[1]).resolve()
state = json.loads(path.read_text())
out = base / 'runs' / state['run_id']
freeze = json.loads((out / 'freeze.json').read_text())
runtime = Path(state['runtime_root']).resolve()
if (path.parent != base or not re.fullmatch(r'wenjin-concurrency-[0-9a-f]{8}', state['project'])
    or runtime != Path(tempfile.gettempdir()).resolve() / 'wenjin-concurrency-baseline' / state['project']
    or hashlib.sha256(path.read_bytes()).hexdigest() != freeze['state_sha256']):
    raise RuntimeError('failed_cleanup_identity_mismatch')
spec = importlib.util.spec_from_file_location('cleanup_helper', base / 'run_model_multilane_benchmark.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
compose = ['docker', 'compose', '--project-name', state['project'], '--project-directory', str(base.parents[2]), '--env-file', state['env_file']]
for item in state['compose_files']:
    compose.extend(['-f', item])
token = Path(state['model_secret_path']).read_text().strip()
helper.run_command(compose + ['stop', '-t', '120'], timeout=150)
helper.redact_logs_and_remove_secrets(state, out, token)
helper.run_command(compose + ['down', '--volumes', '--remove-orphans'], timeout=180)
remaining = helper.verify_no_project_resources(state['project'])
if any(remaining.get(k) != 0 for k in ('containers', 'volumes', 'networks')):
    raise RuntimeError('cleanup_resources_remaining_or_unknown')
shutil.rmtree(runtime)
path.unlink()
remaining['temporary_root_removed'] = not runtime.exists()
(out / 'cleanup.json').write_text(json.dumps(remaining, indent=2), encoding='utf-8')
(out / 'outcome.json').write_text(json.dumps({'passed': False, 'phase': state['phase'], 'lane_count': state['lane_count'], 'failure_type': 'experimental_startup_crash_loop', 'cause': 'Starlette_missing_middleware_decorator', 'performance_requests': 0, 'cleanup': remaining}, indent=2), encoding='utf-8')
print(json.dumps(remaining))
