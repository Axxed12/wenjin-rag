"""Read-only saved-evidence review by root; no service or request generation."""
import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import independent_compare_runs as independent
import model_http_stage as model_stage

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[2]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def resource_summary(run):
    gpu = lines(run / 'resources-gpu-host.jsonl')
    docker = lines(run / 'resources-docker.jsonl')
    def gaps(rows):
        return independent.percentiles([(b['monotonic'] - a['monotonic']) * 1000 for a, b in zip(rows, rows[1:])])
    containers = [c for row in docker for c in row.get('containers', [])]
    model = [c for c in containers if 'model-node' in c.get('Name', '')]
    def memory(value):
        parts = value.split(' / ')[0].split()
        raw = value.split(' / ')[0].strip()
        import re
        match = re.fullmatch(r'([0-9.]+)([A-Za-z]+)', raw)
        if match:
            return float(match[1]) * {'B': 1 / 1048576, 'KiB': 1/1024, 'MiB': 1, 'GiB': 1024}.get(match[2], math.nan)
        return None if not parts else math.nan
    memories = [memory(c['MemUsage']) for c in model]
    return {'gpu_host_samples': len(gpu), 'gpu_host_actual_interval_ms': gaps(gpu),
            'docker_samples': len(docker), 'docker_actual_interval_ms': gaps(docker),
            'min_gpu_free_mib': min(r['gpu']['free_mib'] for r in gpu),
            'max_gpu_used_mib': max(r['gpu']['total_mib']-r['gpu']['free_mib'] for r in gpu),
            'max_gpu_utilization_percent': max(r['gpu']['utilization_percent'] for r in gpu),
            'min_host_free_mib': min(r['host_memory_free_mib'] for r in gpu),
            'max_model_cpu_percent': max((float(c['CPUPerc'].rstrip('%')) for c in model), default=None),
            'max_model_container_memory_mib': max((m for m in memories if m is not None and math.isfinite(m)), default=None)}


def mcp_summary(run):
    summary = read(run / 'mcp-summary.json')
    rows = lines(run / 'mcp-requests.jsonl')
    perf = [r for r in rows if r['tag'].startswith('mixed-round-')]
    elapsed = sum(r['duration_seconds'] for r in summary['repetitions'])
    output = {'run': run.name, 'outcome': read(run / 'outcome.json'), 'total_tool_calls': len(rows),
              'http_statuses': dict(Counter(str(r['http_status']) for r in rows)),
              'expected_mcp_errors': sum(r['denied'] for r in rows),
              'unexpected_failures': sum(not r['passed'] for r in rows),
              'performance_requests': len(perf), 'performance_duration_seconds': elapsed,
              'successful_rps': sum(r['passed'] for r in perf) / elapsed,
              'client_e2e_ms': independent.percentiles([r['elapsed_seconds']*1000 for r in perf]),
              'success_e2e_ms': independent.percentiles([r['elapsed_seconds']*1000 for r in perf if r['passed']]),
              'failure_e2e_ms': independent.percentiles([r['elapsed_seconds']*1000 for r in perf if not r['passed']]),
              'resources': resource_summary(run), 'model_queue_per_parent_mcp': '未测得',
              'model_execution_per_parent_mcp': '未测得', 'repetitions': summary['repetitions'],
              'backend_hashes': [a['sha256'] for a in read(run / 'outcome.json')['backend_attestation']],
              'labels': {}, 'fixtures': summary['fixtures'], 'competition': []}
    for label in ('shared', 'managed'):
        chosen = [r for r in perf if r['label']==label]
        output['labels'][label] = {'requests': len(chosen), 'client_e2e_ms': independent.percentiles([r['elapsed_seconds']*1000 for r in chosen]),
            'retrieval_counts': dict(Counter(str({k:r['tool']['retrieval'].get(k) for k in ('dense_returned','bm25_returned','fused_candidates','reranker_used')}) for r in chosen))}
    events = lines(run / 'model-events.jsonl')
    kinds = {e['request_id']:e['operation_kind'] for e in events if e['event']=='operation_enter'}
    admitted = set(kinds)
    metrics = model_stage._analyze_server_events(events, admitted, kinds)
    output['all_model_ops'] = {'count_by_kind':dict(Counter(kinds.values())), 'cuda_peak_lanes':metrics['max_simultaneous_lanes_in_forward_intervals'],
                             'cross_lane_pairs':metrics['cross_lane_cuda_interval_overlap_pair_count'], 'cross_model_request_pairs':metrics['mixed_embedding_rerank_cuda_overlap_pair_count']}
    by_id = defaultdict(lambda:defaultdict(list))
    for e in events:
        by_id[e.get('request_id','')][e['event']].append(e)
    for name, begin, end in [('queue_wait_ms','admission_enqueued','worker_dequeued'),('execution_ms','operation_enter','operation_exit')]:
        output['all_model_ops'][name] = independent.percentiles([v for rid in admitted if (v:=independent.event_interval_ms(by_id,rid,begin,end)) is not None])
    for cohort in summary['query_ingest']:
        begin,end = datetime.fromisoformat(cohort['begin_utc']),datetime.fromisoformat(cohort['end_utc'])
        ids = {e['request_id'] for e in events if e['event']=='request_received' and begin <= datetime.fromisoformat(e['time_utc']) <= end and e['request_id'] in admitted}
        measured = model_stage._analyze_server_events(events,ids,kinds)
        pairs = [pair for pair in measured['overlap_pairs'] if (kinds[pair['left_request_id']]=='embedding:document') != (kinds[pair['right_request_id']]=='embedding:document')]
        output['competition'].append({'time_window_begin_utc':cohort['begin_utc'], 'time_window_end_utc':cohort['end_utc'],
            'scope':'model request UUID + server events; parent attribution by time window only', 'ops':dict(Counter(kinds[rid] for rid in ids)),
            'query_ingest_cuda_overlap_forward_pairs':len(pairs), 'peak_lanes':measured['max_simultaneous_lanes_in_forward_intervals'],
            'post_ingest_readback_passed':cohort['readback']['passed']})
    return output


