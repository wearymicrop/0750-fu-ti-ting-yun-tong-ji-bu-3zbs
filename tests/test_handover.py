import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.modules.reports.manifest import sha256
from app.pipeline import evaluate
from app.samples import load_sample
from app.times import instant

HANDOVER_AT = '2026-11-20T02:00:00+08:00'
POINT = instant(HANDOVER_AT)


def save_handover_report(client):
    report = client.post('/api/runs', json={'sample': 'handover'}).json()
    return report


def test_page_exposes_handover_section(client):
    home = client.get('/')
    assert home.status_code == 200 and home.text.count('data-section=') == 8
    assert '班次交接' in home.text


def test_preview_settles_verifiable_and_carries_remainder(client):
    response = client.get('/api/handover/preview/handover', params={'at': HANDOVER_AT})
    assert response.status_code == 200
    record = response.json()['record']
    settled, carry = record['settled'], record['carry_over']
    # 本班 22:00-02:00 共 14400 秒，全部可证实并落账
    assert settled['service_seconds'] == 14400
    assert settled['responsible_seconds'] == 1800
    assert settled['exempt_seconds'] == 10800
    assert settled['available_seconds'] == 1800
    assert settled['missing_seconds'] == 0
    # 余量 14400 秒结转下一班，其中未恢复报码形成缺证
    assert carry['service_seconds'] == 14400
    assert carry['missing_seconds'] == 7200
    assert carry['unconfirmed_seconds'] == 7200
    assert record['conservation'] == {'service_total': 28800, 'difference': 0}


def test_carried_events_keep_codes_and_split_seconds(client):
    record = client.get('/api/handover/preview/handover', params={'at': HANDOVER_AT}).json()['record']
    by_id = {row['id']: row for row in record['carry_over']['events']}
    # 跨班供电报码：本班已结 10800，下一班承接 3600
    power = by_id['H-102']
    assert power['state'] == 'CONFIRMED'
    assert (power['settled_seconds'], power['carried_seconds']) == (10800, 3600)
    assert power['unconfirmed_seconds'] == 0
    # 未恢复安全联锁：缺证余量随报码整体移交，下一班可继续补证
    open_event = by_id['H-103']
    assert open_event['state'] == 'OPEN_EVENT'
    assert open_event['carried_seconds'] == 14400
    assert open_event['unconfirmed_seconds'] == 7200
    # 交接点之后才发生的报码进入下一班承接清单，但本班已结秒数为零
    later = by_id['H-104']
    assert later['settled_seconds'] == 0 and later['carried_seconds'] == 3600


def test_adopted_codes_and_approved_permits(client):
    record = client.get('/api/handover/preview/handover', params={'at': HANDOVER_AT}).json()['record']
    codes = {row['code']: row for row in record['adopted_codes']}
    assert codes['POWER'] == {'code': 'POWER', 'responsible': 0, 'exempt': 14400, 'missing': 0}
    assert codes['MECHANICAL']['responsible'] == 1800
    assert codes['OPEN_EVENT']['missing'] == 7200
    # 只采用已批准审批；待审批的 HP-102 不得出现在交接凭据中
    assert [row['id'] for row in record['permits']] == ['HP-101']
    assert record['permits'][0]['approval'] == '备电检修单 06'


def test_handover_must_align_with_shift_end(client):
    response = client.get('/api/handover/preview/handover',
                          params={'at': '2026-11-20T02:30:00+08:00'})
    assert response.status_code == 422 and response.json()['code'] == 'INVALID_HANDOVER'


def test_default_preview_uses_first_boundary_and_accepts_epoch(client):
    response = client.get('/api/handover/preview/handover')
    assert response.status_code == 200
    assert response.json()['record']['handover_at'] == POINT
    again = client.get('/api/handover/preview/handover', params={'at': str(POINT)})
    assert again.json()['record']['handover_at'] == POINT


def test_persist_handover_once_and_reject_duplicate(client):
    report = save_handover_report(client)
    url = f"/api/runs/{report['id']}/handovers"
    first = client.post(url, json={'at': HANDOVER_AT})
    assert first.status_code == 200 and first.json()['reused'] is False
    handover_id = first.json()['id']
    duplicate = client.post(url, json={'at': HANDOVER_AT})
    assert duplicate.status_code == 409 and duplicate.json()['code'] == 'HANDOVER_EXISTS'
    listing = client.get(url).json()['handovers']
    assert len(listing) == 1 and listing[0]['id'] == handover_id


def test_accepts_epoch_timestamp_on_persist(client):
    report = save_handover_report(client)
    response = client.post(f"/api/runs/{report['id']}/handovers", json={'at': POINT})
    assert response.status_code == 200 and response.json()['handover_at'] == POINT
    bad = client.post(f"/api/runs/{report['id']}/handovers", json={})
    assert bad.status_code == 422 and bad.json()['code'] == 'INVALID_HANDOVER'


