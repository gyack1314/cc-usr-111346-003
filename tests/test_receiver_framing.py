"""MLLP 帧接收层测试：任意分块、跨边界、非法控制字节、超限。"""
import unittest

from hl7_data.receiver.framing import FrameDecoder, FrameError, frame_message, VT, FS, CR


def frame(payload: bytes) -> bytes:
    return bytes([VT]) + payload + bytes([FS, CR])


class FramingTests(unittest.TestCase):
    def test_single_byte_chunks_across_boundaries(self):
        payload = 'MSH|...\rOBX|中文'.encode('utf-8')
        wire = frame(payload)
        dec = FrameDecoder()
        got = []
        for byte in wire:  # 逐字节投递，切断 VT、FS CR 和多字节字符
            got.extend(dec.feed(bytes([byte])))
        self.assertEqual(got, [payload])
        self.assertEqual(dec.pending_bytes, 0)

    def test_chunks_split_inside_end_sequence(self):
        wire = frame(b'MSH|A')
        for cut in range(1, len(wire)):
            dec = FrameDecoder()
            frames = dec.feed(wire[:cut]) + dec.feed(wire[cut:])
            self.assertEqual(frames, [b'MSH|A'], f'切割点 {cut} 失败')

    def test_multiple_frames_in_one_chunk_and_gap_cr(self):
        dec = FrameDecoder()
        wire = frame(b'A') + bytes([CR]) + frame(b'B')
        self.assertEqual(dec.feed(wire), [b'A', b'B'])

    def test_illegal_control_bytes_rejected_no_guess(self):
        dec = FrameDecoder()
        with self.assertRaisesRegex(FrameError, '非法控制字节'):
            dec.feed(bytes([VT]) + b'MSH\x00X')
        # 非法后不猜测正文结束：状态机复位，必须重新等 VT
        dec.reset()
        self.assertEqual(dec.feed(frame(b'MSH|OK')), [b'MSH|OK'])

    def test_data_outside_frame_rejected(self):
        dec = FrameDecoder()
        with self.assertRaisesRegex(FrameError, '帧外字节'):
            dec.feed(b'JUNK' + frame(b'A'))

    def test_vt_inside_frame_rejected(self):
        dec = FrameDecoder()
        with self.assertRaisesRegex(FrameError, 'VT'):
            dec.feed(bytes([VT]) + b'A' + bytes([VT]) + b'B' + bytes([FS, CR]))

    def test_frame_size_limit(self):
        dec = FrameDecoder(max_frame_bytes=5)
        with self.assertRaisesRegex(FrameError, '帧长度'):
            dec.feed(frame(b'0123456789'))

    def test_empty_frame_rejected(self):
        dec = FrameDecoder()
        with self.assertRaisesRegex(FrameError, '空帧'):
            dec.feed(bytes([VT, FS, CR]))

    def test_pending_after_disconnect(self):
        dec = FrameDecoder()
        dec.feed(bytes([VT]) + b'MSH|partial')
        self.assertTrue(dec.in_frame)
        self.assertEqual(dec.pending_bytes, len(b'MSH|partial'))

    def test_frame_message_roundtrip(self):
        self.assertEqual(frame_message(b'X'), bytes([VT]) + b'X' + bytes([FS, CR]))


if __name__ == '__main__':
    unittest.main()
