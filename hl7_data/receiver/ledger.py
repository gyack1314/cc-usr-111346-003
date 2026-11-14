"""SQLite 持久化：原文证据、报告数据与回执账分开存放。

设计原则：

- ``raw_messages`` 只插入不更新，首次接收证据（字节、SHA256、接收时间）
  永不覆盖；离线重解码只写 ``redecodes``；
- 身份 = 发送应用 + 发送机构 + MSH-10；同身份同原文重送复用既有接收
  回执，同身份不同内容记内容冲突，不产生第二份原文；
- ``outbox`` 是独立回执账，PENDING 的确认可被本地调度反复重演，
  SENT 仅在传输层真正写入后登记，写入失败保持 PENDING；
- 报告、观察、待关联与续段指针各自建表，DSC 非空的报告保持 OPEN。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .report import ReportDraft, Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    message_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key    TEXT NOT NULL,
    sending_app     TEXT NOT NULL,
    sending_fac     TEXT NOT NULL,
    control_id      TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    byte_length     INTEGER NOT NULL,
    payload         BLOB NOT NULL,
    received_at     TEXT NOT NULL,
    UNIQUE(identity_key, sha256)
);
CREATE TABLE IF NOT EXISTS content_conflicts (
    conflict_pk     INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key    TEXT NOT NULL,
    existing_sha    TEXT NOT NULL,
    incoming_sha    TEXT NOT NULL,
    byte_length     INTEGER NOT NULL,
    payload         BLOB NOT NULL,
    detected_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frame_events (
    event_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    message_pk      INTEGER,
    event_type      TEXT NOT NULL,
    detail          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS redecodes (
    redecode_pk     INTEGER PRIMARY KEY AUTOINCREMENT,
    message_pk      INTEGER NOT NULL REFERENCES raw_messages(message_pk),
    result_json     TEXT NOT NULL,
    issues_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key      TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETE')),
    continuation_pointer TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_parts (
    part_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_pk       INTEGER NOT NULL REFERENCES reports(report_pk),
    message_pk      INTEGER NOT NULL REFERENCES raw_messages(message_pk),
    segment_index   INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(report_pk, message_pk)
);
CREATE TABLE IF NOT EXISTS orders (
    order_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    report_pk       INTEGER NOT NULL REFERENCES reports(report_pk),
    group_index     INTEGER NOT NULL,
    order_id        TEXT NOT NULL,
    orc_segment     INTEGER,
    obr_segment     INTEGER NOT NULL,
    filler_number   TEXT,
    notes_json      TEXT NOT NULL,
    UNIQUE(report_pk, group_index)
);
CREATE TABLE IF NOT EXISTS observations (
    observation_pk  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_pk       INTEGER NOT NULL REFERENCES reports(report_pk),
    order_pk        INTEGER NOT NULL REFERENCES orders(order_pk),
    message_pk      INTEGER NOT NULL REFERENCES raw_messages(message_pk),
    source_segment  INTEGER NOT NULL,
    source_path     TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    sub_id          TEXT NOT NULL,
    repeat_index    INTEGER NOT NULL,
    identifier      TEXT,
    identifier_text TEXT,
    coding_system   TEXT,
    value_type      TEXT NOT NULL,
    value_state     TEXT NOT NULL,
    value_raw       TEXT,
    decoded_json    TEXT,
    units           TEXT,
    status          TEXT NOT NULL,
    is_versioned    INTEGER NOT NULL,
    link_json       TEXT,
    notes_json      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_links (
    pending_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_pk  INTEGER NOT NULL REFERENCES observations(observation_pk),
    report_pk       INTEGER NOT NULL REFERENCES reports(report_pk),
    link_json       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    message_pk      INTEGER NOT NULL REFERENCES raw_messages(message_pk),
    phase           TEXT NOT NULL CHECK(phase IN ('accept','application')),
    ack_control_id  TEXT NOT NULL,
    ack_code        TEXT NOT NULL,
    result          TEXT NOT NULL CHECK(result IN ('SUCCESS','FAILURE')),
    issues_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(message_pk, phase)
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    message_pk      INTEGER,
    identity_key    TEXT NOT NULL,
    phase           TEXT NOT NULL,
    ack_control_id  TEXT NOT NULL,
    ack_code        TEXT NOT NULL,
    payload         BLOB NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('PENDING','SENT','FAILED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_key(sending_app: str, sending_fac: str, control_id: str) -> str:
    return '\x1f'.join((sending_app, sending_fac, control_id))


class Ledger:
    def __init__(self, path: str = ':memory:'):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 原文证据 -------------------------------------------------------
    def find_identity(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            'SELECT * FROM raw_messages WHERE identity_key=? ORDER BY message_pk',
            (key,)).fetchone()

    def insert_raw(self, key: str, app: str, fac: str, control_id: str,
                   payload: bytes) -> sqlite3.Row:
        digest = hashlib.sha256(payload).hexdigest()
        existing = self.conn.execute(
            'SELECT * FROM raw_messages WHERE identity_key=? AND sha256=?',
            (key, digest)).fetchone()
        if existing is not None:
            return existing
        cur = self.conn.execute(
            """INSERT INTO raw_messages
               (identity_key, sending_app, sending_fac, control_id,
                sha256, byte_length, payload, received_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (key, app, fac, control_id, digest, len(payload), payload, _now()))
        self.conn.commit()
        return self.conn.execute(
            'SELECT message_pk, identity_key, sha256, byte_length, received_at'
            ' FROM raw_messages WHERE message_pk=?',
            (cur.lastrowid,)).fetchone()

    def record_conflict(self, key: str, existing_sha: str, payload: bytes) -> None:
        self.conn.execute(
            """INSERT INTO content_conflicts
               (identity_key, existing_sha, incoming_sha, byte_length,
                payload, detected_at)
               VALUES (?,?,?,?,?,?)""",
            (key, existing_sha, hashlib.sha256(payload).hexdigest(),
             len(payload), payload, _now()))
        self.conn.commit()

    def record_frame_event(self, message_pk: int | None, event_type: str,
                           detail: str) -> None:
        self.conn.execute(
            'INSERT INTO frame_events (message_pk, event_type, detail, created_at)'
            ' VALUES (?,?,?,?)', (message_pk, event_type, detail, _now()))
        self.conn.commit()

    def get_raw(self, message_pk: int) -> sqlite3.Row:
        return self.conn.execute(
            'SELECT * FROM raw_messages WHERE message_pk=?', (message_pk,)).fetchone()

    def iter_raw(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT * FROM raw_messages ORDER BY message_pk LIMIT ?',
            (limit,)).fetchall()

    def insert_redecode(self, message_pk: int, result_json: str,
                        issues_json: str) -> None:
        self.conn.execute(
            'INSERT INTO redecodes (message_pk, result_json, issues_json, created_at)'
            ' VALUES (?,?,?,?)', (message_pk, result_json, issues_json, _now()))
        self.conn.commit()

    # ---- 报告组装结果 ----------------------------------------------------
    def open_report_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM reports WHERE status='OPEN'").fetchone()['n']

    def persist_draft(self, draft: ReportDraft, message_pk: int,
                      segment_index: int) -> dict:
        now = _now()
        cur = self.conn.execute(
            'SELECT * FROM reports WHERE report_key=?', (draft.report_key,))
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO reports
                   (report_key, status, continuation_pointer, created_at, updated_at)
                   VALUES (?,?,?,?,?)""",
                (draft.report_key,
                 'OPEN' if not draft.complete else 'COMPLETE',
                 draft.continuation_pointer, now, now))
            report_pk = self.conn.execute(
                'SELECT report_pk FROM reports WHERE report_key=?',
                (draft.report_key,)).fetchone()['report_pk']
        else:
            report_pk = row['report_pk']
            self.conn.execute(
                'UPDATE reports SET status=?, continuation_pointer=?, updated_at=?'
                ' WHERE report_pk=?',
                ('OPEN' if not draft.complete else 'COMPLETE',
                 draft.continuation_pointer, now, report_pk))
        self.conn.execute(
            """INSERT INTO report_parts (report_pk, message_pk, segment_index, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(report_pk, message_pk) DO UPDATE SET segment_index=segment_index""",
            (report_pk, message_pk, segment_index, now))

        order_pks: dict[int, int] = {}
        for order in draft.orders:
            self.conn.execute(
                """INSERT INTO orders
                   (report_pk, group_index, order_id, orc_segment, obr_segment,
                    filler_number, notes_json)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(report_pk, group_index) DO UPDATE SET order_id=order_id""",
                (report_pk, order.group_index, order.order_id, order.orc_segment,
                 order.obr_segment, order.filler_number,
                 json.dumps([n.__dict__ for n in order.notes], ensure_ascii=False)))
            order_pks[order.group_index] = self.conn.execute(
                'SELECT order_pk FROM orders WHERE report_pk=? AND group_index=?',
                (report_pk, order.group_index)).fetchone()['order_pk']

        for obs in draft.observations:
            self.conn.execute(
                """INSERT INTO observations
                   (report_pk, order_pk, message_pk, source_segment, source_path,
                    order_id, sub_id, repeat_index, identifier, identifier_text,
                    coding_system, value_type, value_state, value_raw, decoded_json,
                    units, status, is_versioned, link_json, notes_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (report_pk, order_pks[obs.group_index], message_pk,
                 obs.source_segment, obs.source_path(), obs.order_id, obs.sub_id,
                 obs.repeat_index, obs.identifier, obs.identifier_text,
                 obs.coding_system, obs.value_type, obs.value_state, obs.value_raw,
                 json.dumps(obs.decoded, ensure_ascii=False, default=str),
                 obs.units, obs.status, 1 if obs.is_versioned else 0,
                 json.dumps(obs.target_link, ensure_ascii=False),
                 json.dumps([n.__dict__ for n in obs.notes], ensure_ascii=False), now))
            observation_pk = self.conn.execute(
                'SELECT last_insert_rowid()').fetchone()[0]
            if obs.is_versioned and obs in draft.pending_links:
                self.conn.execute(
                    """INSERT INTO pending_links
                       (observation_pk, report_pk, link_json, status, created_at)
                       VALUES (?,?,?,'PENDING',?)""",
                    (observation_pk, report_pk,
                     json.dumps(obs.target_link, ensure_ascii=False), now))
        self.conn.commit()
        return {'report_pk': report_pk,
                'status': 'OPEN' if not draft.complete else 'COMPLETE'}

    def list_observations(self, report_pk: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT * FROM observations WHERE report_pk=? ORDER BY observation_pk',
            (report_pk,)).fetchall()

    def list_pending_links(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_links WHERE status='PENDING' ORDER BY pending_pk"
        ).fetchall()

    # ---- 回执账 ----------------------------------------------------------
    def get_receipt(self, message_pk: int, phase: str) -> sqlite3.Row | None:
        return self.conn.execute(
            'SELECT * FROM receipts WHERE message_pk=? AND phase=?',
            (message_pk, phase)).fetchone()

    def record_receipt(self, message_pk: int, phase: str, ack_control_id: str,
                       ack_code: str, result: str, issues: list) -> None:
        self.conn.execute(
            """INSERT INTO receipts
               (message_pk, phase, ack_control_id, ack_code, result,
                issues_json, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(message_pk, phase) DO UPDATE SET
                 ack_control_id=ack_control_id, ack_code=ack_code,
                 result=result, issues_json=issues_json""",
            (message_pk, phase, ack_control_id, ack_code, result,
             json.dumps([i.__dict__ if hasattr(i, '__dict__') else i
                         for i in issues], ensure_ascii=False, default=str), _now()))
        self.conn.commit()

    def enqueue_ack(self, *, message_pk: int | None, identity_key: str, phase: str,
                    ack_control_id: str, ack_code: str, payload: bytes) -> int:
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO outbox
               (message_pk, identity_key, phase, ack_control_id, ack_code,
                payload, status, attempts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'PENDING',0,?,?)""",
            (message_pk, identity_key, phase, ack_control_id, ack_code,
             payload, now, now))
        outbox_pk = cur.lastrowid
        self.conn.commit()
        return outbox_pk

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE status='PENDING' ORDER BY outbox_pk"
        ).fetchall()

    def mark_outbox(self, outbox_pk: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            'UPDATE outbox SET status=?, attempts=attempts+1,'
            ' last_error=?, updated_at=? WHERE outbox_pk=?',
            (status, error, _now(), outbox_pk))
        self.conn.commit()

    def list_outbox(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT * FROM outbox ORDER BY outbox_pk').fetchall()
