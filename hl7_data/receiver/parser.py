"""HL7 v2 受限消息解码层。

输入是 framing 层产出的整帧字节（不含 VT/FS/CR 包裹），输出为
:class:`ParsedMessage`。本层只做语法与受限段结构解释，不做临床判断：

- 允许段固定为 MSH、PID、ORC、OBR、OBX、NTE、DSC，其余段拒绝；
- MSH-1/2 为特殊位置（字段分隔符与编码字符），MSH-3 起按普通字段处理；
- 字段分隔符与编码字符均由消息自己声明；
- 非法字符集、编码字符、转义、未知段、组级错位等以 :class:`Issue`
  定位到段（及字段），结构问题不静默丢弃。
"""
from dataclasses import dataclass, field

from .encoding import (
    CHARSETS, EncodingChars, EncodingError, FieldNode, MISSING, NULL,
    parse_field,
)
from .limits import Limits

ALLOWED_SEGMENTS = frozenset(('MSH', 'PID', 'ORC', 'OBR', 'OBX', 'NTE', 'DSC'))
ALLOWED_OBX_TYPES = frozenset(('NM', 'ST', 'CE', 'ED'))


@dataclass(frozen=True)
class Issue:
    """可定位问题。phase 取 framing/accept/application，severity 取 E/W/I。"""
    code: str
    phase: str
    severity: str
    message: str
    segment_id: str | None = None
    segment_index: int | None = None
    field: int | None = None
    component: int | None = None


class MessageError(ValueError):
    """解码无法继续（字符集、MSH 头等致命问题）。"""

    def __init__(self, message, code='MESSAGE_UNDECODABLE', issue=None):
        super().__init__(message)
        self.code = code
        self.issue = issue


@dataclass
class ParsedSegment:
    name: str
    index: int                                   # 0 基段序号
    fields: list[FieldNode]                      # 见 field() 的编号约定
    raw: str
    # MSH 专用：HL7 字段号 -> fields 下标的显式映射（None=动态分隔符）
    _msh_map: dict | None = None

    def field(self, number: int) -> FieldNode:
        """按 HL7 字段号取字段；不存在返回 MISSING 哨兵。"""
        if number < 1:
            raise ValueError('字段号从 1 开始')
        if self.name == 'MSH' and self._msh_map is not None:
            pos = self._msh_map.get(number)
            if pos is None:
                # MSH-1 字段分隔符动态构造
                return FieldNode(
                    state='present',
                    repetitions=(_single_char_value(self.raw[3]),),
                    raw=self.raw[3])
        else:
            pos = number - 1
        return self.fields[pos] if 0 <= pos < len(self.fields) else MISSING


@dataclass
class ParsedMessage:
    segments: list[ParsedSegment]
    encoding: EncodingChars
    charset: str
    issues: list[Issue] = field(default_factory=list)
    # 组级骨架：每项为 (OBR 段或 None, ORC 段或 None, OBX/NTE 段列表)
    groups: list[dict] = field(default_factory=list)

    @property
    def msh(self) -> ParsedSegment:
        return self.segments[0]

    def segment_text(self, number: int) -> str | None:
        node = self.msh.field(number)
        if node in (MISSING, NULL) or node.state != 'present':
            return None
        return node.text

    def fatal_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == 'E']


