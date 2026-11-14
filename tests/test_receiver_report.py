"""消息解码与报告组装测试：受限段、三态、路径、NTE 归属、版本与续段。"""
import unittest

from hl7_data.receiver.framing import VT, FS
from hl7_data.receiver.parser import parse_payload, parse_envelope, MessageError
from hl7_data.receiver.report import assemble_report
from hl7_data.receiver.encoding import MISSING
from hl7_data.receiver.limits import Limits

from tests._hl7_messages import mllp, msh, pid, orc, obr, obx, nte, dsc


def payload(wire: bytes) -> bytes:
    return wire[1:-2]


class ParserTests(unittest.TestCase):
    def test_msh_special_positions_and_declared_encoding(self):
        wire = mllp([msh('C1'), pid(), obr('O1'),
                     obx(1, 'NM', 'ACTH^激素', 'a', '12.5')])
        enc, charset, _, _ = parse_envelope(payload(wire))
        self.assertEqual(enc.field, '|')
        self.assertEqual(charset, 'utf-8')
        msg = parse_payload(payload(wire))
        self.assertEqual(msg.msh.field(10).text, 'C1')
        # MSH-1/2 不占业务字段位：MSH-3 是发送应用
        self.assertEqual(msg.msh.field(3).text, 'SYNTH')

    def test_disallowed_segment_rejected(self):
        wire = mllp([msh('C1'), 'ZZZ|1', obr('O1')])
        with self.assertRaises(MessageError) as ctx:
            parse_payload(payload(wire))
        self.assertEqual(ctx.exception.code, 'SEGMENT_NOT_ALLOWED')

    def test_wrong_message_type_is_issue_not_crash(self):
        wire = mllp([msh('C1', mtype='ADT^A04'), pid()])
        msg = parse_payload(payload(wire))
        codes = {i.code for i in msg.issues}
        self.assertIn('UNSUPPORTED_MESSAGE_TYPE', codes)

    def test_unsupported_obx_type(self):
        wire = mllp([msh('C1'), pid(), obr('O1'),
                     obx(1, 'DT', 'X^x', 'a', '20260101')])
        msg = parse_payload(payload(wire))
        self.assertIn('OBX_TYPE_UNSUPPORTED', {i.code for i in msg.issues})

    def test_multibyte_charset_decoding(self):
        wire = mllp([msh('C1'), pid('受试者-甲'), obr('O1'),
                     obx(1, 'ST', 'NOTE^备注', 'a', '中文数值说明')])
        msg = parse_payload(payload(wire))
        self.assertEqual(msg.segments[1].field(3).text, '受试者-甲')

    def test_bad_charset_bytes_fatal(self):
        bad = bytes([VT]) + 'MSH|^~\\&|A|B||||C1|P|2.5.1|||||||UNICODE UTF-8\r'.encode() \
            + b'\xff\xfe' + bytes([FS, 0x0D])
        with self.assertRaises(MessageError) as ctx:
            parse_payload(bad[1:-2])
        self.assertEqual(ctx.exception.code, 'CHARSET_DECODE_FAILED')


