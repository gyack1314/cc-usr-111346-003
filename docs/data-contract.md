# 业务资料约定

sender与control_id只是资料身份。expected_groups用于记录人工构造的组数量，不是接收器的预先输出。未使用任何真实医疗信息。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，未知版本或内容变化应重新登记。标识、时间和测量数值均为合成资料，不代表真实研究对象或测量结果。

- senders：id、application、facility。
- messages：id、sender、control_id、report、continuation。
- expected_groups：id、message、order、observations。

normal与boundary是相互独立的资料包，不能直接拼接重复id。引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，不替代原始格式、引用归属、版本一致性或采集完成性判断。