def parse_envelope(payload: bytes) -> tuple[EncodingChars, str, int, int]:
    """只解析 MSH 头所需的最小信息，供身份登记与故障回执使用。

    返回 (编码字符, Python 字符集名, 头字节长度, 首个 CR 的位置)。
    即使后续正文解码失败，身份（MSH-3/4/10）仍可在字节层尽力登记。
    """
    if len(payload) < 8:
        raise MessageError('帧正文短于最小 MSH 头', 'MESSAGE_TOO_SHORT')
    if payload[0:3] != b'MSH':
        raise MessageError('首段不是 MSH', 'NO_MSH_HEADER')
    field_sep = chr(payload[3])
    encoding_chars = payload[4:8].split(b'\r', 1)[0].decode('ascii', 'replace')
    try:
        enc = EncodingChars.from_msh(field_sep, encoding_chars)
    except EncodingError as exc:
        raise MessageError(str(exc), 'ENCODING_CHARS_INVALID') from exc
    first_cr = payload.find(b'\r')
    header_end = first_cr if first_cr != -1 else len(payload)
    charset = 'utf-8'
    if first_cr != -1:
        try:
            header = payload[:header_end].decode('ascii', errors='replace')
        except UnicodeError as exc:  # pragma: no cover - ascii replace 不会失败
            raise MessageError('MSH 头无法解码', 'MESSAGE_UNDECODABLE') from exc
        parts = header.split(enc.field)
        # parts[0]="MSH<sep><编码字符>"，parts[n] 对应 MSH-(n+1)，
        # 故 MSH-18 字符集位于 parts[17]
        if len(parts) > 17 and parts[17]:
            charset = CHARSETS.get(parts[17].split(enc.component)[0].upper(), 'utf-8')
    return enc, charset, header_end, first_cr if first_cr != -1 else -1


def _decode_text(payload: bytes, charset: str) -> str:
    try:
        return payload.decode(charset)
    except (UnicodeDecodeError, LookupError) as exc:
        raise MessageError(f'按 {charset} 解码失败', 'CHARSET_DECODE_FAILED') from exc


def _split_segments(text: str) -> list[str]:
    if not text.startswith('MSH'):
        raise MessageError('首段不是 MSH', 'NO_MSH_HEADER')
    raw_segments = text.split('\r')
    # 最后一个元素是结尾 CR 后的空串；其余位置出现空段为结构错误
    if raw_segments and raw_segments[-1] == '':
        raw_segments.pop()
    if any(seg == '' for seg in raw_segments):
        raise MessageError('存在空段（连续 CR）', 'EMPTY_SEGMENT')
    return raw_segments


def _parse_msh(raw: str, index: int, limits: Limits) -> tuple[ParsedSegment, EncodingChars, str]:
    field_sep = raw[3]
    if len(raw) < 8:
        raise MessageError('MSH 头不完整', 'MESSAGE_TOO_SHORT')
    encoding_chars = raw[4:8]
    try:
        enc = EncodingChars.from_msh(field_sep, encoding_chars)
    except EncodingError as exc:
        raise MessageError(str(exc), 'ENCODING_CHARS_INVALID') from exc
    # 对完整 MSH 段按字段分隔符切分：parts[0]="MSH"，parts[1]=MSH-2
    # 编码字符，parts[2]=MSH-3 发送应用，parts[8]=MSH-9 消息类型，
    # parts[9]=MSH-10 控制 ID。
    parts = raw.split(enc.field)
    if len(parts) < 2 or parts[1] != encoding_chars:
        raise MessageError('MSH-2 编码字符位置与声明不一致',
                           'ENCODING_CHARS_INVALID')
    fields: list[FieldNode] = []
    msh_map: dict[int, int] = {1: None}  # MSH-1 分隔符动态构造
    # MSH-2 编码字符原样保留，不参与转义解释（反斜杠本身是编码字符）
    fields.append(FieldNode(
        state='present',
        repetitions=(_single_char_value(encoding_chars),),
        raw=encoding_chars))
    msh_map[2] = 0
    # parts[i] 对应 MSH-(i+1)：parts[2]=MSH-3、parts[8]=MSH-9 消息类型、
    # parts[9]=MSH-10 控制 ID。fields[0] 固定为 MSH-2，parts[2:] 依次
    # append 到 fields[1:]；登记映射时直接采用 append 后的真实下标。
    for part_index in range(2, len(parts)):
        hl7_number = part_index + 1
        raw_field = parts[part_index]
        try:
            node = parse_field(raw_field, enc, limits.max_field_chars)
        except EncodingError as exc:
            raise MessageError(str(exc), 'FIELD_TOO_LARGE') from exc
        fields.append(node)
        msh_map[hl7_number] = len(fields) - 1
    charset_name = 'utf-8'
    msh18 = ParsedSegment('MSH', index, fields, raw, msh_map).field(18)
    if msh18.state == 'present':
        declared = (msh18.leaf(0) or '').upper()
        charset_name = CHARSETS.get(declared, 'utf-8')
    return (ParsedSegment('MSH', index, fields, raw, msh_map),
            enc, charset_name)


