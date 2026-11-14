"""报告组装层：把受限段消息组装为可持久化的报告草稿。

本层不做临床判断，只如实建立来源结构：

- 医嘱由 ORC/OBR 组确定；观察路径由「医嘱 + OBX-4 子ID + 重复序号」共同
  决定，绝不按检测代码（OBX-3）去重或覆盖；
- OBX-5 的缺失、空、显式双引号空值（null）三态分别保留；
- NTE 归属最近的合法 OBX（无则归属所属医嘱），不跨组挪动；
- 更正/删除/撤回状态作为带关联证据的新版本留存，找不到原观察时进入
  待关联列表，不伪造正常结果；
- DSC 非空表示续段未收齐，单帧不等同完整报告。
"""
from dataclasses import dataclass, field, asdict
import base64

from .encoding import MISSING, NULL, FieldNode
from .parser import Issue, ParsedMessage, ParsedSegment

# OBX-11 状态分类（HL7 v2 公开子集）
VERSIONED_STATUSES = frozenset(('C', 'D', 'X', 'U', 'R'))
NORMAL_STATUSES = frozenset(('F', 'P', 'W', 'N', 'I'))
ALL_STATUSES = VERSIONED_STATUSES | NORMAL_STATUSES


@dataclass
class Note:
    source_segment: int
    text: str


@dataclass
class Observation:
    source_segment: int
    order_id: str
    group_index: int
    sub_id: str
    repeat_index: int
    identifier: str | None
    identifier_text: str | None
    coding_system: str | None
    value_type: str
    value_state: str                 # present / empty / missing / null
    value_raw: str | None
    decoded: object
    units: str | None
    status: str
    is_versioned: bool
    target_link: dict | None = None
    notes: list[Note] = field(default_factory=list)

    def source_path(self) -> str:
        return (f'group[{self.group_index}]/order={self.order_id}'
                f'/sub_id={self.sub_id}/repeat={self.repeat_index}')

    def to_dict(self) -> dict:
        data = asdict(self)
        return data


@dataclass
class OrderGroup:
    group_index: int
    order_id: str
    orc_segment: int | None
    obr_segment: int
    filler_number: str | None
    notes: list[Note] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


@dataclass
class ReportDraft:
    report_key: str | None
    orders: list[OrderGroup]
    observations: list[Observation]
    pending_links: list[Observation]
    continuation_pointer: str | None
    continuation_style: str | None
    issues: list[Issue]

    @property
    def complete(self) -> bool:
        return self.continuation_pointer is None

    def to_jsonable(self) -> dict:
        return {
            'report_key': self.report_key,
            'complete': self.complete,
            'continuation_pointer': self.continuation_pointer,
            'continuation_style': self.continuation_style,
            'orders': [
                {
                    'group_index': g.group_index,
                    'order_id': g.order_id,
                    'orc_segment': g.orc_segment,
                    'obr_segment': g.obr_segment,
                    'filler_number': g.filler_number,
                    'notes': [n.__dict__ for n in g.notes],
                    'observations': [o.source_path() for o in g.observations],
                }
                for g in self.orders
            ],
            'observations': [o.to_dict() for o in self.observations],
            'pending_links': [o.to_dict() for o in self.pending_links],
            'issues': [asdict(i) for i in self.issues],
        }


def _node_leaf(node: FieldNode, component: int = 0) -> str | None:
    if node in (MISSING, NULL) or node.state != 'present':
        return None
    return node.leaf(component)


def _decode_nm(node: FieldNode, issues: list[Issue], seg: ParsedSegment) -> list:
    decoded = []
    for repeat_index, value in enumerate(node.repetitions):
        raw_text = value.text
        try:
            number = float(raw_text)
        except ValueError:
            issues.append(Issue(
                'OBX_NM_NOT_NUMERIC', 'application', 'E',
                f'NM 观察值不是合法数值: {raw_text!r}',
                'OBX', seg.index, field=5))
            number = None
        decoded.append({'repeat': repeat_index, 'raw': raw_text, 'number': number})
    return decoded


def _decode_ce(node: FieldNode) -> list:
    return [
        {
            'repeat': ri,
            'identifier': value.leaf(0),
            'text': value.leaf(1),
            'coding_system': value.leaf(2),
        }
        for ri, value in enumerate(node.repetitions)
    ]


