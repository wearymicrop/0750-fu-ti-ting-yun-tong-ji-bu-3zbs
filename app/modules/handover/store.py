import hashlib
import json
import sqlite3

from app.errors import BusinessError
from app.modules.reports.manifest import canonical
from app.modules.reports.store import database as report_database

_SCHEMA = '''CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    handover_at INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(report_id, handover_at)
)'''
_CREATE_INDEX = 'CREATE INDEX IF NOT EXISTS idx_handovers_report ON handovers(report_id)'


def _ensure_schema(connection):
    connection.execute(_SCHEMA)
    connection.execute(_CREATE_INDEX)


def _record_id(report_id, point):
    return hashlib.sha256(f'{report_id}:{point}'.encode('utf-8')).hexdigest()


def save_handover(report_id, record):
    """同一班报同一交接点只允许一份结转；写入失败时旧记录原样保留。"""
    point = record['handover_at']
    handover_id = _record_id(report_id, point)
    encoded = canonical(record)
    with report_database() as connection:
        _ensure_schema(connection)
        try:
            cursor = connection.execute(
                'INSERT INTO handovers (id, report_id, handover_at, record_json) VALUES (?, ?, ?, ?)',
                (handover_id, report_id, point, encoded),
            )
        except sqlite3.IntegrityError as error:
            raise BusinessError('HANDOVER_EXISTS', '该班次已结转，重跑不会新增或重复结转', 409) from error
        row = connection.execute('SELECT record_json FROM handovers WHERE id = ?', (handover_id,)).fetchone()
        return {'id': handover_id, 'report_id': report_id, 'reused': cursor.rowcount == 0,
                'record': json.loads(row[0])}


def list_handovers(report_id):
    with report_database() as connection:
        _ensure_schema(connection)
        rows = connection.execute(
            'SELECT id, handover_at, record_json, created_at FROM handovers WHERE report_id = ? ORDER BY handover_at',
            (report_id,),
        ).fetchall()
    return [{'id': row[0], 'report_id': report_id, 'handover_at': row[1],
             'created_at': row[3], 'record': json.loads(row[2])} for row in rows]


def fetch_handover(report_id, handover_id):
    with report_database() as connection:
        _ensure_schema(connection)
        row = connection.execute(
            'SELECT record_json FROM handovers WHERE id = ? AND report_id = ?',
            (handover_id, report_id),
        ).fetchone()
    if row is None:
        raise BusinessError('HANDOVER_NOT_FOUND', '交接记录不存在', 404)
    return json.loads(row[0])
