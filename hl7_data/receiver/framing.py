"""MLLP 帧接收层。

规范帧以 VT(0x0B) 开始，以 FS(0x1C)+CR(0x0D) 结束；帧内消息用 CR 分段。
本层只处理字节边界：

- 任意分块可能切断开始字节、结束序列，也可能切断多字节字符（字符解码在
  parser 层按整帧进行，本层不解释字符）；
- 除 CR、FS、VT 外的 ASCII 控制字节一律拒绝，且不猜测正文结束位置；
- 本期不处理压缩帧与非标准转义的帧边界；
- 帧长超过限制时拒绝，避免无限缓冲。

解码器为纯状态机，不持有任何传输连接，因此断连后可检查 ``pending_bytes``
并在重连后继续喂入字节，或按本地策略重放。
"""
from dataclasses import dataclass, field

VT = 0x0B
FS = 0x1C
CR = 0x0D

# 本层允许出现的控制字节（VT 起始、FS/CR 结束序列、CR 段分隔）
_ALLOWED_CONTROL = frozenset({VT, FS, CR})


class FrameError(ValueError):
    """帧级非法状态。code 与 ERR 段使用的问题码一致。"""

    def __init__(self, message, code='ILLEGAL_CONTROL_BYTE', offset=None):
        super().__init__(message)
        self.code = code
        self.offset = offset


@dataclass
class FrameDecoder:
    """增量 MLLP 解码器。

    用法::

        dec = FrameDecoder(max_frame_bytes=...)
        for chunk in transport.chunks():
            frames = dec.feed(chunk)   # 每次 feed 可能产出 0..N 个完整帧
        if dec.pending_bytes:          # 断连时仍有未成帧字节
            ...
    """

    max_frame_bytes: int = 65536
    _buffer: bytearray = field(default_factory=bytearray, init=False)
    _in_frame: bool = field(default=False, init=False)
    _total_offset: int = field(default=0, init=False)

    @property
    def in_frame(self) -> bool:
        return self._in_frame

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()
        self._in_frame = False
        self._total_offset = 0

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[bytes]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError('chunk 必须是字节序列')
        frames: list[bytes] = []
        for byte in chunk:
            frames.extend(self._step(byte))
        return frames

    def _step(self, byte: int) -> list[bytes]:
        self._total_offset += 1
        if byte < 0x20 and byte not in _ALLOWED_CONTROL:
            raise FrameError(
                f'偏移 {self._total_offset} 出现非法控制字节 0x{byte:02X}',
                code='ILLEGAL_CONTROL_BYTE',
                offset=self._total_offset,
            )
        if not self._in_frame:
            if byte == VT:
                self._in_frame = True
                self._buffer.clear()
            else:
                # 帧外只允许静默的 CR（某些实现会在帧间补 CR），
                # 其它任何字节都拒绝，绝不猜测下一帧从哪里开始。
                if byte != CR:
                    raise FrameError(
                        f'偏移 {self._total_offset} 的帧外字节 0x{byte:02X}',
                        code='DATA_OUTSIDE_FRAME',
                        offset=self._total_offset,
                    )
            return []

        # 帧内
        if len(self._buffer) + 1 > self.max_frame_bytes:
            raise FrameError(
                f'帧长度超过 {self.max_frame_bytes} 字节限制',
                code='FRAME_TOO_LARGE',
                offset=self._total_offset,
            )
        if byte == VT:
            raise FrameError(
                f'偏移 {self._total_offset} 在结束序列前出现新的 VT',
                code='UNEXPECTED_VT',
                offset=self._total_offset,
            )
        if byte == FS:
            self._buffer.append(byte)
        elif byte == CR and self._buffer and self._buffer[-1] == FS:
            payload = bytes(self._buffer[:-1])  # 去掉 FS，CR 不入缓冲
            self._buffer.clear()
            self._in_frame = False
            if not payload:
                raise FrameError(
                    '空帧（VT 后直接为 FS CR）',
                    code='EMPTY_FRAME',
                    offset=self._total_offset,
                )
            return [payload]
        else:
            self._buffer.append(byte)
        return []


def frame_message(payload: bytes | bytearray) -> bytes:
    """把消息原文包装成规范 MLLP 帧（回执出站时使用）。"""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError('payload 必须是字节序列')
    return bytes([VT]) + bytes(payload) + bytes([FS, CR])