def _decode_ed(node: FieldNode, issues: list[Issue], seg: ParsedSegment) -> list:
    decoded = []
    for ri, value in enumerate(node.repetitions):
        ed = {
            'repeat': ri,
            'type': value.leaf(0, 0),
            'subtype': value.leaf(1, 0),
            'data_encoding': value.leaf(2, 0),
            'data_pointer': value.leaf(3, 0),
            'raw_data': value.leaf(4, 0),
            'decoded_data_b64': None,
        }
        if ed['data_encoding'] and ed['data_encoding'].upper() == 'BASE64' and ed['raw_data']:
            compact = ''.join(ed['raw_data'].split())
            try:
                ed['decoded_data_b64'] = base64.b64decode(compact, validate=True)
            except (ValueError, TypeError) as exc:
                issues.append(Issue(
                    'OBX_ED_BAD_BASE64', 'application', 'E',
                    f'ED Base64 数据非法: {exc}', 'OBX', seg.index, field=5))
        decoded.append(ed)
    return decoded


def _decode_value(seg: ParsedSegment, issues: list[Issue]) -> tuple[str, str, object, str | None]:
    node = seg.field(5)
    if node is MISSING or node.state == 'missing':
        return 'missing', 'missing', None, None
    if node is NULL or node.state == 'null':
        return 'null', 'null', None, node.raw
    if node.state == 'empty':
        return 'empty', 'empty', None, node.raw
    type_node = seg.field(2)
    value_type = type_node.text if type_node.state == 'present' else ''
    if value_type == 'NM':
        decoded = _decode_nm(node, issues, seg)
    elif value_type == 'CE':
        decoded = _decode_ce(node)
    elif value_type == 'ED':
        decoded = _decode_ed(node, issues, seg)
    else:  # ST 及其它已在 parser 层限定的类型
        decoded = [{'repeat': ri, 'text': v.text} for ri, v in enumerate(node.repetitions)]
    return 'present', 'present', decoded, node.raw


def _order_id(group: dict) -> tuple[str, str | None]:
    obr = group['obr']
    orc = group['orc']
    # ORC-2 申请单号 / ORC-3 申请方医嘱号 / OBR-2 申请单 / OBR-3 申请方医嘱号
    candidates = []
    if orc is not None:
        candidates += [_node_leaf(orc.field(2)), _node_leaf(orc.field(3))]
    candidates += [_node_leaf(obr.field(2)), _node_leaf(obr.field(3))]
    order_id = next((c for c in candidates if c), f'ANON-G{group["index"]}')
    filler = _node_leaf(obr.field(3)) or (_node_leaf(orc.field(3)) if orc else None)
    return order_id, filler