def main():
    result = {'authored_by':'root; API comparison algorithm authored by independent Luna Agent', 'mcp2_independent_agent_signoff':'未完成：独立子Agent额度限制；root复算与审核', 'runs':{}, 'mcp':[], 'boundary':[]}
    for run in sorted((BASE/'runs').iterdir()):
        freeze = read(run/'freeze.json')
        valid = all(hashlib.sha256((run/'frozen'/name).read_bytes()).hexdigest()==sha for name,sha in freeze['files_sha256'].items())
        result['runs'][run.name] = {'frozen_files_hash_match':valid,'frozen_file_count':len(freeze['files_sha256']),'cleanup':read(run/'cleanup.json'),'outcome_pass':read(run/'outcome.json').get('passed') if (run/'outcome.json').exists() else None}
    for name in ('20261012T130402Z-ddb5c7','20261012T130745Z-6ff9dd'):
        result['mcp'].append(mcp_summary(BASE/'runs'/name))
    for name in ('20261012T124331Z-e77826','20261012T125116Z-ee4f61'):
        run = BASE/'runs'/name
        rows = lines(run/'boundary-requests.jsonl')
        span = max(r['begin_monotonic']+r['elapsed_seconds'] for r in rows)-min(r['begin_monotonic'] for r in rows)
        by_id = defaultdict(lambda:defaultdict(list))
        for event in lines(run/'model-events.jsonl'):
            by_id[event.get('request_id','')][event['event']].append(event)
        request_timings = []
        for row in rows:
            rid = row['request_id']
            end_event = next((event for event in ('worker_dequeued','queue_timeout','queued_cancel') if by_id[rid].get(event)), None)
            request_timings.append({'request_id':rid,'status':row['status'],'client_terminal_ms':row['elapsed_seconds']*1000,
                'queue_end_event':end_event, 'queue_ms':independent.event_interval_ms(by_id,rid,'admission_enqueued',end_event) if end_event else '未测得：未进入队列',
                'execution_ms':independent.event_interval_ms(by_id,rid,'operation_enter','operation_exit') if by_id[rid].get('operation_enter') else '未测得：未执行'})
        result['boundary'].append({'run':name, 'scope':'intentional real HTTP boundaries; CPU holds are not inference performance',
            'request_count':len(rows), 'client_observation_duration_seconds':span,'terminal_requests_per_second':len(rows)/span,
            'status_counts':dict(Counter(str(r['status']) for r in rows)),
            'error_counts':dict(Counter(r['body'].get('error','none') if isinstance(r.get('body'),dict) else r.get('error','none') for r in rows)),
            'all_client_terminal_ms':independent.percentiles([r['elapsed_seconds']*1000 for r in rows]),
            'http200_client_terminal_ms':independent.percentiles([r['elapsed_seconds']*1000 for r in rows if r['status']==200]),
            'expected_rejection_or_cancel_client_terminal_ms':independent.percentiles([r['elapsed_seconds']*1000 for r in rows if r['status']!=200]),
            'queue_ms_direct':independent.percentiles([r['queue_ms'] for r in request_timings if isinstance(r['queue_ms'],(int,float))]),
            'execution_ms_direct':independent.percentiles([r['execution_ms'] for r in request_timings if isinstance(r['execution_ms'],(int,float))]),
            'request_timings':request_timings,
            'checks':{k:v['passed'] for k,v in read(run/'boundary-summary.json')['checks'].items()}})
    result['api'] = [independent.load_run(BASE/'runs'/name) for name in ('20261012T124501Z-fe800f','20261012T124736Z-eb9321')]
    result['api_embedding_subsets'] = []
    for name in ('20261012T124501Z-fe800f','20261012T124736Z-eb9321'):
        run = BASE/'runs'/name
        rows = lines(run/'client-requests.jsonl')
        by_id = defaultdict(lambda:defaultdict(list))
        for event in lines(run/'model-events.jsonl'):
            by_id[event.get('request_id','')][event['event']].append(event)
        subsets = {'run':name}
        for kind in ('query','document'):
            chosen = [r for r in rows if r['benchmark_family']=='pure_embedding' and r['group'].startswith('embedding_'+kind)]
            span = (max(r['response_completed_monotonic_ns'] for r in chosen)-min(r['request_started_monotonic_ns'] for r in chosen))/1e9
            stats = {'request_n':len(chosen), 'phase_duration_seconds':span, 'successful_rps':len(chosen)/span, 'client_e2e_ms':independent.percentiles([r['duration_ms'] for r in chosen])}
            for metric, begin, end in [('queue_ms','admission_enqueued','worker_dequeued'),('execution_ms','operation_enter','operation_exit')]:
                stats[metric] = independent.percentiles([v for r in chosen if (v:=independent.event_interval_ms(by_id,r['request_id'],begin,end)) is not None])
            subsets[kind] = stats
        result['api_embedding_subsets'].append(subsets)
    result['api_resources'] = [resource_summary(BASE/'runs'/name) for name in ('20261012T124501Z-fe800f','20261012T124736Z-eb9321')]
    before = read(BASE/'business-files-before.json')
    result['business_files_checked'] = len(before)
    result['business_files_changed'] = [name for name, sha in before.items() if not (REPO/name).exists() or hashlib.sha256((REPO/name).read_bytes()).hexdigest()!=sha]
    current = [json.loads(line) for line in subprocess.check_output(['docker','ps','--format','json'],text=True).splitlines()]
    result['original_container_ids_still_running'] = all(c['ID'] in {row['ID'] for row in current} for c in read(BASE/'preflight.json')['running_containers'])
    result['remaining_test_projects'] = [r['Names'] for r in current if 'wenjin-concurrency-' in r['Names']]
    result['remaining_state_credentials_files'] = [p.name for p in BASE.glob('run-state.*.json')]
    (BASE/'final-evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'business_changed':result['business_files_changed'],'original_containers_running':result['original_container_ids_still_running'],'mcp':[{k:r[k] for k in ('run','performance_requests','successful_rps','client_e2e_ms','all_model_ops','competition')} for r in result['mcp']]},indent=2))


if __name__=='__main__':
    main()