def _single_char_value(text: str):
    from .encoding import Value, Component
    return Value((Component((text,), text),), text)


def _parse_general(raw: str, index: int, enc: EncodingChars,
                   limits: Limits) -> ParsedSegment:
    name = raw[:3]
    body = raw[4:] if len(raw) > 3 else ''
    raw_fields = body.split(enc.field) if body else []
    fields: list[FieldNode] = []
    for raw_field in raw_fields:
        try:
            fields.append(parse_field(raw_field, enc, limits.max_field_chars))
        except EncodingError as exc:
            raise MessageError(str(exc), 'FIELD_TOO_LARGE') from exc
    return ParsedSegment(name, index, fields, raw)


def _build_groups(message: ParsedMessage) -> None:
    """建立 ORC/OBR 医嘱组骨架并检查段间位置。

    分组规则（ORU^R01 受限子集）：每个 OBR 开一个新医嘱组；ORC 归属其后
    首个 OBR（同 ORC 多 OBR 时后续 OBR 继承该 ORC）。
    """
    groups: list[dict] = []
    current: dict | None = None
    pending_orc: ParsedSegment | None = None
    order_started = False

    for seg in message.segments[1:]:
        if seg.name == 'PID':
            if order_started:
                message.issues.append(Issue(
                    'SEGMENT_POSITION', 'accept', 'E',
                    'PID 出现在医嘱组之后', 'PID', seg.index))
        elif seg.name == 'ORC':
            if current is not None and current['obr'] is None and current['orc'] is not None:
                message.issues.append(Issue(
                    'SEGMENT_POSITION', 'accept', 'E',
                    '连续 ORC 缺少 OBR', 'ORC', seg.index))
            pending_orc = seg
        elif seg.name == 'OBR':
            current = {'orc': pending_orc, 'obr': seg,
                       'items': [], 'segments': []}
            if pending_orc is not None:
                current['segments'].append(pending_orc)
            groups.append(current)
            current['segments'].append(seg)
            order_started = True
        elif seg.name in ('OBX', 'NTE'):
            if current is None or current['obr'] is None:
                message.issues.append(Issue(
                    'SEGMENT_POSITION', 'accept', 'E',
                    f'{seg.name} 缺少所属 OBR 医嘱组', seg.name, seg.index))
                continue
            current['segments'].append(seg)
            # OBX 与 NTE 都入组，NTE 的具体归属在 report 层裁决
            current['items'].append(seg)
        elif seg.name == 'DSC':
            if seg is not message.segments[-1]:
                message.issues.append(Issue(
                    'SEGMENT_POSITION', 'accept', 'E',
                    'DSC 只能作为末段', 'DSC', seg.index))
    # 有 ORC 但从未跟 OBR
    if pending_orc is not None and not order_started:
        message.issues.append(Issue(
            'ORDER_WITHOUT_OBR', 'accept', 'E',
            'ORC 后缺少 OBR 段', 'ORC', pending_orc.index))
    message.groups = groups


