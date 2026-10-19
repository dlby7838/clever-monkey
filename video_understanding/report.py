"""Reproduce multi-model evaluation tables without sending any API requests."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import statistics
from nas_scanner.safety import local_output
from .evaluation import price_usage


def compile_reports(folders, output):
    output = local_output(output)
    output.mkdir(parents=True, exist_ok=True)
    cases, models, runs = [], {}, []
    fingerprints = {}
    for folder in folders:
        report = json.loads((folder / 'report.json').read_text())
        runs.append({'folder': str(folder), 'status': report['status'], 'stop_reason': report.get('stop_reason')})
        for attempt in report['attempts']:
            sample = report['samples'][attempt['sample']-1]
            model = next(m for m in report['models'] if m['model_id'] == attempt['requested_model'])
            models[model['model_id']] = model
            case = dict(attempt, source=sample['source_relative_path'], sha256=sample['sha256'])
            key = (sample['sha256'], model['model_id'])
            if any((c['sha256'], c['requested_model']) == key for c in cases):
                raise ValueError('Duplicate source/model pair across reports')
            if attempt.get('proxy_sha256'):
                previous = fingerprints.setdefault(sample['sha256'], attempt['proxy_sha256'])
                if previous != attempt['proxy_sha256']:
                    raise ValueError('Proxy mismatch across models')
            if attempt.get('usage'):
                index = report['models'].index(model)+1
                body = json.loads((folder / f'sample-{attempt["sample"]}-model-{index}.json').read_text())
                case['raw_output'] = body['choices'][0]['message']['content']
                try:
                    case['parsed'] = json.loads(case['raw_output'])
                except ValueError:
                    case['parsed'] = None
            cases.append(case)
    summary = []
    for model_id, model in models.items():
        attempts = [c for c in cases if c['requested_model'] == model_id]
        returned = [c for c in attempts if c.get('usage')]
        assessments = [c.get('assessment', {}) for c in returned]
        costs = [Decimal(c['list_price_cny']) for c in returned if c.get('list_price_cny') is not None]
        normalized = []
        for c in returned:
            usage = dict(c['usage'], prompt_tokens_details={'cached_tokens': 0})
            value = price_usage(usage, model, 'default')
            if value is not None:
                normalized.append(Decimal(value))
        summary.append({'model':model['name'], 'model_id':model_id, 'attempted':len(attempts), 'returned':len(returned),
                        'machine_pass':sum(bool(a.get('machine_usable')) for a in assessments),
                        'structure_valid':sum(bool(a.get('structure_valid')) for a in assessments),
                        'json_valid':sum(bool(a.get('json_valid')) for a in assessments),
                        'chinese':sum(a.get('language_status') == 'chinese' for a in assessments),
                        'time_status':dict(Counter(a.get('time_status','unavailable') for a in assessments)),
                        'known_cost_cny':str(sum(costs, Decimal(0))), 'priced':len(costs),
                        'no_cache_standard_cost_cny':str(sum(normalized, Decimal(0))),
                        'cached_input_tokens':sum(c['usage'].get('prompt_tokens_details',{}).get('cached_tokens',0) for c in returned),
                        'prompt_tokens':sum(c['usage']['prompt_tokens'] for c in returned),
                        'completion_tokens':sum(c['usage']['completion_tokens'] for c in returned),
                        'mean_response_seconds':statistics.mean(c['elapsed_seconds'] for c in returned) if returned else None,
                        'content_accuracy':None})
    result = {'runs':runs, 'summary':summary, 'same_proxy_fingerprints':True, 'cases':cases}
    (output / 'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    lines = ['# 原始模型返回内容', '', '机械检查不代表内容准确率。保留原文，不自动纠错。', '']
    briefs = ['# 每条素材三模型摘要', '']
    for sha in dict.fromkeys(c['sha256'] for c in cases):
        group = [c for c in cases if c['sha256'] == sha]
        title = group[0]['source']
        lines += ['## '+title, '']
        briefs += ['## '+title, '']
        for c in group:
            lines += ['### '+c['model'], '', '```json', c.get('raw_output',c['status']), '```', '']
            parsed = c.get('parsed')
            brief = parsed.get('summary','缺少summary') if isinstance(parsed,dict) else c['status']
            briefs += [c['model']+'：'+brief, '']
    (output/'MODEL_OUTPUTS.md').write_text('\n'.join(lines))
    (output/'SUMMARIES.md').write_text('\n'.join(briefs))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=compile_reports(args.reports,args.output)
    print(json.dumps(result['summary'],ensure_ascii=False,indent=2))
