"""编码字符、转义层级与字段三态测试。"""
import unittest

from hl7_data.receiver.encoding import (
    EncodingChars, EncodingError, FieldNode, MISSING, NULL,
    parse_field, split_escaped, unescape, escape_text, join_field, is_null,
)

ENC = EncodingChars()


class EncodingTests(unittest.TestCase):
    def test_declared_delimiters_not_default(self):
        # 编码字符顺序固定为 组件^ 重复~ 转义\ 子组件&，这里整体替换字符
        enc = EncodingChars.from_msh('#', '@~!%')
        # # 是字段分隔符；字段内按 @ 组件、~ 重复拆分（! 是转义符）
        node = parse_field('a@b~c', enc, 10000)
        self.assertEqual(len(node.repetitions), 2)
        self.assertEqual(node.repetitions[0].components[0].text, 'a')
        self.assertEqual(node.repetitions[0].components[1].text, 'b')

    def test_invalid_encoding_chars(self):
        with self.assertRaises(EncodingError):
            EncodingChars.from_msh('||', '^~\\&')
        with self.assertRaises(EncodingError):
            EncodingChars.from_msh('|', '^~\\')

    def test_escaped_delimiter_not_resplit(self):
        # \F\ 还原出的 | 只是叶子文本，字段不能再被拆开
        node = parse_field(r'12.5\F\ng/dL', ENC, 10000)
        self.assertEqual(node.state, 'present')
        self.assertEqual(len(node.repetitions[0].components), 1)
        self.assertEqual(node.text, '12.5|ng/dL')

    def test_full_escape_hierarchy(self):
        self.assertEqual(unescape(r'a\F\b\S\c\T\d\R\e\E\f', ENC),
                         'a|b^c&d~e\\f')
        self.assertEqual(unescape('\\X41\\\\X42\\', ENC), 'AB')
        # 非法十六进制转义仍拒绝
        with self.assertRaises(EncodingError):
            unescape('\\X4\\', ENC)
        # 未配对的转义符按普通字符保留（不拒绝整条消息）
        self.assertEqual(unescape('unclosed\\F', ENC), 'unclosed\\F')

    def test_unknown_escape_preserved(self):
        text = r'x\Zzz\y'
        self.assertEqual(unescape(text, ENC), text)

    def test_field_three_states(self):
        self.assertIs(parse_field('abc', ENC, 10000).state, 'present')
        empty = parse_field('', ENC, 10000)
        self.assertEqual(empty.state, 'empty')
        null = parse_field('""', ENC, 10000)
        self.assertEqual(null.state, 'null')
        self.assertTrue(is_null(null))
        self.assertIs(MISSING, MISSING)

    def test_repetitions_components_subcomponents(self):
        node = parse_field('a1&a2^b~c', ENC, 10000)
        self.assertEqual(len(node.repetitions), 2)
        comp = node.repetitions[0].components[0]
        self.assertEqual(comp.subcomponents, ('a1', 'a2'))
        self.assertEqual(node.leaf(0, 1), 'a2')
        self.assertIsNone(node.leaf(9))

    def test_field_length_limit(self):
        with self.assertRaises(EncodingError):
            parse_field('x' * 5, ENC, 4)

    def test_roundtrip_escaping(self):
        original = 'a|b^c&d~e\\f\rg'
        escaped = escape_text(original, ENC)
        self.assertEqual(unescape(escaped, ENC), original)
        node = parse_field(escaped, ENC, 10000)
        self.assertEqual(node.text, original)
        self.assertEqual(join_field(node, ENC), escaped)

    def test_join_null_and_empty(self):
        self.assertEqual(join_field(parse_field('""', ENC, 10000), ENC), '""')
        self.assertEqual(join_field(parse_field('', ENC, 10000), ENC), '')
        with self.assertRaises(ValueError):
            join_field(MISSING, ENC)


if __name__ == '__main__':
    unittest.main()
