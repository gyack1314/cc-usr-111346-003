"""两阶段确认层。

按 MSH-15（接收确认 accept ack）与 MSH-16（应用确认 application ack）
区分两个阶段：

- 接收确认只证明原文已持久化，成功码 AA 仅在原文落库后产生；
- 应用确认在结构/组级处理（及可注入的应用处理）成功后才允许 AA；
- 失败按阶段给出 AE/AR（应用层）或 CE/CR（结构/帧层），并携带可定位
  ERR；确认本身的 MSA-2 必须回填原消息 MSH-10 控制 ID。

本层只构造 ACK 消息字节，不负责发送——TCP 写入成功不等同对方收到确认，
发送与重试由 ledger 的 outbox + service 调度负责。
"""
from dataclasses import dataclass

from .encoding import EncodingChars, escape_text
from .parser import Issue

# 阶段 -> 成功/失败确认码
PHASE_CODES = {
    'accept': {'success': 'AA', 'error': 'CE', 'reject': 'CR'},
    'application': {'success': 'AA', 'error': 'AE', 'reject': 'AR'},
}


@dataclass(frozen=True)
class AckPolicy:
    """MSH-15/16 声明的确认策略。"""
    accept: str          # AL / ER / NE / SU / ''（空按 AL 处理接收确认）
    application: str     # AL / ER / NE / SU / ''（空表示无应用确认）

    @classmethod
    def from_message(cls, message) -> 'AckPolicy':
        msh = message.msh
        # 业务字段位 = HL7 字段号 - 2（MSH-1/2 为特殊位置）
        accept_node = msh.field(15)
        app_node = msh.field(16)
        accept = accept_node.text if accept_node.state == 'present' else 'AL'
        app = app_node.text if app_node.state == 'present' else ''
        return cls(accept or 'AL', app)

    def wants(self, phase: str, success: bool) -> bool:
        mode = self.accept if phase == 'accept' else self.application
        if not mode:
            # 未声明：接收确认按 AL 处理，应用确认不需要
            return phase == 'accept'
        if mode == 'NE':
            return False
        if mode == 'AL':
            return True
        if mode == 'ER':
            return not success
        if mode == 'SU':
            return success
        return False


@dataclass
class AckOutcome:
    phase: str
    code: str                       # AA/AE/AR/CE/CR
    text: str
    issues: list[Issue]
    payload: bytes | None           # 构造好的 ACK 消息（不含 MLLP 包裹）
    ack_control_id: str | None


def _ack_text_code(phase: str, severity: str | None) -> str:
    if severity == 'E' and phase == 'accept':
        return PHASE_CODES['accept']['error']       # CE
    if severity == 'E':
        return PHASE_CODES['application']['error']  # AE
    if phase == 'accept':
        return PHASE_CODES['accept']['reject']      # CR
    return PHASE_CODES['application']['reject']     # AR


def _err_segment(issue: Issue, enc: EncodingChars) -> str:
    location = []
    if issue.segment_id:
        location.append(issue.segment_id)
    if issue.segment_index is not None:
        location.append(str(issue.segment_index + 1))  # 1 基段序号
    if issue.field is not None:
        location.append(str(issue.field))
    loc = '-'.join(location) or 'MSH'
    code = escape_text(issue.code, enc)
    message = escape_text(issue.message, enc)
    severity = issue.severity or 'E'
    # 定位串：段ID-段序号-字段号，如 OBX-5-4
    # ERR-1 错误位置复合字段 / ERR-3 HL70357 错误码 / ERR-4 严重度 /
    # ERR-5 错误文本 / ERR-8 定位串
    return (f'ERR{enc.field}{enc.component}{enc.component}{enc.component}'
            f'{enc.field}{enc.field}{code}{enc.component}{message}'
            f'{enc.component}{enc.component}{enc.component}'
            f'{enc.field}{severity}{enc.field}{message}{enc.field}{loc}')


def build_ack(*, original, phase: str, success: bool,
              issues: list[Issue] | None = None,
              ack_control_id: str, app_text: str = '',
              payload_charset: str | None = None) -> AckOutcome:
    """构造一阶段 ACK。

    original 为 ParsedMessage（正常路径）或一个轻量头对象（致命帧/解码
    失败路径，需具备 encoding、charset、segment_text、msh.field 属性）。
    """
    issues = issues or []
    enc: EncodingChars = original.encoding
    charset = payload_charset or getattr(original, 'charset', 'utf-8')
    msh = original.msh

    def msh_leaf(number: int) -> str:
        node = msh.field(number)
        from .encoding import MISSING, NULL
        if node in (MISSING, NULL) or node.state != 'present':
            return ''
        return node.text

    if success:
        code = PHASE_CODES[phase]['success']
        text = app_text or ('原文已接收并持久化' if phase == 'accept'
                            else '结构与组级处理成功')
    else:
        severity = issues[0].severity if issues else 'E'
        code = _ack_text_code(phase, severity)
        text = issues[0].message if issues else '处理失败'

    # MSH 字段号（MSH-1/2 特殊位置，field(n) 已按 HL7 字段号取值）
    sending_app = msh_leaf(5) or 'RESEARCH-RECEIVER'
    sending_fac = msh_leaf(6) or 'RESEARCH-LOCAL'
    recv_app = msh_leaf(3)
    recv_fac = msh_leaf(4)
    processing_id = msh_leaf(11) or 'P'
    version = msh_leaf(12) or '2.5.1'
    original_control = msh_leaf(10)

    lines = [
        'MSH' + enc.field + enc.component + enc.repetition + enc.escape
        + enc.subcomponent + enc.field
        + escape_text(sending_app, enc) + enc.field
        + escape_text(sending_fac, enc) + enc.field
        + escape_text(recv_app, enc) + enc.field
        + escape_text(recv_fac, enc) + enc.field
        + enc.field + enc.field
        + f'ACK{enc.component}R01' + enc.field
        + escape_text(ack_control_id, enc) + enc.field
        + escape_text(processing_id, enc) + enc.field
        + escape_text(version, enc) + enc.field,
        # MSA-1 确认码 / MSA-2 原消息控制 ID（关联证据）
        f'MSA{enc.field}{code}{enc.field}{escape_text(original_control, enc)}'
        f'{enc.field}{escape_text(text, enc)}',
    ]
    for issue in issues:
        lines.append(_err_segment(issue, enc))

    payload = '\r'.join(lines).encode(charset, errors='strict')
    return AckOutcome(
        phase=phase,
        code=code,
        text=text,
        issues=list(issues),
        payload=payload,
        ack_control_id=ack_control_id,
    )
