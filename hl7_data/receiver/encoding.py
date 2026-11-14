"""HL7 v2 编码字符、转义层级与字段三态。

分隔符由消息的 MSH 段声明（MSH-1 字段分隔符、MSH-2 四个编码字符），
本模块不假定固定为 ``|^~\\&``。

关键规则：先按转义层级切分，再做转义还原；因此 ``\\F\\`` 等转义还原出的
分隔符只是叶子文本，绝不会被重新当作字段分隔符拆分。

字段三态（OBX-5 等位置必须分别保留）：

- 缺失（MISSING）：段里根本没有该字段位置；
- 空（EMPTY）：字段位置存在但内容为空字符串；
- 显式双引号空值（NULL）：整个字段值为 ``""``，即 HL7 null。
"""
from dataclasses import dataclass, field


class _Sentinel:
    # 哨兵也带 state，使调用方可以统一写 node.state 判断，无需先判身份
    state = 'missing'

    def __init__(self, label):
        self._label = label

    def __repr__(self):
        return self._label


MISSING = _Sentinel('MISSING')   # 字段位置不存在
NULL = _Sentinel('NULL')         # 显式 HL7 null：""
NULL.state = 'null'

# 公开子集支持的字符集声明（MSH-18）到 Python 编码的映射
CHARSETS = {
    'UNICODE UTF-8': 'utf-8',
    'UTF-8': 'utf-8',
    '8859/1': 'latin-1',
    'ISO 8859-1': 'latin-1',
    'ASCII': 'ascii',
    '': 'utf-8',
}


class EncodingError(ValueError):
    """编码字符或转义序列非法。"""


@dataclass(frozen=True)
class EncodingChars:
    field: str = '|'
    component: str = '^'
    repetition: str = '~'
    escape: str = '\\'
    subcomponent: str = '&'

    @classmethod
    def from_msh(cls, field_sep: str, encoding_chars: str):
        if len(field_sep) != 1:
            raise EncodingError('MSH-1 字段分隔符必须是单个字符')
        if len(encoding_chars) != 4:
            raise EncodingError('MSH-2 必须声明四个编码字符')
        chars = [field_sep, *encoding_chars]
        if len(set(chars)) != 5 or any(not c.isprintable() for c in chars):
            raise EncodingError('编码字符必须互不相同且可打印')
        return cls(field_sep, *encoding_chars)

    def all_delimiters(self) -> frozenset[str]:
        return frozenset((self.field, self.component,
                          self.repetition, self.escape, self.subcomponent))


def is_missing(node) -> bool:
    return node is MISSING


def is_null(node) -> bool:
    return node is NULL or (hasattr(node, 'state') and node.state == 'null')


