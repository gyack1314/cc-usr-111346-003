"""接收端限制。

单帧、单字段与未完成报告状态均有上限，触发上限属于可定位的拒绝，
不通过猜测正文结束等方式绕过。数值均可按部署环境调整。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_frame_bytes: int = 65536          # 单帧（含 VT/FS/CR）字节数
    max_field_chars: int = 10000          # 单字段解码后字符数
    max_segments: int = 2000              # 单消息段数
    max_open_reports: int = 50            # 未完成（续段未收齐）报告数
    max_pending_links: int = 1000         # 待关联观察版本上限
    max_raw_replay: int = 10000           # 一次离线重解批处理上限
