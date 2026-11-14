"""可注入的字节传输。

接收器只依赖 :class:`ByteTransport` 协议（read 任意长度字节、write 整帧），
不依赖具体 socket，因此：

- 测试与演示用 :class:`ChunkTransport` 精确控制分块（可切断 VT、FS CR 与
  多字节字符）；
- 真实部署用 :class:`SocketTransport` 包装标准库 socket；
- 断连恢复、重送与应用暂时失败后的重演全部可以在本地离线驱动。
"""
from typing import Protocol


class ByteTransport(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class ChunkTransport:
    """按预设分块投递字节的内存传输；write 收集出站帧供独立核对。"""

    def __init__(self, chunks: list[bytes] | None = None):
        self._chunks = list(chunks or [])
        self._pos = 0
        self.written: list[bytes] = []
        self.closed = False

    def add_chunk(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def read(self, size: int) -> bytes:
        if self._pos >= len(self._chunks):
            return b''
        chunk = self._chunks[self._pos]
        self._pos += 1
        if size and len(chunk) > size:
            head, tail = chunk[:size], chunk[size:]
            self._chunks.insert(self._pos, tail)
            return head
        return chunk

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.closed = True

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._chunks)


class SocketTransport:
    """标准库 socket 的薄封装，分块由网络实际交付决定。"""

    def __init__(self, sock):
        self._sock = sock

    def read(self, size: int = 4096) -> bytes:
        return self._sock.recv(size)

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def close(self) -> None:
        try:
            self._sock.shutdown(1)
        except OSError:
            pass
        self._sock.close()