def split_escaped(text: str, delim: str, escape_char: str) -> list[str]:
    """按 delim 切分，但跳过转义对 ``\\...\\`` 内部的 delim。

    未配对的转义符按普通字符处理（不报错），真正的闭合校验发生在
    :func:`unescape`，使本函数可用于任意自定义转义符声明。
    """
    parts: list[str] = []
    start = 0
    in_escape = False
    for index, char in enumerate(text):
        if char == escape_char:
            in_escape = not in_escape
        elif char == delim and not in_escape:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def unescape(text: str, enc: EncodingChars) -> str:
    """还原 HL7 转义。未识别的转义按原样保留（不丢数据、不拆分）。"""
    if enc.escape not in text:
        return text
    table = {
        'F': enc.field,
        'S': enc.component,
        'T': enc.subcomponent,
        'R': enc.repetition,
        'E': enc.escape,
        'H': '',
        'N': '',
        '.br': '\r',
    }
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char != enc.escape:
            out.append(char)
            i += 1
            continue
        end = text.find(enc.escape, i + 1)
        if end == -1:
            # 未配对的转义符按普通字符保留，不丢数据、不拒绝整条消息
            out.append(text[i:])
            break
        body = text[i + 1:end]
        if body in table:
            out.append(table[body])
        elif body.startswith('X'):
            digits = body[1:]
            if not digits or len(digits) % 2 != 0:
                raise EncodingError(f'非法十六进制转义: {body}')
            try:
                out.append(bytes.fromhex(digits).decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as exc:
                raise EncodingError(f'非法十六进制转义: {body}') from exc
        else:
            # 未识别的本地转义：原样保留反斜杠与转义体
            out.append(enc.escape + body + enc.escape)
        i = end + 1
    return ''.join(out)


def escape_text(text: str, enc: EncodingChars) -> str:
    """出站文本转义：分隔符与反斜杠转义，换行写为 .br。"""
    mapping = {
        enc.field: 'F',
        enc.component: 'S',
        enc.subcomponent: 'T',
        enc.repetition: 'R',
        enc.escape: 'E',
    }
    out: list[str] = []
    for char in text:
        if char in mapping:
            out.append(enc.escape + mapping[char] + enc.escape)
        elif char == '\r':
            out.append(enc.escape + '.br' + enc.escape)
        else:
            out.append(char)
    return ''.join(out)


@dataclass(frozen=True)
class Component:
    """组件：可能含子组件；leaf 文本均已还原转义。"""
    subcomponents: tuple[str, ...]
    raw: str

    @property
    def text(self) -> str:
        return self.subcomponents[0] if len(self.subcomponents) == 1 else self.raw


@dataclass(frozen=True)
class Value:
    """一次重复的值：可能含多个组件。"""
    components: tuple[Component, ...]
    raw: str

    @property
    def text(self) -> str:
        # 多组件时返回完整原文（如 ORU^R01），单组件返回叶子文本
        if len(self.components) == 1:
            return self.components[0].text
        return self.raw

    def leaf(self, component: int = 0, subcomponent: int = 0) -> str | None:
        try:
            return self.components[component].subcomponents[subcomponent]
        except IndexError:
            return None


@dataclass(frozen=True)
class FieldNode:
    """解析后的字段。state 为 missing/null/present。"""
    state: str = 'present'
    repetitions: tuple[Value, ...] = field(default_factory=tuple)
    raw: str | None = None

    @property
    def value(self) -> Value:
        """首次重复；missing/null 节点访问将报错。"""
        if self.state != 'present':
            raise ValueError(f'字段状态为 {self.state}，无值')
        return self.repetitions[0]

    @property
    def text(self) -> str:
        """首次重复首个组件首子组件的叶子文本。"""
        return self.value.text

    def leaf(self, component: int = 0, subcomponent: int = 0,
             repetition: int = 0) -> str | None:
        """取叶子文本；组件/子组件位置不存在返回 None。"""
        if self.state != 'present':
            return None
        try:
            comp = self.repetitions[repetition].components[component]
            return comp.subcomponents[subcomponent]
        except IndexError:
            return None


def _parse_component(raw: str, enc: EncodingChars) -> Component:
    if enc.subcomponent in raw:
        subs = tuple(unescape(part, enc)
                     for part in _split_level(raw, enc.subcomponent, enc))
    else:
        subs = (unescape(raw, enc),)
    return Component(subs, raw)


def _parse_value(raw: str, enc: EncodingChars) -> Value:
    if enc.component in raw:
        comps = tuple(_parse_component(part, enc)
                      for part in _split_level(raw, enc.component, enc))
    else:
        comps = (_parse_component(raw, enc),)
    return Value(comps, raw)


def _split_repetitions(raw: str, enc: EncodingChars) -> list[str]:
    """重复分隔切分：转义符成对时跳过其内部，未配对时按普通字符处理。"""
    parts: list[str] = []
    start = 0
    in_escape = False
    for index, char in enumerate(raw):
        if char == enc.escape:
            in_escape = not in_escape
        elif char == enc.repetition and not in_escape:
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    return parts


def _split_level(raw: str, delim: str, enc: EncodingChars) -> list[str]:
    """通用层级切分：成对转义内的 delim 跳过；未配对转义符按普通字符。"""
    parts: list[str] = []
    start = 0
    in_escape = False
    for index, char in enumerate(raw):
        if char == enc.escape:
            in_escape = not in_escape
        elif char == delim and not in_escape:
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    return parts


def parse_field(raw: str, enc: EncodingChars, max_field_chars: int) -> FieldNode:
    """把单个字段原文解析为三态 FieldNode。"""
    if len(raw) > max_field_chars:
        raise EncodingError(f'字段长度超过 {max_field_chars} 字符限制')
    if raw == '':
        return FieldNode(state='empty', raw='')
    if raw == '""':
        return FieldNode(state='null', raw=raw)
    if enc.repetition in raw:
        reps = tuple(_parse_value(part, enc)
                     for part in _split_repetitions(raw, enc))
    else:
        reps = (_parse_value(raw, enc),)
    return FieldNode(state='present', repetitions=reps, raw=raw)


def join_field(node: FieldNode, enc: EncodingChars) -> str:
    """FieldNode 还原为 HL7 字段原文（出站构造 ACK 时使用）。"""
    if node is MISSING:
        raise ValueError('MISSING 字段不能序列化')
    if node is NULL or node.state == 'null':
        return '""'
    if node.state == 'empty':
        return ''
    reps = []
    for value in node.repetitions:
        comps = []
        for comp in value.components:
            comps.append(enc.subcomponent.join(
                escape_text(sub, enc) for sub in comp.subcomponents))
        reps.append(enc.component.join(comps))
    return enc.repetition.join(reps)