class ReportTests(unittest.TestCase):
    def _msg(self, segments):
        return parse_payload(payload(mllp(segments)))

    def test_multiple_obr_groups_and_group_paths(self):
        segments = [
            msh('C1'), pid(),
            orc('O1'), obr('O1'),
            obx(1, 'NM', 'ACTH^激素', 'a', '12.5'),
            nte(1, r'核对\F\分隔'),
            orc('O2'), obr('O2'),
            obx(2, 'NM', 'COR^皮质醇', 'x', '8.1'),
            obx(3, 'NM', 'COR^皮质醇', 'y', '9.2'),
        ]
        draft = assemble_report(self._msg(segments))
        self.assertEqual([g.order_id for g in draft.orders], ['O1', 'O2'])
        paths = [o.source_path() for o in draft.observations]
        self.assertEqual(paths, [
            'group[0]/order=O1/sub_id=a/repeat=0',
            'group[1]/order=O2/sub_id=x/repeat=0',
            'group[1]/order=O2/sub_id=y/repeat=0',
        ])
        # NTE 归属最近 OBX，不跨组
        self.assertEqual(draft.orders[0].observations[0].notes[0].text,
                         '核对|分隔')
        self.assertEqual(draft.orders[1].notes, [])

    def test_same_code_different_subids_never_dedup(self):
        segments = [msh('C1'), pid(), orc('O1'), obr('O1'),
                    obx(1, 'NM', 'ACTH^激素', 'a', '12.5'),
                    obx(2, 'NM', 'ACTH^激素', 'b', '13.5'),
                    obx(3, 'NM', 'ACTH^激素', 'a', '14.0')]
        draft = assemble_report(self._msg(segments))
        self.assertEqual(len(draft.observations), 3)
        self.assertEqual([o.decoded['number'] for o in draft.observations],
                         [12.5, 13.5, 14.0])

    def test_repeated_values_share_subid_distinct_repeat_path(self):
        segments = [msh('C1'), pid(), orc('O1'), obr('O1'),
                    obx(1, 'NM', 'ACTH^激素', 'a', '1.0~2.0~3.0')]
        draft = assemble_report(self._msg(segments))
        self.assertEqual([o.repeat_index for o in draft.observations], [0, 1, 2])
        self.assertEqual([o.decoded['number'] for o in draft.observations],
                         [1.0, 2.0, 3.0])

    def test_value_three_states_preserved(self):
        segments = [
            msh('C1'), pid(), orc('O1'), obr('O1'),
            'OBX|1|ST|T1^t|a||u|||||F',          # OBX-5 空字段
            'OBX|2|ST|T2^t|b|""|u|||||F',        # OBX-5 显式 HL7 null
            'OBX|3|ST|T3^t|c|value|u|||||F',
        ]
        # 缺失 OBX-5：段在 OBX-4 后结束（连 OBX-6 也不存在）
        segments.append('OBX|4|ST|T4^t|d')
        draft = assemble_report(self._msg(segments))
        states = {o.sub_id: o.value_state for o in draft.observations}
        self.assertEqual(states, {'a': 'empty', 'b': 'null',
                                  'c': 'present', 'd': 'missing'})

    def test_nte_before_any_obx_attaches_to_order(self):
        segments = [msh('C1'), pid(), orc('O1'), obr('O1'),
                    nte(1, '医嘱级注释'),
                    obx(1, 'NM', 'ACTH^激素', 'a', '1.0')]
        draft = assemble_report(self._msg(segments))
        self.assertEqual(draft.orders[0].notes[0].text, '医嘱级注释')
        self.assertEqual(draft.orders[0].observations[0].notes, [])

    def test_correction_version_with_evidence_and_pending_link(self):
        # 第一帧只有删除指令，原观察不在 -> 待关联，不伪造正常结果
        seg1 = [msh('C1'), pid(), orc('O1'), obr('O1'),
                obx(1, 'NM', 'ACTH^激素', 'a', '1.0', status='D')]
        draft1 = assemble_report(self._msg(seg1))
        self.assertEqual(len(draft1.pending_links), 1)
        self.assertEqual(draft1.pending_links[0].target_link['status'], 'D')
        self.assertEqual(draft1.observations[0].value_state, 'present')

        # 同帧存在原观察时关联成功
        seg2 = [msh('C2'), pid(), orc('O1'), obr('O1'),
                obx(1, 'NM', 'ACTH^激素', 'a', '1.0', status='F'),
                obx(2, 'NM', 'ACTH^激素', 'a', '1.1', status='C')]
        draft2 = assemble_report(self._msg(seg2))
        self.assertEqual(draft2.pending_links, [])
        corrected = draft2.observations[1]
        self.assertTrue(corrected.is_versioned)
        self.assertTrue(corrected.target_link['resolved_in_frame'])

    def test_dsc_means_incomplete_report(self):
        seg = [msh('C1'), pid(), orc('O1'), obr('O1'),
               obx(1, 'NM', 'ACTH^激素', 'a', '1.0'), dsc('PTR-2')]
        draft = assemble_report(self._msg(seg))
        self.assertFalse(draft.complete)
        self.assertEqual(draft.continuation_pointer, 'PTR-2')
        empty_dsc = assemble_report(self._msg(
            [msh('C2'), pid(), obr('O1'), obx(1, 'NM', 'T^t', 'a', '1'), 'DSC']))
        self.assertTrue(empty_dsc.complete)

    def test_nm_non_numeric_is_application_issue(self):
        draft = assemble_report(self._msg(
            [msh('C1'), pid(), obr('O1'),
             obx(1, 'NM', 'T^t', 'a', 'abc')]))
        self.assertIn('OBX_NM_NOT_NUMERIC',
                      {i.code for i in draft.issues if i.phase == 'application'})

    def test_ed_base64_decode(self):
        import base64
        blob = base64.b64encode(b'pdf-bytes').decode()
        draft = assemble_report(self._msg(
            [msh('C1'), pid(), obr('O1'),
             f'OBX|1|ED|DOC^附件|a|application^pdf^BASE64^^{blob}|||||F']))
        ed = draft.observations[0].decoded
        self.assertEqual(ed['decoded_data_b64'], b'pdf-bytes')

    def test_ce_decode(self):
        draft = assemble_report(self._msg(
            [msh('C1'), pid(), obr('O1'),
             'OBX|1|CE|T^t|a|H^高^L|||||F']))
        self.assertEqual(draft.observations[0].decoded,
                         {'repeat': 0, 'identifier': 'H', 'text': '高',
                          'coding_system': 'L'})

    def test_escaped_delimiter_inside_value(self):
        draft = assemble_report(self._msg(
            [msh('C1'), pid(), obr('O1'),
                r'OBX|1|ST|T^t|a|12\F\5|||||F']))
        self.assertEqual(draft.observations[0].decoded['text'], '12|5')


if __name__ == '__main__':
    unittest.main()