def test_concurrent_settlement_creates_single_record(client):
    report = save_handover_report(client)
    url = f"/api/runs/{report['id']}/handovers"
    barrier = threading.Barrier(4)

    def post():
        barrier.wait()
        return client.post(url, json={'at': HANDOVER_AT})

    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = [response.status_code for response in executor.map(lambda _: post(), range(4))]
    assert statuses.count(200) == 1 and statuses.count(409) == 3
    assert len(client.get(url).json()['handovers']) == 1


def test_failed_settlement_keeps_previous_record(client):
    report = save_handover_report(client)
    url = f"/api/runs/{report['id']}/handovers"
    ok = client.post(url, json={'at': HANDOVER_AT})
    assert ok.status_code == 200
    # 非法交接点写入失败，旧版交接记录原样保留
    bad = client.post(url, json={'at': '2026-11-19T20:00:00+08:00'})
    assert bad.status_code == 422 and bad.json()['code'] == 'INVALID_HANDOVER'
    listing = client.get(url).json()['handovers']
    assert len(listing) == 1 and listing[0]['id'] == ok.json()['id']


def test_rerun_same_shift_does_not_add_carry_over(client):
    report = save_handover_report(client)
    url = f"/api/runs/{report['id']}/handovers"
    client.post(url, json={'at': HANDOVER_AT})
    # 班报重跑一致后，再结转同一班次仍被唯一约束拦截
    rerun = client.post(f"/api/runs/{report['id']}/rerun")
    assert rerun.status_code == 200
    again = client.post(url, json={'at': HANDOVER_AT})
    assert again.status_code == 409
    assert len(client.get(url).json()['handovers']) == 1


def test_export_matches_page_record(client):
    report = save_handover_report(client)
    created = client.post(f"/api/runs/{report['id']}/handovers", json={'at': HANDOVER_AT}).json()
    handover_id = created['id']
    page = client.get(f"/api/runs/{report['id']}/handovers/{handover_id}").json()
    response = client.get(f"/api/runs/{report['id']}/handovers/{handover_id}/export")
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('attachment;')
    payload = response.json()
    assert payload['record'] == page
    assert payload['report_id'] == report['id']
    assert sha256(payload['input']) == report['manifest']['input_sha256']


def test_handover_persists_across_client_lifetimes(client):
    report = save_handover_report(client)
    created = client.post(f"/api/runs/{report['id']}/handovers", json={'at': HANDOVER_AT}).json()
    with TestClient(app) as later:
        listing = later.get(f"/api/runs/{report['id']}/handovers").json()['handovers']
        assert len(listing) == 1 and listing[0]['id'] == created['id']
        detail = later.get(f"/api/runs/{report['id']}/handovers/{created['id']}")
        assert detail.status_code == 200


def test_second_boundary_produces_second_shift_settlement(client):
    report = save_handover_report(client)
    url = f"/api/runs/{report['id']}/handovers"
    first = client.post(url, json={'at': HANDOVER_AT}).json()
    second_point = '2026-11-20T06:00:00+08:00'
    second = client.post(url, json={'at': second_point})
    assert second.status_code == 200 and second.json()['id'] != first['id']
    records = {item['handover_at']: item['record'] for item in client.get(url).json()['handovers']}
    # 关窗点结班：全部营业均已证实，余量为零
    assert records[instant(second_point)]['carry_over']['service_seconds'] == 0
    assert records[instant(second_point)]['settled']['service_seconds'] == 28800


def test_handover_on_unknown_report_404(client):
    response = client.get('/api/runs/missing/handovers')
    assert response.status_code == 404 and response.json()['code'] == 'REPORT_NOT_FOUND'


def test_settlement_uses_archived_input_not_live_sample(client):
    report = save_handover_report(client)
    # 留档输入与当前样例即使规则不变也必须同源：存档结算基于 fetch 回的留档快照
    created = client.post(f"/api/runs/{report['id']}/handovers", json={'at': HANDOVER_AT}).json()
    snapshot = client.get(f"/api/runs/{report['id']}").json()
    assert created['record']['conservation']['service_total'] == \
        snapshot['result']['reconcile']['totals']['service']


def test_custom_import_flow_has_handover_section(client):
    data = load_sample('handover')
    report = client.post('/api/runs', json={'data': data}).json()
    listing = client.get(f"/api/runs/{report['id']}/handovers")
    assert listing.status_code == 200 and listing.json() == {'handovers': []}


def test_per_device_conservation_on_multi_device_sample(client):
    record = client.get('/api/handover/preview/dirty',
                        params={'at': '2026-11-20T02:00:00+08:00'}).json()['record']
    result = evaluate(load_sample('dirty'))
    for device, totals in result['reconcile']['by_device'].items():
        split = (record['settled']['by_device'].get(device, {}).get('service_seconds', 0)
                 + record['carry_over']['by_device'].get(device, {}).get('service_seconds', 0))
        assert split == totals['service']
    assert record['conservation']['difference'] == 0
