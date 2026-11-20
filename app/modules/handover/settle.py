from collections import defaultdict

from app.errors import BusinessError
from app.intervals import intersect, span
from app.times import instant, local_time

# 交接点必须落在窗口内，并与某条原始班表的下班时刻对齐
def handover_boundaries(result):
    points = set()
    start, end = result['window']['start'], result['window']['end']
    for device in result['schedules']['devices'].values():
        for shift in device['shifts']:
            if start < shift['end'] <= end:
                points.add(shift['end'])
    return sorted(points)


def _validate(result, handover_at):
    try:
        point = instant(handover_at) if isinstance(handover_at, str) else int(handover_at)
    except (ValueError, TypeError) as error:
        raise BusinessError('INVALID_HANDOVER', '交接时刻须为带时区的整秒时间') from error
    window = result['window']
    if not window['start'] < point <= window['end']:
        raise BusinessError('INVALID_HANDOVER', '交接时刻须在统计窗口内且晚于开窗时刻')
    if point not in handover_boundaries(result):
        raise BusinessError('INVALID_HANDOVER', '交接时刻须与某条班表的下班时刻对齐')
    return point

def _split_segments(segments, point):
    settled, carried = [], []
    for segment in segments:
        before = intersect(segment['start'], segment['end'], segment['start'], point)
        after = intersect(segment['start'], segment['end'], point, segment['end'])
        if before:
            settled.append({**segment, **span(*before)})
        if after:
            carried.append({**segment, **span(*after)})
    return settled, carried


def _event_index(result):
    index = {}
    for event in result['events']['events']:
        for line in event['lines']:
            index[line] = event
    return index


def _carry_events(carried, events_index):
    grouped = {}
    order = []
    for segment in carried:
        for line in segment['event_lines']:
            event = events_index.get(line)
            if event is None:
                continue
            key = (event['device'], event['id'])
            if key not in grouped:
                grouped[key] = {
                    'device': event['device'], 'id': event['id'], 'reason': event['reason'],
                    'state': event['state'], 'event_lines': list(event['lines']),
                    'start': event['start'], 'end': event['end'],
                    'settled_seconds': 0, 'carried_seconds': 0, 'unconfirmed_seconds': 0,
                    'codes': [],
                }
                order.append(key)
            entry = grouped[key]
            entry['carried_seconds'] += segment['seconds']
            code = segment['code']
            if code not in entry['codes']:
                entry['codes'].append(code)
            if event['state'] != 'CONFIRMED' and segment['bucket'] == 'missing':
                entry['unconfirmed_seconds'] += segment['seconds']
    return [grouped[key] for key in order]


def _settled_seconds_by_event(settled, events_index):
    totals = defaultdict(int)
    for segment in settled:
        for line in segment['event_lines']:
            event = events_index.get(line)
            if event is not None:
                totals[(event['device'], event['id'])] += segment['seconds']
    return totals


def _approved_permits(result, settled, carried):
    active_lines = {line for segment in (*settled, *carried) for line in segment['permit_lines']}
    permits = []
    for permit in result['permits']:
        if permit['status'] == 'approved' and permit['line'] in active_lines:
            permits.append({
                'line': permit['line'], 'id': permit['id'], 'device': permit['device'],
                'reason': permit['reason'], 'approval': permit['raw']['approval'],
                'start': permit['start'], 'end': permit['end'],
            })
    return sorted(permits, key=lambda row: row['line'])


def _adopted_codes(settled, carried):
    codes = defaultdict(lambda: {'responsible': 0, 'exempt': 0, 'missing': 0})
    for segment in (*settled, *carried):
        if segment['code'] == 'NO_STOP':
            continue
        if segment['bucket'] in codes[segment['code']]:
            codes[segment['code']][segment['bucket']] += segment['seconds']
    return [{'code': code, **seconds} for code, seconds in sorted(codes.items())
            if any(seconds.values())]


def _by_device(rows):
    totals = defaultdict(lambda: {'service_seconds': 0, 'missing_seconds': 0})
    for row in rows:
        bucket = totals[row['device']]
        bucket['service_seconds'] += row['seconds']
        if row['bucket'] == 'missing':
            bucket['missing_seconds'] += row['seconds']
    return {device: dict(seconds) for device, seconds in sorted(totals.items())}


def settle_handover(result, handover_at):
    """按交接点切分已归属片段：本班只结交接点之前可证实的秒数，余量结转下一班。"""
    point = _validate(result, handover_at)
    settled, carried = _split_segments(result['segments'], point)
    events_index = _event_index(result)
    carry = _carry_events(carried, events_index)
    settled_totals = _settled_seconds_by_event(settled, events_index)
    for entry in carry:
        entry['settled_seconds'] = settled_totals.get((entry['device'], entry['id']), 0)

    def seconds(rows, bucket=None):
        return sum(row['seconds'] for row in rows if bucket is None or row['bucket'] == bucket)

    service_settled = seconds(settled)
    missing_carried = seconds(carried, 'missing')
    record = {
        'handover_at': point,
        'handover_at_local': local_time(point),
        'boundaries': handover_boundaries(result),
        'settled': {
            'service_seconds': service_settled,
            'available_seconds': seconds(settled, 'available'),
            'responsible_seconds': seconds(settled, 'responsible'),
            'exempt_seconds': seconds(settled, 'exempt'),
            'missing_seconds': seconds(settled, 'missing'),
            'by_device': _by_device(settled),
            'segments': sorted(settled, key=lambda row: (row['device'], row['start'])),
        },
        'carry_over': {
            'service_seconds': seconds(carried),
            'missing_seconds': missing_carried,
            'unconfirmed_seconds': sum(row['unconfirmed_seconds'] for row in carry),
            'by_device': _by_device(carried),
            'events': carry,
        },
        'adopted_codes': _adopted_codes(settled, carried),
        'permits': _approved_permits(result, settled, carried),
    }
    # 守恒：本班已结 + 下一班承接 = 营业并集总秒数
    record['conservation'] = {
        'service_total': service_settled + record['carry_over']['service_seconds'],
        'difference': result['reconcile']['totals']['service']
                      - (service_settled + record['carry_over']['service_seconds']),
    }
    return record
