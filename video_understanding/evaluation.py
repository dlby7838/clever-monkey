"""Explicit small paid evaluation, with per-attempt usage records and no retries."""
import argparse
import base64
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from nas_scanner.safety import local_output
from nas_scanner.scanner import probe
from .config import load_local_config
from .pipeline import PROMPT, PROMPT_VERSION, validate, visual_duration
from .preprocess import PROFILE, compressed_video

ENDPOINT = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'


def price_usage(usage, model, tier='default'):
    """Public list-price estimate; missing accounting is never silently zero."""
    if tier not in ('default', 'flex') or not isinstance(usage, dict):
        return None
    prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (prompt, completion)):
        return None
    details = usage.get('prompt_tokens_details') or {}
    cached = details.get('cached_tokens', 0)
    if not isinstance(cached, int) or not 0 <= cached <= prompt:
        return None
    factor = Decimal(1)
    if '2-0-' in model['model_id']:
        if prompt > 256000:
            return None
        if prompt > 128000:
            factor = Decimal(4) if 'mini' in model['model_id'] else Decimal(3)
        elif prompt > 32000:
            factor = Decimal(2) if 'mini' in model['model_id'] else Decimal('1.5')
    in_rate = Decimal(str(model['input_price'])) * factor
    out_rate = Decimal(str(model['output_price'])) * factor
    # Catalog's compared models all use 20% of uncached input list price for cache hits.
    discount = Decimal('0.5') if tier == 'flex' else Decimal(1)
    cost = ((prompt - cached) * in_rate * discount + cached * in_rate * Decimal('0.2') + completion * out_rate * discount) / 1000000
    return str(cost)


def request_response(video, model, timeout=600, service_tier='flex'):
    raw = video.read_bytes()
    if len(raw) > 40 * 1024 * 1024:
        raise ValueError('Proxy exceeds 40 MiB')
    payload = {'model': model, 'messages': [{'role': 'user', 'content': [
        {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + base64.b64encode(raw).decode(), 'fps': 1}},
        {'type': 'text', 'text': PROMPT}]}], 'response_format': {'type': 'json_object'},
        'max_tokens': 2000, 'thinking': {'type': 'disabled'}, 'service_tier': service_tier}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                               headers={'Authorization': 'Bearer ' + os.environ['ARK_API_KEY'], 'Content-Type': 'application/json',
                                        'X-Ark-Max-Wait-Timeout-Ms': str(max(1000, min(1200000, int((timeout-10)*1000))))})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
            return {'http_status': response.status, 'body': body,
                    'request_id': response.headers.get('x-request-id')}
    except urllib.error.HTTPError as exc:
        code = None
        try:
            error = json.loads(exc.read(65536)).get('error', {})
            code = error.get('code') if isinstance(error, dict) else None
        except (ValueError, AttributeError):
            pass
        return {'http_status': exc.code, 'error_code': code,
                'request_id': exc.headers.get('x-request-id')}


