"""Remove credentials for a frozen prepare-only run; refuse any started resources."""
from pathlib import Path
import hashlib
import importlib.util
import json
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
    raise RuntimeError('prepare_cleanup_identity_mismatch')
spec = importlib.util.spec_from_file_location('cleanup_helper', base / 'run_model_multilane_benchmark.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
remaining = helper.verify_no_project_resources(state['project'])
if any(remaining.get(k) != 0 for k in ('containers', 'volumes', 'networks')):
    raise RuntimeError('prepare_cleanup_refuses_started_or_unknown_resources')
shutil.rmtree(runtime)
path.unlink()
remaining.update({'temporary_root_removed': not runtime.exists(), 'services_started': False,
                  'reason': 'prepared snapshot superseded before service start'})
(out / 'cleanup.json').write_text(json.dumps(remaining, indent=2), encoding='utf-8')
print(json.dumps(remaining))
