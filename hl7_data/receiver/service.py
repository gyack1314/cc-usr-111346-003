"""接收编排层：把帧接收、解码、报告组装与回执账串成可重演流水线。

关键顺序保证：

1. 帧非法时无法建立消息身份，只记帧事件，绝不猜测正文结束；
2. 任何可识别身份的原文都先落 ``raw_messages``（首次证据不可变），
   只有落库成功后才允许产生成功接收确认（AA）；
3. 同身份同原文重送复用既有接收回执，不重复落证据；同身份不同内容记
   内容冲突并回 CR；
4. 结构/组级问题按阶段产生 CE/CR（accept）或 AE/AR（application），
   ERR 可定位；应用处理器抛临时错误时应用确认保持 PENDING 可重演；
5. outbox 与传输分离，TCP 写入失败/断连不改变确认账，调度器重放即可。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from .framing import FrameDecoder, FrameError, frame_message
from .parser import (
    Issue, MessageError, parse_envelope, parse_payload,
)
from .report import assemble_report
from .ack import AckPolicy, build_ack
from .ledger import Ledger, identity_key
from .limits import Limits


class AppTemporaryError(RuntimeError):
    """应用处理器暂时失败：应用确认留 PENDING，等待本地调度重演。"""


class AppRejectError(RuntimeError):
    """应用处理器确定性拒绝：产生 AR/AE，不再重试。"""

    def __init__(self, message, code='APP_REJECTED', severity='E'):
        super().__init__(message)
        self.code = code
        self.severity = severity


@dataclass
class ServiceConfig:
    limits: Limits = field(default_factory=Limits)
    # 应用处理器：接收 ReportDraft 与消息身份，抛 AppTemporaryError/AppRejectError
    app_handler: object | None = None
    local_app: str = 'RESEARCH-RECEIVER'
    local_facility: str = 'RESEARCH-LOCAL'


@dataclass
class FrameResult:
    outcome: str                      # accepted / duplicate / rejected
    message_pk: int | None
    identity: str | None
    accept_ack: str | None = None     # AA/CE/CR/None(策略不要确认)
    app_ack: str | None = None
    issues: list[Issue] = field(default_factory=list)
    report_status: str | None = None


class _FatalHeader:
    """致命解码失败时构造 ACK 所需的最小 MSH 头视图。"""

    class _Node:
        def __init__(self, text='', state='missing'):
            self._text = text
            self.state = state

        @property
        def text(self):
            return self._text

        def leaf(self, *_):
            return self._text or None

    def __init__(self, payload: bytes, enc, charset: str):
        self.encoding = enc
        self.charset = charset
        first_cr = payload.find(b'\r')
        header = payload[:first_cr if first_cr != -1 else len(payload)] \
            .decode(charset, errors='replace')
        parts = header.split(enc.field)
        # parts[0]="MSH<sep><编码字符>"，parts[1] 是前导分隔符产生的空占位，
        # parts[2] 为 MSH-3，即 parts[n] 对应 MSH-(n+1)
        values = {i + 1: (parts[i] if i < len(parts) else '')
                  for i in range(1, len(parts))}
        self._values = values

    @property
    def msh(self):
        outer = self

        class _MSH:
            @staticmethod
            def field(number):
                text = outer._values.get(number, '')
                return outer._Node(text, 'present' if text else 'empty')
        return _MSH()


class ReceiverService:
    def __init__(self, ledger: Ledger, config: ServiceConfig | None = None):
        self.ledger = ledger
        self.config = config or ServiceConfig()
        self.decoder = FrameDecoder(self.config.limits.max_frame_bytes)

    # ---- 入口 -----------------------------------------------------------
    def handle_chunk(self, chunk: bytes) -> list[FrameResult]:
        """喂入任意字节块，返回本次成帧消息的处理结果。"""
        try:
            frames = self.decoder.feed(chunk)
        except FrameError as exc:
            self.ledger.record_frame_event(
                None, 'frame_error',
                json.dumps({'code': exc.code, 'message': str(exc),
                            'offset': exc.offset}, ensure_ascii=False))
            self.decoder.reset()
            return [FrameResult('rejected', None, None,
                                issues=[Issue(exc.code, 'accept', 'E', str(exc))])]
        return [self.handle_frame(frame) for frame in frames]

    def handle_frame(self, payload: bytes) -> FrameResult:
        # 1) 先尽力解析 MSH 头，建立身份；致命错误也要留痕
        try:
            enc, charset, _, _ = parse_envelope(payload)
            header_view = None
        except MessageError as exc:
            self.ledger.record_frame_event(
                None, 'undecodable',
                json.dumps({'code': exc.code, 'message': str(exc)},
                           ensure_ascii=False))
            issue = exc.issue or Issue(exc.code, 'accept', 'E', str(exc))
            return FrameResult('rejected', None, None, issues=[issue])

        # MSH-3/4/10 在字节层按声明分隔符切出（即使正文解码失败也可登记）
        try:
            header_view = _FatalHeader(payload, enc, charset)
            app = header_view.msh.field(3).text
            fac = header_view.msh.field(4).text
            control_id = header_view.msh.field(10).text
        except Exception as exc:  # pragma: no cover - 防御性
            issue = Issue('IDENTITY_UNRECOVERABLE', 'accept', 'E',
                          f'消息身份无法恢复: {exc}')
            self.ledger.record_frame_event(None, 'identity_error', str(exc))
            return FrameResult('rejected', None, None, issues=[issue])

        key = identity_key(app, fac, control_id)

        # 2) 原文先持久化（首次证据不可变）；重送/冲突在此裁决
        existing = self.ledger.find_identity(key)
        import hashlib
        incoming_sha = hashlib.sha256(payload).hexdigest()
        if existing is not None and existing['sha256'] != incoming_sha:
            self.ledger.record_conflict(key, existing['sha256'], payload)
            issue = Issue('IDENTITY_CONTENT_CONFLICT', 'accept', 'E',
                          f'身份 {key!r} 已有不同原文，拒绝冲突重送',
                          'MSH', 0, field=10)
            ack = self._issue_ack(header_view, payload, key, None,
                                  'accept', False, [issue],
                                  ack_control_id=self._ack_id(existing['message_pk'],
                                                              'accept'))
            return FrameResult('rejected', existing['message_pk'], key,
                               accept_ack=ack, issues=[issue])

        row = self.ledger.insert_raw(key, app, fac, control_id, payload)
        message_pk = row['message_pk']
        duplicate = existing is not None
        self.ledger.record_frame_event(
            message_pk, 'raw_persisted' if not duplicate else 'duplicate_redelivery',
            json.dumps({'sha256': incoming_sha, 'bytes': len(payload)}))

        # 3) 解码（致命失败：只有原文证据，无成功接收确认）
        try:
            message = parse_payload(payload, self.config.limits)
        except MessageError as exc:
            issue = exc.issue or Issue(exc.code, 'accept', 'E', str(exc))
            ack = self._issue_ack(header_view, payload, key, message_pk,
                                  'accept', False, [issue],
                                  ack_control_id=self._ack_id(message_pk, 'accept'))
            return FrameResult('rejected', message_pk, key,
                               accept_ack=ack, issues=[issue])

        policy = AckPolicy.from_message(message)
        all_issues = list(message.issues)

        # 4) 报告组装（组级问题、待关联、续段）
        draft = assemble_report(message)
        all_issues.extend(draft.issues)
        accept_errors = [i for i in all_issues if i.phase == 'accept'
                         and i.severity == 'E']

        if not accept_errors:
            if self.ledger.open_report_count() >= self.config.limits.max_open_reports \
                    and not draft.complete:
                accept_errors.append(Issue(
                    'OPEN_REPORT_LIMIT', 'accept', 'E',
                    f"未完成报告达到 {self.config.limits.max_open_reports} 上限，"
                    '续段暂不能接收', 'DSC'))
            elif len(self.ledger.list_pending_links()) \
                    >= self.config.limits.max_pending_links and draft.pending_links:
                accept_errors.append(Issue(
                    'PENDING_LINK_LIMIT', 'accept', 'E',
                    f"待关联观察达到 {self.config.limits.max_pending_links} 上限",
                    'OBX'))

        report_status = None
        if not accept_errors:
            saved = self.ledger.persist_draft(draft, message_pk,
                                              segment_index=len(message.segments))
            report_status = saved['status']

        # 5) 接收确认：原文已持久化且无 accept 级错误才 AA
        accept_code = None
        if policy.wants('accept', success=not accept_errors):
            accept_code = self._issue_ack(
                message, payload, key, message_pk, 'accept',
                success=not accept_errors,
                issues=accept_errors,
                ack_control_id=self._ack_id(message_pk, 'accept'),
                duplicate=duplicate)

        if accept_errors:
            return FrameResult('rejected', message_pk, key,
                               accept_ack=accept_code,
                               issues=accept_errors,
                               report_status=report_status)

        # 6) 应用阶段：结构/组级成功后才运行；临时失败留 PENDING 重演
        app_issues = [i for i in all_issues if i.phase == 'application'
                      and i.severity == 'E']
        app_success = not app_issues
        app_text = ''
        if not app_issues and self.config.app_handler is not None:
            try:
                self.config.app_handler(draft, {
                    'identity': key, 'message_pk': message_pk,
                    'control_id': control_id,
                })
            except AppTemporaryError as exc:
                # 应用暂时失败：登记事件但不发失败确认，outbox 不建成功项，
                # 由 schedule_application 稍后重演
                self.ledger.record_frame_event(
                    message_pk, 'app_temporary_failure', str(exc))
                return FrameResult('accepted' if not duplicate else 'duplicate',
                                   message_pk, key, accept_ack=accept_code,
                                   app_ack=None, issues=all_issues,
                                   report_status=report_status)
            except AppRejectError as exc:
                app_success = False
                app_issues.append(Issue(exc.code, 'application', exc.severity,
                                        str(exc)))
            else:
                app_text = '应用处理成功'

        app_code = None
        if policy.wants('application', success=app_success):
            app_code = self._issue_ack(
                message, payload, key, message_pk, 'application',
                success=app_success, issues=app_issues,
                ack_control_id=self._ack_id(message_pk, 'application'),
                app_text=app_text, duplicate=duplicate)

        return FrameResult('accepted' if not duplicate else 'duplicate',
                           message_pk, key, accept_ack=accept_code,
                           app_ack=app_code, issues=all_issues,
                           report_status=report_status)

    # ---- 确认账 ----------------------------------------------------------
    def _ack_id(self, message_pk: int | None, phase: str) -> str:
        if message_pk is not None:
            existing = self.ledger.get_receipt(message_pk, phase)
            if existing is not None:
                return existing['ack_control_id']
        return f'ACK-{uuid.uuid4().hex[:16].upper()}'

    def _issue_ack(self, view, payload: bytes, key: str,
                   message_pk: int | None, phase: str, success: bool,
                   issues: list[Issue], ack_control_id: str,
                   app_text: str = '', duplicate: bool = False) -> str:
        # 重送复用既有回执：同身份同原文、同阶段已发过则不新建
        if message_pk is not None and duplicate:
            existing = self.ledger.get_receipt(message_pk, phase)
            if existing is not None:
                return existing['ack_code']
        outcome = build_ack(
            original=view, phase=phase, success=success, issues=issues,
            ack_control_id=ack_control_id, app_text=app_text)
        if message_pk is not None:
            self.ledger.record_receipt(
                message_pk, phase, ack_control_id, outcome.code,
                'SUCCESS' if success else 'FAILURE', issues)
        self.ledger.enqueue_ack(
            message_pk=message_pk, identity_key=key, phase=phase,
            ack_control_id=ack_control_id, ack_code=outcome.code,
            payload=frame_message(outcome.payload))
        return outcome.code

    # ---- 本地调度 --------------------------------------------------------
    def flush_outbox(self, transport) -> dict:
        """尝试把 PENDING 确认真正写入传输；失败保持 PENDING 可重演。"""
        sent, failed = [], []
        for row in self.ledger.pending_outbox():
            try:
                transport.write(row['payload'])
            except OSError as exc:
                self.ledger.mark_outbox(row['outbox_pk'], 'PENDING', str(exc))
                failed.append((row['outbox_pk'], str(exc)))
            else:
                self.ledger.mark_outbox(row['outbox_pk'], 'SENT')
                sent.append(row['outbox_pk'])
        return {'sent': sent, 'failed': failed}

    def schedule_application(self) -> list[FrameResult]:
        """重演应用暂时失败的消息：从已持久化原文重新走应用阶段。

        选取条件：有 app_temporary_failure 事件、尚无 application 回执。
        """
        results = []
        failed_pks = [r['message_pk'] for r in self.ledger.conn.execute(
            "SELECT DISTINCT message_pk FROM frame_events "
            "WHERE event_type='app_temporary_failure' ORDER BY message_pk"
        ).fetchall()]
        for pk in failed_pks:
            if self.ledger.get_receipt(pk, 'application') is not None:
                continue
            row = self.ledger.get_raw(pk)
            results.append(self._replay_application(pk, row['payload']))
        return results

    def _replay_application(self, message_pk: int, payload: bytes) -> FrameResult:
        message = parse_payload(payload, self.config.limits)
        policy = AckPolicy.from_message(message)
        draft = assemble_report(message)
        app_issues = [i for i in draft.issues if i.phase == 'application'
                      and i.severity == 'E']
        success = not app_issues
        text = ''
        if not app_issues and self.config.app_handler is not None:
            try:
                self.config.app_handler(draft, {
                    'identity': identity_key(
                        message.segment_text(3), message.segment_text(4),
                        message.segment_text(10)),
                    'message_pk': message_pk,
                    'control_id': message.segment_text(10),
                })
            except AppTemporaryError as exc:
                self.ledger.record_frame_event(
                    message_pk, 'app_temporary_failure', str(exc))
                return FrameResult('accepted', message_pk, None,
                                   issues=draft.issues)
            except AppRejectError as exc:
                success = False
                app_issues.append(Issue(exc.code, 'application',
                                        exc.severity, str(exc)))
            else:
                text = '应用处理成功（重演）'
        code = None
        if policy.wants('application', success=success):
            code = self._issue_ack(
                message, payload,
                identity_key(message.segment_text(3), message.segment_text(4),
                             message.segment_text(10)),
                message_pk, 'application', success=success,
                issues=app_issues,
                ack_control_id=self._ack_id(message_pk, 'application'),
                app_text=text)
        return FrameResult('accepted', message_pk, None, app_ack=code,
                           issues=app_issues)

    def redecode_offline(self, message_pk: int) -> dict:
        """对已收原文离线重新解码，不覆盖首次接收证据。"""
        row = self.ledger.get_raw(message_pk)
        if row is None:
            raise KeyError(f'message_pk={message_pk} 不存在')
        message = parse_payload(row['payload'], self.config.limits)
        draft = assemble_report(message)
        result = draft.to_jsonable()
        self.ledger.insert_redecode(
            message_pk,
            json.dumps(result, ensure_ascii=False, default=str),
            json.dumps([i.__dict__ for i in draft.issues], ensure_ascii=False))
        return result

    # ---- 传输驱动 --------------------------------------------------------
    def drive_transport(self, transport, max_reads: int = 1000) -> list[FrameResult]:
        """从可注入字节传输读取直到断连（read 返回空），返回处理结果。"""
        results: list[FrameResult] = []
        for _ in range(max_reads):
            chunk = transport.read(4096)
            if not chunk:
                break
            results.extend(self.handle_chunk(chunk))
        return results
