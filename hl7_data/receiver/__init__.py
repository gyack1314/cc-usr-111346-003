"""ORU^R01 受限接收端。

分层设计，各层只通过显式数据结构耦合，便于断连、重送与本地调度重演：

- framing  : MLLP 字节帧接收，容忍任意分块，拒绝非法控制字节
- encoding : HL7 分隔符、转义层级与字段三态（缺失/空/显式空值）
- parser   : 帧正文解码为受限段消息（MSH/PID/ORC/OBR/OBX/NTE/DSC）
- report   : 医嘱组、观察子项、注释归属、版本留存与续段组装
- ack      : 两阶段确认（接收确认/应用确认）与可定位 ERR
- ledger   : SQLite 原文证据、报告数据与回执账（outbox）
- service  : 编排与本地调度，transport 为可注入字节传输
"""
from .framing import FrameDecoder, FrameError, frame_message, VT, FS, CR
from .encoding import MISSING, NULL, EncodingChars, FieldNode, is_missing, is_null
from .parser import ParsedMessage, parse_payload, parse_envelope, MessageError, Issue
from .report import assemble_report, ReportDraft
from .ack import AckPolicy, build_ack
from .ledger import Ledger
from .service import ReceiverService, AppTemporaryError, AppRejectError, ServiceConfig
from .transport import ChunkTransport, SocketTransport
from .limits import Limits

__all__ = [
    'FrameDecoder', 'FrameError', 'frame_message', 'VT', 'FS', 'CR',
    'MISSING', 'NULL', 'EncodingChars', 'FieldNode', 'is_missing', 'is_null',
    'ParsedMessage', 'parse_payload', 'parse_envelope', 'MessageError', 'Issue',
    'assemble_report', 'ReportDraft',
    'AckPolicy', 'build_ack',
    'Ledger', 'ReceiverService', 'AppTemporaryError', 'AppRejectError',
    'ServiceConfig',
    'ChunkTransport', 'SocketTransport', 'Limits',
]
