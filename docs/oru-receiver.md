# ORU^R01 受限接收端

`hl7_data/receiver/` 是合作实验室合成激素检测记录的 Python 资料入口，只做
**受限接收与留存**，不做任何临床判断。全部数据均为合成资料，不接入真实医疗系统。

## 分层与职责

四层严格分开，断连、重送与应用暂时失败都可按本地调度重演：

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `framing.py` | MLLP 字节帧状态机（VT 开头、FS+CR 结束、CR 分段），容忍任意分块 | 不解释字符、不猜测正文结束 |
| `encoding.py` | 消息声明的分隔符、组件/重复/子组件/转义层级、字段三态（缺失/空/`""`空值） | 转义还原出的分隔符不再拆字段 |
| `parser.py` | 整帧解码为受限段 MSH/PID/ORC/OBR/OBX/NTE/DSC，结构问题收集为可定位 `Issue` | 非允许段直接拒绝 |
| `report.py` | 医嘱组、观察来源路径、NTE 归属、版本留存、DSC 续段 | 不按检测代码去重、不伪造正常结果 |
| `ack.py` | 两阶段确认（接收/应用）、MSA 控制 ID 关联、ERR 定位 | 只构造字节，不发送 |
| `ledger.py` | SQLite 原文证据、报告数据、回执账（outbox） | 原文只插入不覆盖 |
| `service.py` | 编排、身份裁决、本地调度与离线重解码 | 传输可注入，不绑定 socket |
| `transport.py` | `ChunkTransport`（测试/演示）与 `SocketTransport`（回环/部署） | — |

## 关键规则

- **帧边界**：非法控制字节（VT/FS/CR 之外的 `<0x20`）拒绝；帧外字节拒绝；
  本期不处理压缩与非标准转义帧边界；单帧超限拒绝。
- **MSH 特殊位置**：MSH-1 字段分隔符、MSH-2 四个编码字符由消息声明；
  `field(n)` 严格按 HL7 字段号取值（field(9) 消息类型、field(10) 控制 ID、
  field(15/16) 确认策略、field(18) 字符集）。
- **观察来源路径**：`group[i]/order=<医嘱>/sub_id=<OBX-4>/repeat=<重复序号>`，
  医嘱 + 子 ID + 重复序号共同决定路径，复测与同代码结果各自留存。
- **字段三态**：缺失（无字段位）、空（`|` 间为空）、显式 null（`""`）分别保留。
- **NTE 归属**：最近的合法 OBX；OBX 之前的 NTE 归属医嘱，不跨组挪动。
- **版本留存**：OBX-11 的 C/D/X/U/R 作为带关联证据的新版本；找不到原观察
  时进 `pending_links` 待关联，不生成伪造正常结果。
- **DSC 续段**：DSC-1 非空时报告保持 OPEN，单帧不等同完整报告；续段按
  报告键（发送应用+机构+患者+医嘱填充号）汇聚。
- **消息身份**：发送应用 + 发送机构 + MSH-10。同身份同原文重送复用接收回执；
  同身份不同内容记 `content_conflicts` 并回 CE，不覆盖首次证据。
- **两阶段确认**：MSH-15 接收确认、MSH-16 应用确认（AL/ER/NE/SU）。只有
  原文持久化后才发 AA 接收确认；结构/组级处理成功后才允许 AA 应用确认；
  失败按阶段回 CE/CR（接收）或 AE/AR（应用），ERR 带段-序号-字段定位。
  确认的 MSA-2 回填原消息 MSH-10。
- **回执账**：确认先入 outbox（PENDING），`flush_outbox` 真正写传输后才
  标记 SENT；TCP 写入失败保持 PENDING，不把写入等同对方收到。
- **限制**：单帧字节、单字段字符、段数、未完成报告数、待关联数均可在
  `limits.Limits` 配置。

## 运行

```sh
# 单元测试（57 个，含逐字节分块、转义层级、两阶段确认、socket 回环）
python -m unittest discover -s tests -v

# 端到端演示：五个场景，独立打印观察组路径与实际发出的两阶段确认
PYTHONPATH=. python3 -I demos/oru_receiver_demo.py

# 编译检查
python -m compileall -q hl7_data tests demos
```

## 持久化表（SQLite）

`raw_messages`（原文证据，只增不改）、`content_conflicts`、`frame_events`、
`redecodes`（离线重解码，不覆盖证据）、`reports`/`report_parts`、`orders`、
`observations`、`pending_links`、`receipts`（每消息每阶段一行回执）、
`outbox`（PENDING/SENT 确认账）。
