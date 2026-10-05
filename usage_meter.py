"""Read provider counters only. No tokenizer guesses, network or account credentials."""
import datetime as dt
import json

PRICE = {'provider': 'opencode-go', 'model': 'deepseek-v4.1-flash',
         'currency': 'USD', 'checked': '2026-10-05', 'source': 'https://opencode.ai/docs/go/',
         'off_peak_per_million': {'inputTokens': .15, 'outputTokens': .60, 'cacheReadTokens': .003},
         'peak_multiplier': 2, 'go_monthly_limit': 60, 'go_plus_monthly_limit': 120}
FIELDS = ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens', 'totalTokens')


def rows(path):
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except (OSError, UnicodeError):
        return


def counter(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def parse_time(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


def multiplier(start, end):
    """Range if request crosses a peak boundary, or a start timestamp is missing."""
    a, b = parse_time(start), parse_time(end)
    if not a or not b or b < a or b - a > dt.timedelta(days=1):
        return 1, 2
    def peak(t):
        return 2 if t.weekday() < 5 and (1 <= t.hour < 4 or 6 <= t.hour < 10) else 1
    values = {peak(a), peak(b)}
    point = a.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while point <= b:
        values.add(peak(point))
        point += dt.timedelta(hours=1)
    return min(values), max(values)


def summarize(path, state, plan=None):
    tokens, occurrences, missing, seen, field_samples = {}, 0, 0, set(), {}
    low = high = 0.0
    unpriced = 0
    native = state.get('backend') == 'desktop'
    capture = False
    for row in rows(path / 'events.jsonl'):
        if row.get('type') == 'usage_capture':
            capture = True
        is_call = row.get('type') == 'usage'
        is_step = not native and row.get('type') == 'status' and row.get('phase') == 'step_end'
        if not (is_call or is_step):
            continue
        identity = ('call', row['seq']) if is_call and type(row.get('seq')) is int else None
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        usage = row.get('usage')
        if not isinstance(usage, dict) or not all(counter(usage.get(k)) for k in FIELDS[:2]):
            missing += 1
            continue
        occurrences += 1
        for key in FIELDS:
            if counter(usage.get(key)):
                tokens[key] = tokens.get(key, 0) + usage[key]
                field_samples[key] = field_samples.get(key, 0) + 1
        provider, model = row.get('provider', state.get('provider')), row.get('model', state.get('model'))
        # Harness inputTokens is UNCACHED input; cache/reasoning are never added twice.
        if (provider != PRICE['provider'] or model != PRICE['model'] or
                not counter(usage.get('cacheReadTokens')) or not counter(usage.get('cacheWriteTokens', 0)) or usage.get('cacheWriteTokens', 0) != 0):
            unpriced += 1
            continue
        amount = sum(usage[k] * rate for k, rate in PRICE['off_peak_per_million'].items()) / 1_000_000
        lo, hi = multiplier(row.get('started'), row.get('time'))
        low += amount * lo
        high += amount * hi
    coverage = 'unavailable' if not occurrences else 'partial' if missing else 'reported'
    cost_status = 'unavailable' if not occurrences or unpriced == occurrences else 'partial' if missing or unpriced else 'estimated'
    result = {'coverage': coverage, 'tokens': tokens, 'samples_reported': occurrences, 'samples_missing': missing,
              'partial_fields': [k for k in tokens if field_samples[k] < occurrences],
              'scope': 'delegated_main_session', 'capture_enabled': capture,
              'note': '提供方已报告的主会话用量；含已报告的重试/压缩，不含客户端接手后、标题生成或子会话。可选计数缺失时不代表零。',
              'cost': {'status': cost_status, 'usd_min': round(low, 9) if cost_status != 'unavailable' else None,
                       'usd_max': round(high, 9) if cost_status != 'unavailable' else None,
                       'unpriced_samples': unpriced, 'pricing': PRICE,
                       'note': '等值用量估算，不是额外扣款、账单或账户剩余额度；价格为日期快照，跨峰谷/开始时间未知时给出区间。'}}
    add_quota(result, plan)
    return result


def add_quota(result, plan):
    if plan in ('go', 'go-plus') and result['cost']['status'] != 'unavailable':
        low, high = result['cost']['usd_min'], result['cost']['usd_max']
        monthly = PRICE['go_monthly_limit' if plan == 'go' else 'go_plus_monthly_limit']
        result['quota_contribution'] = {'plan': plan, 'account_remaining_known': False,
            'windows': {name: {'limit_usd': monthly * fraction,
                'percent_min': round(low / (monthly * fraction) * 100, 6),
                'percent_max': round(high / (monthly * fraction) * 100, 6)}
                for name, fraction in (('five_hour', .2), ('weekly', .5), ('monthly', 1))}}


def combine(values, plan):
    """Whole fixed assignment, including prior rounds; unknown rounds stay unknown."""
    available = [v for v in values if v['coverage'] != 'unavailable']
    priced = [v for v in values if v['cost']['status'] != 'unavailable']
    tokens = {}
    for value in available:
        for key, count in value['tokens'].items():
            tokens[key] = tokens.get(key, 0) + count
    partial_fields = [k for k in tokens if any(k not in v['tokens'] or k in v['partial_fields'] for v in values)]
    result = {'coverage':'unavailable' if not available else 'reported' if all(v['coverage']=='reported' for v in values) else 'partial',
        'tokens':tokens, 'partial_fields':partial_fields, 'rounds':len(values),
        'samples_reported':sum(v['samples_reported'] for v in values),
        'samples_missing':sum(v['samples_missing'] for v in values),
        'cost':{'status':'unavailable' if not priced else 'estimated' if all(v['cost']['status']=='estimated' for v in values) else 'partial',
            'usd_min':round(sum(v['cost']['usd_min'] for v in priced),9) if priced else None,
            'usd_max':round(sum(v['cost']['usd_max'] for v in priced),9) if priced else None}}
    add_quota(result,plan)
    return result


def brief(value):
    if value['coverage'] == 'unavailable':
        return 'DeepSeek 用量：未知（尚无提供方计数）；等值费用：未知。'
    counts = value['tokens']
    prefix = '已报告部分' if value['coverage'] == 'partial' else '已报告'
    text = f"DeepSeek {prefix} token：未缓存输入 {counts['inputTokens']}，输出 {counts['outputTokens']}"
    if 'cacheReadTokens' in counts:
        text += f"，缓存读取 {counts['cacheReadTokens']}" + ('（部分）' if 'cacheReadTokens' in value['partial_fields'] else '')
    cost = value['cost']
    if cost['status'] == 'unavailable':
        return text + '；等值费用未知（缓存/价格数据不全）。'
    a, b = cost['usd_min'], cost['usd_max']
    amount = f'${a:.6f}' if a == b else f'${a:.6f}–${b:.6f}'
    text += f"；{'部分' if cost['status'] == 'partial' else ''}等值用量估算 {amount} USD（非额外扣款）。"
    quota = value.get('quota_contribution')
    if quota:
        q = quota['windows']['five_hour']
        text += f" 本任务约占 {quota['plan']} 五小时额度 {q['percent_min']:.4f}%–{q['percent_max']:.4f}%（非账户剩余）。"
    return text