def parse_payload(payload: bytes, limits: Limits | None = None) -> ParsedMessage:
    """把整帧字节解码为受限 ORU^R01 消息。"""
    limits = limits or Limits()
    enc, charset, _, _ = parse_envelope(payload)
    text = _decode_text(payload, charset)
    raw_segments = _split_segments(text)
    if len(raw_segments) > limits.max_segments:
        raise MessageError(
            f'段数 {len(raw_segments)} 超过 {limits.max_segments} 限制',
            'TOO_MANY_SEGMENTS')

    segments: list[ParsedSegment] = []
    actual_enc = enc
    actual_charset = charset
    for index, raw_seg in enumerate(raw_segments):
        name = raw_seg[:3]
        if index == 0:
            msh, actual_enc, actual_charset = _parse_msh(raw_seg, index, limits)
            segments.append(msh)
            continue
        if name not in ALLOWED_SEGMENTS:
            raise MessageError(
                f'受限接收端不支持 {name} 段（段序号 {index}）',
                'SEGMENT_NOT_ALLOWED',
                Issue('SEGMENT_NOT_ALLOWED', 'accept', 'E',
                      f'不支持的段类型 {name}', name, index))
        segments.append(_parse_general(raw_seg, index, actual_enc, limits))

    message = ParsedMessage(segments, actual_enc, actual_charset)
    _validate_msh(message)
    _validate_obx(message)
    _build_groups(message)
    return message


def _validate_msh(message: ParsedMessage) -> None:
    msh = message.msh
    message_type = msh.field(9)
    if message_type in (MISSING, NULL) or message_type.state != 'present' \
            or message_type.text != 'ORU^R01':
        message.issues.append(Issue(
            'UNSUPPORTED_MESSAGE_TYPE', 'accept', 'E',
            '仅接收 ORU^R01 消息', 'MSH', 0, field=9))
    version = msh.field(12)
    if version in (MISSING, NULL) or version.state != 'present' \
            or not version.text.startswith('2.'):
        message.issues.append(Issue(
            'UNSUPPORTED_VERSION', 'accept', 'E',
            'MSH-12 未声明受支持的 HL7 v2 版本', 'MSH', 0, field=12))
    for number, code in ((3, 'MISSING_SENDING_APP'),
                         (4, 'MISSING_SENDING_FACILITY'),
                         (10, 'MISSING_CONTROL_ID'),
                         (11, 'MISSING_PROCESSING_ID')):
        node = msh.field(number)
        if node is MISSING or node.state == 'empty':
            message.issues.append(Issue(
                code, 'accept', 'E', f'MSH-{number} 缺失或为空',
                'MSH', 0, field=number))
    # MSH-15/16：声明即必须是合法两态策略
    for number, code in ((15, 'INVALID_ACCEPT_ACK'), (16, 'INVALID_APP_ACK')):
        node = msh.field(number)
        if node.state == 'present' and node.text not in ('AL', 'ER', 'NE', 'SU'):
            message.issues.append(Issue(
                code, 'accept', 'E',
                f'MSH-{number} 合法值为 AL/ER/NE/SU', 'MSH', 0, field=number))


def _validate_obx(message: ParsedMessage) -> None:
    for seg in message.segments:
        if seg.name != 'OBX':
            continue
        type_node = seg.field(2)
        if type_node in (MISSING, NULL) or type_node.state != 'present':
            message.issues.append(Issue(
                'OBX_TYPE_MISSING', 'accept', 'E',
                'OBX-2 值类型缺失或为 null', 'OBX', seg.index, field=2))
        elif type_node.text not in ALLOWED_OBX_TYPES:
            message.issues.append(Issue(
                'OBX_TYPE_UNSUPPORTED', 'accept', 'E',
                f'公开子集仅支持 {sorted(ALLOWED_OBX_TYPES)}，收到 {type_node.text}',
                'OBX', seg.index, field=2))
        status = seg.field(11)
        if status in (MISSING, NULL) or status.state != 'present' or status.text == '':
            message.issues.append(Issue(
                'OBX_STATUS_MISSING', 'accept', 'E',
                'OBX-11 观察结果状态缺失', 'OBX', seg.index, field=11))