def run(testset, output, catalog_path, resume=False, service_tier='flex'):
    if service_tier not in ('flex', 'default'):
        raise ValueError('Unsupported service tier')
    load_local_config()
    if not os.environ.get('ARK_API_KEY'):
        raise ValueError('ARK_API_KEY missing')
    testset = testset.resolve()
    output = local_output(output)
    output.mkdir(parents=True, exist_ok=resume)  # Never overwrite or silently replay a paid run.
    manifest = json.loads((testset / 'manifest.json').read_text())
    rows = manifest['files']
    needles = ('袋中展示/', 'R搅拌酸奶/', 'R包装配料营养成分gi/', 'A模特拿产品展示/', '沉浸式倒牛奶/')
    selected = [next(r for r in rows if needle in r['source_relative_path']) for needle in needles]
    models = [m for m in json.loads(catalog_path.read_text())['models'] if m['role'] != 'historical_reference_optional']
    report = {'status': 'running', 'planned_requests': 25, 'prompt_version': PROMPT_VERSION,
              'compression_profile': PROFILE, 'fps': 1, 'thinking': 'disabled', 'service_tier': service_tier,
              'sample_selection': 'fixed coverage pilot, not representative of all 100 clips',
              'samples': selected, 'models': models, 'attempts': [], 'known_list_price_cny': '0',
              'billing_note': 'Usage × dated public rates; not an account invoice. Failed/time-out charges may be unknown.'}
    if resume:
        report = json.loads((output / 'report.json').read_text())
        if report.get('service_tier', 'default') != service_tier:
            raise ValueError('Cannot change service tier while resuming a run')
        if report['samples'] != selected or report['models'] != models:
            raise ValueError('Resume configuration differs from the recorded run')
        if any(a['status'] == 'sent' or a['status'] == 'transport_error_cost_unknown' for a in report['attempts']):
            raise ValueError('Ambiguous billed attempt; automatic replay forbidden')
        report['status'] = 'running'
        report.pop('stop_reason', None)
    def save():
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    save()
    skip = {a['requested_model'] for a in report['attempts'] if a.get('http_status') in (400,403,404)}
    attempted = {(a['sample'], a['requested_model']) for a in report['attempts']}
    stop = False
    try:
        for sample_no, sample in enumerate(selected, 1):
            if all(m['model_id'] in skip or (sample_no, m['model_id']) in attempted for m in models):
                continue
            video = testset / sample['local_relative_path']
            digest = hashlib.sha256()
            with video.open('rb') as source:
                for block in iter(lambda: source.read(1024*1024), b''):
                    digest.update(block)
            if digest.hexdigest() != sample['sha256']:
                raise ValueError('Local source fingerprint changed')
            print(f'Preparing sample {sample_no}/5 ({sample["metadata"]["duration"]} seconds)', flush=True)
            with compressed_video(video, timeout=300, temp_dir=output) as proxy:
                metadata = probe(proxy, 'ffprobe', 30)
                duration = visual_duration(metadata)
                source_metadata = probe(video, 'ffprobe', 30)
                if abs(duration - visual_duration(source_metadata)) > .25:
                    raise ValueError('Proxy timeline changed')
                proxy_sha = hashlib.sha256(proxy.read_bytes()).hexdigest()
                for model_no, model in enumerate(models):
                    if model['model_id'] in skip or (sample_no, model['model_id']) in attempted:
                        continue
                    # Soft safety stop after received usage; no automatic retries on unknown-cost errors.
                    if Decimal(report['known_list_price_cny']) >= Decimal('2'):
                        report['stop_reason'] = 'Reached CNY 2 received-usage safety threshold'
                        stop = True
                        break
                    attempt = {'sample': sample_no, 'model': model['name'], 'requested_model': model['model_id'],
                               'duration_seconds': duration, 'proxy_bytes': proxy.stat().st_size, 'proxy_sha256': proxy_sha,
                               'status': 'sent', 'requested_service_tier': service_tier, 'started_unix': time.time()}
                    report['attempts'].append(attempt)
                    save()  # Persist intent before network call, including crash/interrupt ambiguity.
                    started = time.monotonic()
                    try:
                        response = request_response(proxy, model['model_id'], service_tier=service_tier)
                        attempt['elapsed_seconds'] = round(time.monotonic() - started, 3)
                        attempt.update({k: v for k, v in response.items() if k != 'body'})
                        body = response.get('body')
                        if body is not None:
                            (output / f'sample-{sample_no}-model-{model_no+1}.json').write_text(json.dumps(body, ensure_ascii=False, indent=2))
                            usage = body.get('usage')
                            attempt.update(usage=usage, returned_model=body.get('model'), response_id=body.get('id'),
                                           service_tier=body.get('service_tier', service_tier))
                            if attempt['service_tier'] != service_tier:
                                stop = True
                                report['stop_reason'] = 'Returned service tier differs from requested tier; stopping'
                            cost = price_usage(usage, model, attempt['service_tier'])
                            attempt['list_price_cny'] = cost
                            if cost is not None:
                                report['known_list_price_cny'] = str(Decimal(report['known_list_price_cny']) + Decimal(cost))
                            else:
                                stop = True
                                report['stop_reason'] = 'Cannot price returned usage/tier; stopped to avoid untracked cost'
                            try:
                                choice = body['choices'][0]
                                attempt['finish_reason'] = choice.get('finish_reason')
                                if choice.get('finish_reason') != 'stop':
                                    raise ValueError('Response truncated or not complete')
                                validate(json.loads(choice['message']['content']), duration)
                                attempt['status'] = 'schema_valid'
                            except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
                                attempt['status'] = 'schema_invalid'
                                attempt['validation_error_type'] = type(exc).__name__
                        else:
                            attempt['status'] = 'http_error'
                            if response['http_status'] in (401, 402):
                                stop = True
                                report['stop_reason'] = 'Authentication/payment failure; no further requests sent'
                            elif response['http_status'] in (400, 403, 404):
                                skip.add(model['model_id'])
                    except Exception as exc:
                        attempt['status'] = 'transport_error_cost_unknown'
                        attempt['error_type'] = type(exc).__name__
                        attempt['elapsed_seconds'] = round(time.monotonic() - started, 3)
                        stop = True
                        report['stop_reason'] = 'Transport failure with unknown billing; no automatic retry'
                    save()
                    print(json.dumps({'sample': sample_no, 'model': model['name'], 'status': attempt['status'],
                                      'usage': attempt.get('usage'), 'list_price_cny': attempt.get('list_price_cny'),
                                      'http_status': attempt.get('http_status'), 'error_code': attempt.get('error_code')}, ensure_ascii=False), flush=True)
                    if stop:
                        break
            if stop:
                break
        report['status'] = 'stopped' if stop else 'finished'
    finally:
        report['temporary_proxies_remaining'] = len(list(output.glob('ai-video-p1-*')))
        save()
    print(json.dumps({'status': report['status'], 'requests_sent': len(report['attempts']),
                      'known_list_price_cny': report['known_list_price_cny'], 'stop_reason': report.get('stop_reason')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--testset', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--service-tier', choices=['flex','default'], default='flex')
    parser.add_argument('--resume', action='store_true', help='Resume known completed attempts without repeating requests')
    parser.add_argument('--catalog', type=Path, default=Path('docs/evaluation/model-catalog-2026-10-19.json'))
    args = parser.parse_args()
    run(args.testset, args.output, args.catalog, args.resume, args.service_tier)
