"""编排、回执账与两阶段确认测试。"""
import json
import unittest

from hl7_data.receiver import (
    Ledger, ReceiverService, ServiceConfig, ChunkTransport, Limits,
    AppTemporaryError, AppRejectError,
)
from hl7_data.receiver.ack import AckPolicy, build_ack
from hl7_data.receiver.parser import parse_payload
from hl7_data.receiver.framing import VT, FS, CR

from tests._hl7_messages import mllp, msh, pid, orc, obr, obx, nte, dsc


def wire(segments):
    return mllp(segments)


def parse_ack(frame_bytes: bytes) -> dict:
    """解析出站 ACK 帧，返回 MSA/ERR 与控制 ID 信息。"""
    assert frame_bytes[0] == VT and frame_bytes[-2:] == bytes([FS, CR])
    text = frame_bytes[1:-2].decode('utf-8')
    rows = {}
    for line in text.split('\r'):
        name = line[:3]
        rows.setdefault(name, []).append(line)
    msa = rows['MSA'][0].split('|')
    msh_parts = rows['MSH'][0].split('|')
    return {
        'ack_control_id': msh_parts[9],
        'msa_code': msa[1],
        'original_control_id': msa[2],
        'text': msa[3] if len(msa) > 3 else '',
        'errs': [e.split('|') for e in rows.get('ERR', [])],
        'raw': text,
    }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(':memory:')
        self.service = ReceiverService(self.ledger)

    def _feed(self, data: bytes):
        transport = ChunkTransport([data])
        return self.service.drive_transport(transport), transport

    def test_happy_path_two_phase_acks_with_control_id_binding(self):
        results, transport = self._feed(wire([
            msh('MSG-1'), pid(), orc('O1'), obr('O1'),
            obx(1, 'NM', 'ACTH^激素', 'a', '12.5'),
        ]))
        self.assertEqual(results[0].outcome, 'accepted')
        # 处理阶段只入 outbox，未 flush 前传输层没有任何字节，状态为 PENDING
        self.assertEqual(transport.written, [])
        self.assertEqual({r['status'] for r in self.ledger.list_outbox()},
                         {'PENDING'})
        flush = self.service.flush_outbox(transport)
        self.assertEqual(len(flush['sent']), 2)
        self.assertEqual(len(transport.written), 2)
        accept = parse_ack(transport.written[0])
        app = parse_ack(transport.written[1])
        self.assertEqual(accept['msa_code'], 'AA')
        self.assertEqual(app['msa_code'], 'AA')
        # MSA-2 必须关联原消息 MSH-10
        self.assertEqual(accept['original_control_id'], 'MSG-1')
        self.assertEqual(app['original_control_id'], 'MSG-1')
        self.assertTrue(accept['ack_control_id'].startswith('ACK-'))
        # flush 后 outbox 为 SENT（写入不等于对方收到，发送状态单独登记）
        self.assertEqual({r['status'] for r in self.ledger.list_outbox()},
                         {'SENT'})

    def test_resend_same_bytes_reuses_receipt(self):
        data = wire([msh('MSG-2'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^t', 'a', '1.0')])
        r1, t1 = self._feed(data)
        r2, t2 = self._feed(data)
        self.assertEqual(r1[0].outcome, 'accepted')
        self.assertEqual(r2[0].outcome, 'duplicate')
        # 复用接收回执：receipts 每阶段只有一行，ack_control_id 相同
        receipts = self.ledger.conn.execute(
            'SELECT phase, ack_control_id, ack_code FROM receipts'
        ).fetchall()
        self.assertEqual(len(receipts), 2)
        # 重送不新增原文
        n = self.ledger.conn.execute(
            'SELECT COUNT(*) AS n FROM raw_messages').fetchone()['n']
        self.assertEqual(n, 1)

    def test_resend_different_bytes_same_identity_conflicts(self):
        first = wire([msh('MSG-3'), pid(), obr('O1'),
                      obx(1, 'NM', 'T^t', 'a', '1.0')])
        second = wire([msh('MSG-3'), pid(), obr('O1'),
                       obx(1, 'NM', 'T^t', 'a', '2.0')])
        self._feed(first)
        results, transport = self._feed(second)
        self.assertEqual(results[0].outcome, 'rejected')
        self.assertEqual(results[0].accept_ack, 'CE')
        self.service.flush_outbox(transport)
        ack = parse_ack(transport.written[-1])
        self.assertEqual(ack['msa_code'], 'CE')
        self.assertIn('IDENTITY_CONTENT_CONFLICT', ack['raw'])
        self.assertEqual(ack['original_control_id'], 'MSG-3')
        conflicts = self.ledger.conn.execute(
            'SELECT COUNT(*) AS n FROM content_conflicts').fetchone()['n']
        self.assertEqual(conflicts, 1)

    def test_illegal_frame_no_identity_no_ack(self):
        results, transport = self._feed(bytes([VT]) + b'MSH\x01bad' + bytes([FS, CR]))
        self.assertEqual(results[0].outcome, 'rejected')
        self.assertEqual(transport.written, [])
        self.assertEqual(self.ledger.conn.execute(
            'SELECT COUNT(*) AS n FROM raw_messages').fetchone()['n'], 0)

    def test_structure_failure_emits_ce_with_locatable_err(self):
        # OBX-11 状态缺失 -> accept 级 E -> CE，且没有应用确认
        data = wire([msh('MSG-4'), pid(), obr('O1'),
                     'OBX|1|NM|T^t|a|1.0|u|||||'])
        results, transport = self._feed(data)
        self.assertEqual(results[0].accept_ack, 'CE')
        self.assertIsNone(results[0].app_ack)
        self.service.flush_outbox(transport)
        ack = parse_ack(transport.written[0])
        self.assertEqual(ack['msa_code'], 'CE')
        self.assertTrue(ack['errs'])
        self.assertIn('OBX-11', ack['raw'])

    def test_application_failure_then_redelivery_and_replay(self):
        calls = {'n': 0}

        def flaky_handler(draft, ctx):
            calls['n'] += 1
            if calls['n'] == 1:
                raise AppTemporaryError('下游暂时不可用')

        cfg = ServiceConfig(app_handler=flaky_handler)
        svc = ReceiverService(self.ledger, cfg)
        data = wire([msh('MSG-5', app_ack='AL'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^t', 'a', '1.0')])
        t1 = ChunkTransport([data])
        results = svc.drive_transport(t1)
        self.assertIsNone(results[0].app_ack)
        # 只有接收确认入 outbox，应用确认暂缺
        self.assertEqual(len(self.ledger.pending_outbox()), 1)

        # 本地调度重演（模拟重送/恢复）：第二次 handler 成功
        replay = svc.schedule_application()
        self.assertEqual(replay[0].app_ack, 'AA')
        self.assertEqual(len(self.ledger.pending_outbox()), 2)

        # 确定性拒绝 -> AE/AR 且不重试
        def reject_handler(draft, ctx):
            raise AppRejectError('登记规则拒绝')
        cfg2 = ServiceConfig(app_handler=reject_handler)
        svc2 = ReceiverService(Ledger(':memory:'), cfg2)
        t2 = ChunkTransport([wire([msh('MSG-6'), pid(), obr('O1'),
                                   obx(1, 'NM', 'T^t', 'b', '1.0')])])
        r2 = svc2.drive_transport(t2)
        self.assertEqual(r2[0].app_ack, 'AE')

    def test_ack_policy_modes(self):
        def policy(accept, app):
            msg = parse_payload(wire([msh('C', accept=accept, app_ack=app)])[1:-2])
            return AckPolicy.from_message(msg)
        self.assertFalse(policy('NE', 'NE').wants('accept', True))
        self.assertFalse(policy('SU', 'NE').wants('accept', False))
        self.assertTrue(policy('ER', 'NE').wants('accept', False))
        self.assertFalse(policy('ER', 'NE').wants('accept', True))
        self.assertTrue(policy('AL', 'SU').wants('application', True))
        self.assertFalse(policy('AL', 'SU').wants('application', False))

    def test_offline_redecode_preserves_first_evidence(self):
        data = wire([msh('MSG-7'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^t', 'a', '1.0')])
        self._feed(data)
        row = self.ledger.conn.execute(
            'SELECT * FROM raw_messages').fetchone()
        before = (row['sha256'], row['received_at'], row['payload'])
        result = self.service.redecode_offline(row['message_pk'])
        self.assertEqual(len(result['observations']), 1)
        row2 = self.ledger.conn.execute(
            'SELECT * FROM raw_messages WHERE message_pk=1').fetchone()
        self.assertEqual((row2['sha256'], row2['received_at'], row2['payload']),
                         before)
        self.assertEqual(self.ledger.conn.execute(
            'SELECT COUNT(*) AS n FROM redecodes').fetchone()['n'], 1)

    def test_open_report_limit(self):
        cfg = ServiceConfig(limits=Limits(max_open_reports=1))
        svc = ReceiverService(Ledger(':memory:'), cfg)
        t = ChunkTransport([
            wire([msh('R-1'), pid('P1'), obr('O1'),
                  obx(1, 'NM', 'T^t', 'a', '1'), dsc('P2')]),
            wire([msh('R-2'), pid('P2'), obr('O2'),
                  obx(1, 'NM', 'T^t', 'a', '1'), dsc('P3')]),
        ])
        results = svc.drive_transport(t)
        self.assertEqual(results[0].report_status, 'OPEN')
        self.assertEqual(results[1].outcome, 'rejected')
        self.assertEqual(results[1].accept_ack, 'CE')
        self.assertIn('OPEN_REPORT_LIMIT',
                      [i.code for i in results[1].issues])

    def test_continuation_completes_report(self):
        part1 = wire([msh('RPT-1A'), pid('P9'), orc('O1'), obr('O1'),
                      obx(1, 'NM', 'T^t', 'a', '1'), dsc('PTR-2')])
        part2 = wire([msh('RPT-1B'), pid('P9'), orc('O1'), obr('O1'),
                      obx(2, 'NM', 'T^t', 'b', '2'), dsc(None)])
        r1, _ = self._feed(part1)
        self.assertEqual(r1[0].report_status, 'OPEN')
        r2, _ = self._feed(part2)
        self.assertEqual(r2[0].report_status, 'COMPLETE')
        report = self.ledger.conn.execute(
            "SELECT * FROM reports WHERE status='COMPLETE'").fetchone()
        obs = self.ledger.list_observations(report['report_pk'])
        self.assertEqual([o['sub_id'] for o in obs], ['a', 'b'])
        parts = self.ledger.conn.execute(
            'SELECT COUNT(*) AS n FROM report_parts').fetchone()['n']
        self.assertEqual(parts, 2)

    def test_chunked_delivery_split_everywhere(self):
        data = wire([msh('CHUNK-1'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^中文', 'a', '1.0')])
        transport = ChunkTransport([data[i:i + 3] for i in range(0, len(data), 3)])
        results = self.service.drive_transport(transport)
        self.assertEqual(results[0].outcome, 'accepted')

    def test_tcp_write_failure_keeps_outbox_pending(self):
        class BrokenTransport:
            def write(self, data):
                raise OSError('connection reset')
        data = wire([msh('MSG-8'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^t', 'a', '1')])
        self._feed(data)
        flush = self.service.flush_outbox(BrokenTransport())
        self.assertEqual(len(flush['failed']), 2)
        self.assertEqual({r['status'] for r in self.ledger.pending_outbox()},
                         {'PENDING'})
        # 恢复后重放成功
        good = ChunkTransport()
        flush2 = self.service.flush_outbox(good)
        self.assertEqual(len(flush2['sent']), 2)
        self.assertEqual(len(good.written), 2)

    def test_ack_msa_and_err_structure(self):
        msg = parse_payload(wire([msh('MSG-9')])[1:-2])
        from hl7_data.receiver.parser import Issue
        outcome = build_ack(
            original=msg, phase='application', success=False,
            issues=[Issue('APP_REJECTED', 'application', 'E', '拒绝原因',
                          'OBX', 4, field=5)],
            ack_control_id='ACK-XYZ')
        text = outcome.payload.decode('utf-8')
        self.assertIn('MSA|AE|MSG-9|拒绝原因', text)
        self.assertIn('ERR|', text)
        self.assertIn('OBX-5-5', text.split('ERR|', 1)[1])


if __name__ == '__main__':
    unittest.main()
