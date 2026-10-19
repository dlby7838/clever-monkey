"""Independent structure, rounding, language and unreviewed content dimensions."""
import json
import math
import re
from .pipeline import validate

ROUNDING_TOLERANCE_SECONDS = 0.3


def assess_text(text, duration, tolerance=ROUNDING_TOLERANCE_SECONDS):
    result = {'json_valid': False, 'structure_valid': False, 'time_status': 'not_evaluated',
              'language_status': 'not_evaluated', 'content_status': 'not_reviewed', 'issues': []}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        result['issues'].append('invalid_json')
        return result
    result['json_valid'] = True
    if not isinstance(data, dict):
        result['issues'].append('structure_error:result_not_object')
        return result
    natural = [data.get(k) for k in ('summary', 'visual_description', 'shot_type', 'camera_motion')]
    actions = data.get('actions', [])
    natural += (actions if isinstance(actions, list) else [actions]) + [item.get('description') for item in (data.get('timeline') if isinstance(data.get('timeline'), list) else []) if isinstance(item, dict)]
    # OCR and proper names may legitimately preserve Latin originals.
    result['language_status'] = 'chinese' if all(isinstance(v, str) and re.search(r'[\u3400-\u9fff]', v) for v in natural if v) else 'requires_review'
    # Validate all required field types without conflating time geometry with structure.
    try:
        copy = dict(data)
        timeline = data.get('timeline')
        if not isinstance(timeline, list):
            raise ValueError('timeline_not_array')
        for item in timeline:
            if not isinstance(item, dict) or not isinstance(item.get('description'), str) or not item['description'].strip():
                raise ValueError('invalid_timeline_item')
            for name in ('start', 'end'):
                value = item.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError('invalid_or_missing_' + name)
        copy['timeline'] = []
        validate(copy, duration)
        result['structure_valid'] = True
    except (TypeError, ValueError, RuntimeError) as exc:
        result['issues'].append('structure_error:' + str(exc))
        # Other field errors do not prevent inspecting a well-formed timeline.
        if not isinstance(data, dict) or not isinstance(data.get('timeline'), list):
            return result
        for item in data['timeline']:
            if not isinstance(item, dict) or any(isinstance(item.get(k), bool) or not isinstance(item.get(k), (int, float)) or not math.isfinite(item[k]) for k in ('start','end')):
                return result
    previous = 0
    warnings = []
    errors = []
    for i, segment in enumerate(timeline):
        start, end = segment['start'], segment['end']
        if start >= end:
            errors.append({'segment': i, 'kind': 'nonpositive_interval'})
        if start < previous - 1e-6:
            errors.append({'segment': i, 'kind': 'overlap_or_negative_start'})
        if end > duration + 1e-6:
            delta = end - duration
            if i == len(timeline)-1 and start < duration and delta <= tolerance + 1e-6:
                warnings.append({'segment': i, 'kind': 'terminal_rounding', 'excess_seconds': round(delta, 6)})
            else:
                errors.append({'segment': i, 'kind': 'time_out_of_bounds', 'excess_seconds': round(delta, 6)})
        previous = end
    result['rounding_warnings'] = warnings
    result['time_errors'] = errors
    result['time_status'] = 'boundary_error' if errors else ('rounding_warning' if warnings else 'within_bounds')
    result['machine_usable'] = result['structure_valid'] and not errors and result['language_status'] == 'chinese'
    # Never infer action correctness from any of these mechanical checks.
    return result
