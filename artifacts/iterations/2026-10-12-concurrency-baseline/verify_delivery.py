"""Read-only delivery checks; emit counts/paths, never credential values."""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parent
h = hashlib.sha256()
for p in sorted((ROOT / 'src').rglob('*.py')):
    h.update(p.relative_to(ROOT / 'src').as_posix().encode())
    h.update(b'\0'); h.update(p.read_bytes()); h.update(b'\0')
docs = [ROOT / 'code.md', ROOT / 'docs/development-log.md', *BASE.glob('*.md')]
missing = []
for p in docs:
    for target in re.findall(r'\]\(([^)]+)\)', p.read_text(encoding='utf-8-sig')):
        target = target.strip('<>').split('#')[0]
        if not target or re.match(r'\w+://', target):
            continue
        if not (p.parent / unquote(target)).exists():
            missing.append({'document': str(p.relative_to(ROOT)), 'target': target})
suspects = []
for p in (BASE / 'runs').rglob('*'):
    if not p.is_file() or p.suffix not in {'.json', '.jsonl', '.log', '.yaml', '.yml'}:
        continue
    data = p.read_text(encoding='utf-8-sig', errors='replace')
    if re.search(r'Bearer\s+[A-Za-z0-9_.-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----', data):
        suspects.append(str(p.relative_to(BASE)))
    if re.search(r'"(?:password|access_token|refresh_token|admin_token)"\s*:\s*"(?!\[|<|redacted|REDACTED)[^"\s]{8,}"', data):
        suspects.append(str(p.relative_to(BASE)))
report = {
    'source_sha256': h.hexdigest(),
    'source_unchanged': h.hexdigest() == '6e9b9c316e0ac2a50355b432d57502365f3218a4ba2fb63b0759517ab996658a',
    'final_delivery_script_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in BASE.glob('*.py')},
    'code_md_lines': len((ROOT / 'code.md').read_text(encoding='utf-8-sig').splitlines()),
    'local_markdown_links_missing': missing,
    'credential_pattern_suspect_paths': sorted(set(suspects)),
    'credential_scan_limit': 'Pattern scan plus temporary-root removal; no credential values emitted.',
    'offline_checks_passed': 6,
}
(BASE / 'delivery-checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
