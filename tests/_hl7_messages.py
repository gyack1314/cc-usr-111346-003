"""合成 ORU^R01 消息构造工具（测试与演示共用，全部为虚构数据）。"""
from hl7_data.receiver.framing import VT, FS, CR


def mllp(segments: list[str], charset: str = 'utf-8') -> bytes:
    body = '\r'.join(segments) + '\r'
    return bytes([VT]) + body.encode(charset) + bytes([FS, CR])


def msh(control_id: str, *, accept='AL', app_ack='AL', charset='UNICODE UTF-8',
        sending_app='SYNTH', sending_fac='LAB', mtype='ORU^R01',
        version='2.5.1', processing='P') -> str:
    # 字段位置：MSH-3 发送应用 / MSH-4 机构 / MSH-5/6 接收方 /
    # MSH-7 日期时间(空) / MSH-9 消息类型 / MSH-10 控制 ID /
    # MSH-11 处理 ID / MSH-12 版本 / MSH-15/16 确认策略 / MSH-18 字符集
    return (f'MSH|^~\\&|{sending_app}|{sending_fac}|RESEARCH-RECEIVER|RESEARCH-LOCAL'
            f'|||{mtype}|{control_id}|{processing}|{version}|||{accept}|{app_ack}'
            f'||{charset}')


def pid(subject='SYNTH-SUBJECT-1') -> str:
    return f'PID|1||{subject}'


def orc(order_id: str, placer=None) -> str:
    return f'ORC|RE|{placer or order_id}|{order_id}'


def obr(order_id: str, code='ACTH^合成激素项^L', filler=None) -> str:
    return f'OBR|1|{order_id}|{filler or order_id}|{code}'


def obx(set_id: int, value_type: str, identifier: str, sub_id: str, value: str,
        *, status='F', unit='unit') -> str:
    return (f'OBX|{set_id}|{value_type}|{identifier}|{sub_id}|{value}|{unit}||||'
            f'|{status}')


def nte(set_id: int, text: str) -> str:
    return f'NTE|{set_id}||{text}'


def dsc(pointer: str | None, style='I') -> str:
    if pointer is None:
        return 'DSC'
    return f'DSC|{pointer}|{style}'