def assemble_report(message: ParsedMessage) -> ReportDraft:
    issues = list(message.issues)
    orders: list[OrderGroup] = []
    observations: list[Observation] = []
    pending: list[Observation] = []

    for group_index, group in enumerate(message.groups):
        if group['obr'] is None:
            issues.append(Issue(
                'ORDER_WITHOUT_OBR', 'accept', 'E',
                '医嘱组缺少 OBR 段', 'ORC',
                group['orc'].index if group['orc'] else None))
            continue
        order_id, filler = _order_id({**group, 'index': group_index})
        order = OrderGroup(
            group_index=group_index,
            order_id=order_id,
            orc_segment=group['orc'].index if group['orc'] else None,
            obr_segment=group['obr'].index,
            filler_number=filler,
        )
        orders.append(order)
        current_obs: Observation | None = None

        for seg in group['items']:
            if seg.name == 'NTE':
                note = Note(seg.index, _note_text(seg))
                if current_obs is not None:
                    current_obs.notes.append(note)
                else:
                    order.notes.append(note)
                continue

            if seg.name != 'OBX':
                continue
            status_node = seg.field(11)
            status = status_node.text if status_node.state == 'present' else ''
            if status == '':
                issues.append(Issue(
                    'OBX_STATUS_MISSING', 'accept', 'E',
                    'OBX-11 观察结果状态缺失', 'OBX', seg.index, field=11))
            elif status not in ALL_STATUSES:
                issues.append(Issue(
                    'OBX_STATUS_UNKNOWN', 'application', 'W',
                    f'未知 OBX-11 状态 {status}，按普通版本留存',
                    'OBX', seg.index, field=11))
            sub_id = _node_leaf(seg.field(4)) or ''
            id_node = seg.field(3)
            identifier = _node_leaf(id_node, 0)
            identifier_text = _node_leaf(id_node, 1)
            coding_system = _node_leaf(id_node, 2)
            units = _node_leaf(seg.field(6))
            value_state, _, decoded, raw = _decode_value(seg, issues)
            is_versioned = status in VERSIONED_STATUSES
            link = None
            if is_versioned:
                # 更正/删除/撤回的关联证据：优先 OBX-4 子ID，其次 OBX-3 标识
                link = {
                    'status': status,
                    'sub_id': sub_id,
                    'identifier': identifier,
                    'order_id': order_id,
                }

            type_node = seg.field(2)
            obs_type = type_node.text if type_node.state == 'present' else ''
            repeat_count = max(1, len(seg.field(5).repetitions)
                               if seg.field(5).state == 'present' else 1)
            for repeat_index in range(repeat_count):
                obs = Observation(
                    source_segment=seg.index,
                    order_id=order_id,
                    group_index=group_index,
                    sub_id=sub_id,
                    repeat_index=repeat_index,
                    identifier=identifier,
                    identifier_text=identifier_text,
                    coding_system=coding_system,
                    value_type=obs_type,
                    value_state=value_state,
                    value_raw=raw,
                    decoded=_single_repeat(decoded, repeat_index),
                    units=units,
                    status=status,
                    is_versioned=is_versioned,
                    target_link=link,
                )
                observations.append(obs)
                order.observations.append(obs)
                if is_versioned:
                    pending.append(obs)
            current_obs = observations[-1]

    # 尝试在本帧内关联版本（同一来源路径已有非版本化观察）
    resolved = set()
    normal_paths = {
        (o.order_id, o.sub_id, o.identifier)
        for o in observations if not o.is_versioned
    }
    still_pending = []
    for obs in pending:
        key = (obs.target_link['order_id'],
               obs.target_link['sub_id'],
               obs.target_link['identifier'])
        if key in normal_paths:
            obs.target_link['resolved_in_frame'] = True
            resolved.add(id(obs))
        else:
            still_pending.append(obs)

    pointer, style = _dsc(message)
    report_key = _report_key(message)
    return ReportDraft(
        report_key=report_key,
        orders=orders,
        observations=observations,
        pending_links=still_pending,
        continuation_pointer=pointer,
        continuation_style=style,
        issues=issues,
    )


def _single_repeat(decoded, repeat_index):
    if decoded is None:
        return None
    for item in decoded:
        if item.get('repeat') == repeat_index:
            return item
    return None


def _note_text(seg: ParsedSegment) -> str:
    node = seg.field(3)
    if node in (MISSING, NULL) or node.state != 'present':
        return ''
    return node.repetitions[0].text


def _dsc(message: ParsedMessage) -> tuple[str | None, str | None]:
    dsc_segments = [s for s in message.segments if s.name == 'DSC']
    if not dsc_segments:
        return None, None
    dsc = dsc_segments[0]
    pointer_node = dsc.field(1)
    if pointer_node in (MISSING, NULL) or pointer_node.state != 'present' \
            or pointer_node.text == '':
        return None, None
    style_node = dsc.field(2)
    style = style_node.text if style_node.state == 'present' else None
    return pointer_node.text, style


def _report_key(message: ParsedMessage) -> str:
    """报告身份：发送应用+机构+患者+医嘱填充号。续段共享同一报告。"""
    pid = next((s for s in message.segments if s.name == 'PID'), None)
    patient = _node_leaf(pid.field(3)) if pid else None
    fillers = []
    for group in message.groups:
        if group['obr'] is not None:
            filler = _node_leaf(group['obr'].field(3))
            if filler:
                fillers.append(filler)
    parts = [
        message.segment_text(3) or '',
        message.segment_text(4) or '',
        patient or '',
        '|'.join(fillers),
    ]
    return '\x1f'.join(parts)
