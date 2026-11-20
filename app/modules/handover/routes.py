from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from app.errors import BusinessError
from app.modules.handover.settle import handover_boundaries, settle_handover
from app.modules.handover.store import fetch_handover, list_handovers, save_handover
from app.modules.reports.manifest import canonical
from app.modules.reports.store import fetch
from app.pipeline import evaluate
from app.samples import load_sample

router = APIRouter(prefix='/api', tags=['班次交接'])


@router.get('/handover/preview/{sample}', summary='交接试算（不落档）')
def preview(sample: str, at: str = Query(default='')):
    result = evaluate(load_sample(sample))
    boundaries = handover_boundaries(result)
    if not at:
        return {'boundaries': boundaries, 'record': settle_handover(result, boundaries[0])}
    # 前端可传整秒时间戳（数字字符串）或带时区的 ISO 时间
    raw = int(at) if at.isdigit() else at
    record = settle_handover(result, raw)
    return {'boundaries': boundaries, 'record': record}


@router.post('/runs/{report_id}/handovers', summary='结班存档并结转余量')
def create(report_id: str, payload: dict = Body(default_factory=dict)):
    at = payload.get('at')
    if not isinstance(at, (str, int)) or isinstance(at, bool):
        raise BusinessError('INVALID_HANDOVER', '请提交交接时刻 at（整秒时间戳或带时区时间）')
    _data, snapshot = fetch(report_id)
    record = settle_handover(snapshot['result'], at)
    saved = save_handover(report_id, record)
    return {'id': saved['id'], 'reused': saved['reused'],
            'handover_at': record['handover_at'], 'record': record,
            'handovers': list_handovers(report_id)}


@router.get('/runs/{report_id}/handovers', summary='班报的交接记录')
def index(report_id: str):
    fetch(report_id)
    return {'handovers': list_handovers(report_id)}


@router.get('/runs/{report_id}/handovers/{handover_id}', summary='读取交接记录')
def detail(report_id: str, handover_id: str):
    return fetch_handover(report_id, handover_id)


@router.get('/runs/{report_id}/handovers/{handover_id}/export', summary='下载交接证据')
def export(report_id: str, handover_id: str):
    data, snapshot = fetch(report_id)
    record = fetch_handover(report_id, handover_id)
    # 下载内容与页面读取的同一份记录一致，并附留档输入与规则版本可复核
    payload = {
        'report_id': report_id, 'handover_id': handover_id,
        'rule_version': snapshot['manifest']['rule_version'],
        'manifest': snapshot['manifest'], 'record': record, 'input': data,
    }
    return Response(canonical(payload) + '\n', media_type='application/json',
                    headers={'Content-Disposition':
                             f'attachment; filename="lift-handover-{handover_id[:12]}.json"'})
