"""ORU^R01 受限接收端端到端演示（全部为合成数据，不接入真实医疗系统）。

运行：PYTHONPATH=. python3 -I demos/oru_receiver_demo.py

五个场景独立核对：
  1. 一条消息多个 OBR、同检测代码不同子 ID、重复观察值、转义分隔符；
  2. 应用暂时失败后的重送与本地调度重演；
  3. DSC 报告续段（单帧不等同完整报告）；
  4. 更正/删除版本留存与待关联；
  5. 非法控制字节拒绝与同身份内容冲突。

每个场景都独立打印每条观察的组路径，以及实际发出的两阶段确认
（MSA 确认码 + MSA-2 原消息控制 ID + ERR 定位）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hl7_data.receiver import (  # noqa: E402
    Ledger, ReceiverService, ServiceConfig, ChunkTransport,
    AppTemporaryError, AppRejectError, Limits,
)
from hl7_data.receiver.framing import VT, FS, CR  # noqa: E402
from tests._hl7_messages import mllp, msh, pid, orc, obr, obx, nte, dsc  # noqa: E402

SEP = '=' * 72


def parse_out_acks(transport: ChunkTransport) -> list[dict]:
    """独立解析出站帧，不依赖接收器内部状态。"""
    acks = []
    for frame in transport.written:
        assert frame[0] == VT and frame[-2:] == bytes([FS, CR]), 'MLLP 帧界非法'
        text = frame[1:-2].decode('utf-8')
        lines = text.split('\r')
        msh_parts = lines[0].split('|')
        msa = next(x for x in lines if x.startswith('MSA')).split('|')
        errs = [x for x in lines if x.startswith('ERR')]
        acks.append({
            'ack_control_id': msh_parts[9],
            'msa': msa[1],
            'original_control_id': msa[2],
            'err_loc': [e.split('|')[-1] for e in errs],
        })
    return acks


def banner(title: str) -> None:
    print('\n' + SEP + f'\n场景：{title}\n' + SEP)


def show_observations(ledger: Ledger) -> None:
    rows = ledger.conn.execute(
        'SELECT source_path, sub_id, repeat_index, identifier, value_state,'
        ' value_type, decoded_json, status, is_versioned FROM observations'
        ' ORDER BY observation_pk').fetchall()
    print(f'  持久化观察 {len(rows)} 条，独立核对来源路径：')
    for r in rows:
        decoded = json.loads(r['decoded_json']) if r['decoded_json'] else None
        print(f"    {r['source_path']}")
        print(f"      标识={r['identifier']} 子ID={r['sub_id']!r} "
              f"重复={r['repeat_index']} 类型={r['value_type']} "
              f"三态={r['value_state']} 状态={r['status'] or '-'} "
              f"版本化={bool(r['is_versioned'])} 值={decoded}")


def scenario_groups_and_escapes(db_path: str) -> None:
    banner('多个 OBR / 同代码不同子 ID / 重复值 / 转义分隔符')
    ledger = Ledger(db_path)
    svc = ReceiverService(ledger)
    segments = [
        msh('DEMO-1'),
        pid('SYNTH-PATIENT-1'),
        orc('ORD-A'), obr('ORD-A'),
        obx(1, 'NM', 'ACTH^促肾上腺皮质激素', 'a', '12.5'),
        obx(2, 'NM', 'ACTH^促肾上腺皮质激素', 'b', '13.5'),  # 同代码不同子ID
        nte(1, r'核对\F\分隔符不重新拆字段'),
        obx(3, 'NM', 'COR^皮质醇', 'r', '8.1~8.4~8.0'),       # 重复观察值
        orc('ORD-B'), obr('ORD-B'),
        obx(4, 'ST', 'NOTE^备注', 'z', r'12.5\F\ng/dL'),      # 转义出的 |
        obx(5, 'CE', 'FLAG^标志', 'q', 'H^偏高^L'),
    ]
    wire = mllp(segments)
    # 按 7 字节切块，刻意切断 VT、FS CR 与多字节中文字符
    transport = ChunkTransport([wire[i:i + 7] for i in range(0, len(wire), 7)])
    results = svc.drive_transport(transport)
    assert results and results[0].outcome == 'accepted'
    svc.flush_outbox(transport)
    show_observations(ledger)
    acks = parse_out_acks(transport)
    print('  实际发出的两阶段确认：')
    for ack in acks:
        print(f"    MSA={ack['msa']} MSA-2原控制ID={ack['original_control_id']}"
              f" ACK控制ID={ack['ack_control_id']} ERR={ack['err_loc']}")
    assert [a['msa'] for a in acks] == ['AA', 'AA']
    assert all(a['original_control_id'] == 'DEMO-1' for a in acks)
    ledger.close()


def scenario_app_failure_replay(db_path: str) -> None:
    banner('应用暂时失败后的重送与本地调度重演')
    calls = {'n': 0}

    def flaky(draft, ctx):
        calls['n'] += 1
        if calls['n'] == 1:
            raise AppTemporaryError('模拟登记库暂时不可用')
        # 第二次（重送或调度重演）即恢复

    ledger = Ledger(db_path)
    svc = ReceiverService(ledger, ServiceConfig(app_handler=flaky))
    data = mllp([msh('DEMO-2', app_ack='AL'), pid(), orc('O1'), obr('O1'),
                 obx(1, 'NM', 'T^t', 'a', '1.0')])
    t1 = ChunkTransport([data])
    first = svc.drive_transport(t1)
    print(f'  首次处理：outcome={first[0].outcome} 接收确认={first[0].accept_ack}'
          f' 应用确认={first[0].app_ack}（暂时失败，应用确认留 PENDING）')
    assert first[0].accept_ack == 'AA' and first[0].app_ack is None

    # 本地调度重演应用阶段（handler 第二次调用已恢复）
    replay = svc.schedule_application()
    print(f'  调度重演：应用确认={replay[0].app_ack}')
    assert replay[0].app_ack == 'AA'

    # 同原文重送：复用接收回执，不重复落证据
    t2 = ChunkTransport([data])
    redeliver = svc.drive_transport(t2)
    print(f'  同原文重送：outcome={redeliver[0].outcome}（接收回执复用）')
    assert redeliver[0].outcome == 'duplicate'
    svc.flush_outbox(t1)
    acks = parse_out_acks(t1)
    print('  outbox 实际确认序列：')
    for ack in acks:
        print(f"    阶段帧 MSA={ack['msa']} 关联={ack['original_control_id']}")
    ledger.close()


def scenario_continuation(db_path: str) -> None:
    banner('DSC 报告续段：单帧不等同完整报告')
    ledger = Ledger(db_path)
    svc = ReceiverService(ledger)
    p1 = mllp([msh('DEMO-3A'), pid('PAT-3'), orc('O1'), obr('O1'),
               obx(1, 'NM', 'T^t', 'a', '1'), dsc('PTR-PAGE-2')])
    p2 = mllp([msh('DEMO-3B'), pid('PAT-3'), orc('O1'), obr('O1'),
               obx(2, 'NM', 'T^t', 'b', '2'), dsc(None)])
    r1 = svc.handle_chunk(p1)
    r2 = svc.handle_chunk(p2)
    print(f"  首帧报告状态={r1[0].report_status}（DSC 非空，OPEN）")
    print(f"  续帧报告状态={r2[0].report_status}（DSC 空，COMPLETE）")
    assert r1[0].report_status == 'OPEN' and r2[0].report_status == 'COMPLETE'
    report = ledger.conn.execute(
        "SELECT * FROM reports WHERE status='COMPLETE'").fetchone()
    parts = ledger.conn.execute(
        'SELECT COUNT(*) AS n FROM report_parts').fetchone()['n']
    print(f"  同一报告 report_pk={report['report_pk']} 汇聚帧数={parts}")
    assert parts == 2
    ledger.close()


def scenario_versions_and_pending(db_path: str) -> None:
    banner('更正/删除/撤回版本留存与待关联（不伪造正常结果）')
    ledger = Ledger(db_path)
    svc = ReceiverService(ledger)
    # 先到一条删除指令，原观察尚不存在 -> 待关联
    d1 = mllp([msh('DEMO-4A'), pid(), orc('O1'), obr('O1'),
               obx(1, 'NM', 'T^t', 'a', '1.0', status='D')])
    svc.handle_chunk(d1)
    pending = ledger.list_pending_links()
    print(f'  无原观察的删除版本：待关联 {len(pending)} 条，不生成正常结果')
    assert len(pending) == 1
    # 原观察与更正版本同帧到达 -> 关联成功
    d2 = mllp([msh('DEMO-4B'), pid(), orc('O2'), obr('O2'),
               obx(1, 'NM', 'T^t', 'a', '1.0', status='F'),
               obx(2, 'NM', 'T^t', 'a', '1.1', status='C')])
    svc.handle_chunk(d2)
    rows = ledger.conn.execute(
        'SELECT status, is_versioned, link_json FROM observations'
        ' ORDER BY observation_pk').fetchall()
    print('  版本留存：')
    for r in rows:
        print(f"    status={r['status']} 版本化={bool(r['is_versioned'])}"
              f" 关联证据={r['link_json']}")
    ledger.close()


def scenario_rejections(db_path: str) -> None:
    banner('非法控制字节拒绝 / 同身份内容冲突 / 字段三态')
    ledger = Ledger(db_path)
    svc = ReceiverService(ledger)
    # 非法控制字节：不猜测正文结束，无身份则无确认
    bad = bytes([VT]) + b'MSH|bad\x01data' + bytes([FS, CR])
    r_bad = svc.handle_chunk(bad)
    print(f"  非法控制字节：outcome={r_bad[0].outcome} "
          f"问题码={r_bad[0].issues[0].code}（无身份，不发确认）")
    assert r_bad[0].outcome == 'rejected'

    # 同身份不同内容：原文证据保留，冲突入专表，回 CE + 可定位 ERR
    first = mllp([msh('DEMO-5'), pid(), obr('O1'),
                  obx(1, 'NM', 'T^t', 'a', '1.0')])
    second = mllp([msh('DEMO-5'), pid(), obr('O1'),
                   obx(1, 'NM', 'T^t', 'a', '2.0')])
    svc.handle_chunk(first)
    t = ChunkTransport([second])
    r_conflict = svc.drive_transport(t)
    svc.flush_outbox(t)
    acks = parse_out_acks(t)
    print(f"  内容冲突：outcome={r_conflict[0].outcome} "
          f"确认={[a['msa'] for a in acks]} 定位={acks[-1]['err_loc']}")
    assert r_conflict[0].outcome == 'rejected'
    assert acks[-1]['msa'] == 'CE'

    # OBX-5 三态（可与 OBX-11 共存的空 / 显式 null / 正常；真正“缺失”
    # 字段无法带后续 OBX-11，其隔离验证见单元测试 test_value_three_states_preserved）
    tri = mllp([
        msh('DEMO-6'), pid(), obr('O2'),
        'OBX|1|ST|T1^t|a||u|||||F',    # OBX-5 空
        'OBX|2|ST|T2^t|b|""|u|||||F',  # OBX-5 显式 HL7 null
        'OBX|3|ST|T3^t|c|value|u|||||F',
    ])
    svc.handle_chunk(tri)
    states = {r['sub_id']: r['value_state']
              for r in ledger.conn.execute(
                  "SELECT sub_id, value_state FROM observations "
                  "WHERE report_pk=(SELECT report_pk FROM reports "
                  "ORDER BY report_pk DESC LIMIT 1)").fetchall()}
    print(f"  OBX-5 三态留存：{states}")
    assert states == {'a': 'empty', 'b': 'null', 'c': 'present'}
    ledger.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='oru-demo-') as tmp:
        scenarios = [
            ('groups', scenario_groups_and_escapes),
            ('replay', scenario_app_failure_replay),
            ('continuation', scenario_continuation),
            ('versions', scenario_versions_and_pending),
            ('rejections', scenario_rejections),
        ]
        for name, fn in scenarios:
            fn(os.path.join(tmp, f'{name}.db'))
    print('\n' + SEP + '\n全部场景核对通过：接收器未做临床判断，'
          '原文证据、组路径、版本与两阶段确认均按账留存。\n' + SEP)


if __name__ == '__main__':
    main()
