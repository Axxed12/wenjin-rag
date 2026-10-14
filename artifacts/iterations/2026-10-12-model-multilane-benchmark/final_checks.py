"""Final local links, artifact hashes, credential redaction and AST checks."""
import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

base = Path(__file__).resolve().parent
repo = base.parents[2]
checks = {'missing_links': [], 'unredacted_secret_fields': [], 'ast_files': 0, 'frozen_hash_failures': [], 'cleanup_failures': []}
for run in sorted((base/'runs').iterdir()):
    freeze = json.loads((run/'freeze.json').read_text())
    prepare = ['uv','run','--no-sync','python','artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py','prepare','--lanes',str(freeze['lanes']),'--phase',freeze['phase'],'--rounds',str(freeze['rounds'])]
    if freeze.get('reference_source'):
        prepare += ['--reference',freeze['reference_source']]
    state = 'artifacts/iterations/2026-10-12-model-multilane-benchmark/run-state.'+run.name+'.json'
    commands = {'cwd':str(repo),'prepare_argv':prepare,'run_argv':['uv','run','--no-sync','python',prepare[4],'run','--state',state], 'provenance':'invocations recorded from frozen phase/lanes/rounds/reference metadata; no credential arguments'}
    cleanup = json.loads((run/'cleanup.json').read_text())
    if cleanup.get('services_started') is False:
        commands['run_argv'] = None
        commands['manual_cleanup_argv'] = ['uv','run','--no-sync','python','artifacts/iterations/2026-10-12-model-multilane-benchmark/cleanup_prepared.py',state]
    elif run.name=='20261012T121422Z-b42d0b':
        commands['manual_cleanup_argv'] = ['uv','run','--no-sync','python','artifacts/iterations/2026-10-12-model-multilane-benchmark/cleanup_failed.py',state]
    (run/'commands.json').write_text(json.dumps(commands,indent=2),encoding='utf-8')
    for name, expected in freeze['files_sha256'].items():
        if hashlib.sha256((run/'frozen'/name).read_bytes()).hexdigest()!=expected:
            checks['frozen_hash_failures'].append(run.name+'/'+name)
    if any(cleanup.get(k)!=0 for k in ('containers','volumes','networks')) or not cleanup.get('temporary_root_removed'):
        checks['cleanup_failures'].append(run.name)
for path in [*base.glob('*.py'),*(base/'experimental-source').rglob('*.py')]:
    ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
    checks['ast_files'] += 1
mdfiles = [repo/'code.md',repo/'docs/development-log.md',base/'final-report.md',base/'development-log.md',base/'independent-acceptance-report.md']
for path in mdfiles:
    for target in re.findall(r'(?<!!)\[[^\]]*\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
        target = unquote(target.strip('<>')).split('#')[0]
        if not target or re.match(r'[a-z]+://',target):
            continue
        target = re.sub(r':\d+$','',target)
        if not (path.parent/target).exists():
            checks['missing_links'].append({'file':str(path),'target':target})
for path in base.rglob('*.json'):
    text = path.read_text(encoding='utf-8')
    for match in re.finditer(r'"(access_token|refresh_token|mcp_token|password|private_key)"\s*:\s*"([^"\n]*)"',text,re.I):
        value = match[2].lower()
        if value and 'redact' not in value and value not in {'none','null','***'}:
            checks['unredacted_secret_fields'].append({'file':str(path),'key':match[1]})
    if '-----BEGIN PRIVATE KEY-----' in text or '-----BEGIN RSA PRIVATE KEY-----' in text:
        checks['unredacted_secret_fields'].append({'file':str(path),'key':'PEM private key'})
checks['code_md_lines'] = len((repo/'code.md').read_text().splitlines())
checks['remaining_state_files'] = [p.name for p in base.glob('run-state.*.json')]
checks['passed'] = not any(checks[k] for k in ('missing_links','unredacted_secret_fields','frozen_hash_failures','cleanup_failures','remaining_state_files')) and checks['code_md_lines']<=120
(base/'final-checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print(json.dumps(checks,indent=2))
if not checks['passed']:
    raise SystemExit(1)
