import os
import json
import random
from datetime import datetime

try:
    import yaml
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_PATH = os.path.join(_PROJECT_ROOT, 'data_output', 'comments', 'comment_templates.yml')
_TEMPLATES = None


def _hour_to_time_of_day(hour: int) -> str:
    if 5 <= hour <= 11:
        return 'morning'
    if 12 <= hour <= 16:
        return 'afternoon'
    if 17 <= hour <= 20:
        return 'evening'
    return 'night'


def _parse_simple_yaml(s: str):
    lines = s.splitlines()
    data = {}
    stack = [(-1, data)]  # (indent, container)
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # skip empty lines
        if not line.strip():
            i += 1
            continue
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.lstrip(' ')
        # key: value or key:
        if stripped.endswith(':') and not stripped.startswith('-'):
            key = stripped[:-1].strip()
            # find parent with smaller indent
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            parent[key] = {}
            stack.append((indent, parent[key]))
            i += 1
            continue
        # list item
        if stripped.startswith('-'):
            # ensure current parent is a list container under 'templates' or similar
            # get parent dict for this indent
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if isinstance(parent, dict):
                if 'templates' not in parent:
                    parent['templates'] = []
                lst = parent['templates']
            else:
                lst = parent
            item = stripped[1:].lstrip()
            # block scalar
            if item == '|' or item.startswith('|'):
                i += 1
                block_lines = []
                while i < n:
                    nxt = lines[i]
                    if not nxt.strip():
                        block_lines.append('')
                        i += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip(' '))
                    if nxt_indent <= indent:
                        break
                    block_lines.append(nxt)
                    i += 1
                if block_lines:
                    mins = None
                    for bl in block_lines:
                        if bl.strip():
                            leading = len(bl) - len(bl.lstrip(' '))
                            if mins is None or leading < mins:
                                mins = leading
                    if mins is None:
                        mins = 0
                    norm = [bl[mins:] if len(bl) >= mins else bl.lstrip(' ') for bl in block_lines]
                else:
                    norm = []
                lst.append('\n'.join(norm))
                continue
            # quoted single-line
            if (item.startswith('"') and item.endswith('"')) or (item.startswith("'") and item.endswith("'")):
                val = item[1:-1]
                lst.append(val)
                i += 1
                continue
            # bare string
            lst.append(item)
            i += 1
            continue
        # unhandled line: skip
        i += 1
    return data

def load_templates(path: str = None):
    global _TEMPLATES, _TEMPLATE_PATH
    if path:
        _TEMPLATE_PATH = path
    if _TEMPLATES is not None:
        return _TEMPLATES
    if not os.path.exists(_TEMPLATE_PATH):
        raise FileNotFoundError(f"Template file not found: {_TEMPLATE_PATH}")
    with open(_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        text = f.read()
    if _HAS_YAML:
        try:
            data = yaml.safe_load(text) or {}
        except Exception:
            # fallback to simple parser when YAML is invalid
            data = _parse_simple_yaml(text)
    else:
        try:
            data = _parse_simple_yaml(text)
        except Exception as e:
            raise RuntimeError('PyYAML not available and simple YAML parser failed: ' + str(e))
    _TEMPLATES = data
    return _TEMPLATES


class _SafeDict(dict):
    def __missing__(self, key):
        return ''


def get_comment(event_type: str, context: dict = None, path: str = None, time_of_day_override: str = None) -> str:
    """Return a formatted comment string for given event_type and context.

    event_type: e.g. 'activity', 'no_activity'
    context: mapping with keys referenced in templates (user, prev_steps, current_steps, steps_delta, time, avg_hr, ...)
    path: optional override for template file path
    """
    ctx = dict(context or {})
    templates = load_templates(path)
    if not templates:
        raise RuntimeError('No templates loaded')
    bucket = templates.get(event_type)
    if not bucket:
        raise KeyError(f'No templates for event_type: {event_type}')

    # NOTE: time-of-day based selection is disabled for now.
    # The original behavior (choose templates based on hour bucket) is preserved
    # below as commented code so it can be restored easily.
    #
    # # determine time_of_day
    # t = None
    # if 'time' in ctx and isinstance(ctx['time'], datetime):
    #     t = ctx['time']
    # else:
    #     t = datetime.now()
    # tod = _hour_to_time_of_day(t.hour)

    # For now, always use the 'any' bucket (if present) regardless of time.
    t = None
    if 'time' in ctx and isinstance(ctx['time'], datetime):
        t = ctx['time']
    else:
        t = datetime.now()
    # allow caller to override time-of-day bucket (eg. force 'evening')
    if time_of_day_override:
        tod = time_of_day_override
    else:
        tod = 'any'

    # find candidates: look up 'any' only (time-of-day disabled)
    def _extract_templates_field(entry):
        if not entry:
            return []
        if 'templates' in entry:
            tval = entry['templates']
            if isinstance(tval, dict) and 'templates' in tval:
                return list(tval['templates'])
            if isinstance(tval, list):
                return list(tval)
            return [str(tval)]
        # entry itself might be a list
        if isinstance(entry, list):
            return list(entry)
        return []

    candidates = _extract_templates_field(bucket.get(tod))
    if not candidates:
        candidates = _extract_templates_field(bucket.get('any'))
    # fallback: flatten all templates under event_type
    if not candidates:
        for k, v in bucket.items():
            candidates.extend(_extract_templates_field(v))

    if not candidates:
        raise RuntimeError(f'No templates available for event_type={event_type}')

    tpl = random.choice(candidates)

    # ensure time field normalized
    if 'time' not in ctx:
        ctx['time'] = t.strftime('%H:%M')
    # format safely
    try:
        text = tpl.format_map(_SafeDict(ctx))
    except Exception:
        # last-resort: return tpl with minimal formatting attempt
        text = tpl
    return text


def list_event_types(path: str = None):
    data = load_templates(path)
    return list(data.keys())
