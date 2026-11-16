# 栅格更正台账与对象身份（ledger v1）

单文件命令 `raster-objects` 只能对一份栅格重新编号：标注员更正像素后，扫描顺序编号会变，
无法可靠追踪同一目标。台账在保留原命令与结果语义的前提下，把每次更正、逐格触及历史和
持久对象身份写入一个 SQLite 数据库，仅依赖 Python 标准库与 sqlite3。

## 命令行

```sh
python -m raster_objects.ledger_cli init   --db DB --source JSON
python -m raster_objects.ledger_cli apply  --db DB --source JSON
python -m raster_objects.ledger_cli export --db DB --version N --output JSON
```

* `init` 只接受不存在的库路径；按现有 `connectivity`/`grid` 契约校验源文件，建立
  `version=0`，其几何结果与 `raster_objects.model.analyze` 完全一致，对象 id 从 1 起按
  行优先扫描顺序分配。初始化失败会删除半成品文件，可直接重试。
* `apply` 的输入严格为 `{"request_id","base_version","changes"}`。`changes` 非空，每项
  严格含 `row,col,before,after`，坐标不重复；`before`/`after` 只能是 0、1 或 null 且必须
  不同；布尔值一律拒绝；`base_version` 必须是已提交的非负整数（不接受布尔）。
* `export` 把指定版本的权威报告原子写入 `--output`（沿用 `common.save` 的安全发布边界），
  同时在 stdout 打印同一份报告。

退出码：0 成功；2 输入/协议错误；3 陈旧基线冲突（stderr 为按行列排序的冲突坐标）；
4 可重试的忙错误（无任何副作用）。`apply` 对短暂写竞争做有界退避重试，耗尽后返回 4。

## Python API

```python
from raster_objects.ledger import (
    Ledger, LedgerError, UnknownVersion, RequestConflict,
    RequestIdConflict, LedgerBusy, normalize_request,
    init_ledger, apply_request, export_version,
)
from raster_objects.matching import match_objects

ledger = Ledger.initialize('work.db', 'input.json')
receipt = ledger.apply({
    'request_id': 'fix-001',
    'base_version': 0,
    'changes': [{'row': 2, 'col': 0, 'before': 1, 'after': 0}],
})
report = ledger.export(receipt['version'])
```

异常分层：`LedgerError` 为基类；未知版本 `UnknownVersion`、同 id 异请求 `RequestIdConflict`、
可重试忙 `LedgerBusy` 均为输入/协议侧错误；`RequestConflict` 携带 `.conflicts` 坐标列表。
`Ledger(path, precommit_hook=...)` 支持在事务提交前注入回调，回调抛异常则整笔回滚。

## 陈旧基线与逐格触及历史

每个版本的栅格尺寸、连通性在 init 后固定；每版仍受原栅格规则约束，目标数上限 128，
超限整次失败，不产生版本、不推进 id 分配器。

一笔更正被接受当且仅当：

1. 每个 `before` 等于**请求基线版本**该格的值（未知版本、越界、坏类型、before 不符都是
   输入错误，优先于冲突报告）；
2. 每个坐标自 `base_version` 之后没有被任何成功更正触及。`touches` 表逐格记录每次成功
   提交，即使单元格后来被改回原值，陈旧写入仍判冲突；
3. 应用后目标数不超过 128。

冲突时返回**全部**冲突坐标（按行、列排序），不产生版本、不改变 id 分配器。全部坐标通过
后才把 `after` 应用到当前 head，因此成功版本号始终连续递增。

## 持久对象身份

身份匹配只在**当前已提交 head** 与候选栅格之间进行，从不使用请求基线或扫描编号；陈旧
请求被接受时，按重定位后的 head 重新确定全部对象的匹配、退休 id 与几何，而不是只重算
changes 触及的对象。未被更改的对象同样参与匹配，可同时处理一次修订中的拆分与合并、
目标全消失以及 null 变前景。

候选边权为两对象共同前景格数，只有正权边可参与匹配。一对一匹配依次优化：

1. 总重叠格数最大；
2. 匹配对数最大；
3. 同分情况下，按新对象最小坐标（行优先、再列）的顺序，取旧 id 向量字典序最小者；
   未匹配位置视为大于所有旧 id 的无穷值。

匹配对象继承旧 id；未匹配的新对象按上述新对象顺序领取全库递增 id；消失的旧 id 进入
`retired_ids` 且永不复用。`version=0` 从 1 按扫描顺序分配。匹配算法在
`raster_objects.matching` 中以单个数权值的分配问题实现，三级目标编码为一个任意精度
整数，由匈牙利算法求解；`tests/test_identity.py` 用独立的穷举小匹配 oracle 校验。

## 回执、幂等与并发

成功回执包含 `version`、`parent_version`、`request_id`、对象继承配对（含每对重叠格数）、
`retired_ids`、`birth_ids`。请求身份由规范化指纹决定：键顺序与 changes 顺序不影响身份。

* 同一 `request_id` 重试同一规范化请求：即使基线已陈旧，也直接返回第一次成功的完整回执，
  不再计算、分配 id 或提交。
* 同一 `request_id` 携带不同请求：`RequestIdConflict`，库不变。
* 失败请求不占用 `request_id`。

所有写入在单条 `BEGIN IMMEDIATE` 事务内提交：栅格版本、逐格触及历史、对象匹配结果、
id 分配器（`meta.next_id`）与请求回执同生共死；竞争失败或任一步异常都不会暴露半份版本，
也不会出现“新栅格配旧匹配”的快照。两个客户端并发 apply 等价于某个串行提交顺序，竞争
失败以无副作用的 `LedgerBusy` 明确返回，可安全重试。

## 导出报告

导出报告以数据库为唯一权威来源，包含 `schema_version`、`version`、`parent_version`、
`connectivity`、源文件 `source_sha256`、该版本成功回执（version 0 为 null）与 `result`。
`result` 保留原契约的标签栅格、对象几何（bbox、cells、周长、精确分数字符串）与三类格数；
标签和 `objects.id` 一律使用持久 id。导出任意旧版本不会混入当前几何或后来分配的 id。

## 存储结构

| 表 | 职责 |
| --- | --- |
| `meta` | 固定的连通性、高宽、全库 id 分配器 `next_id`、源 SHA256 |
| `versions` | 每版本可重建栅格 `grid_json`、持久 id 几何 `result_json`、回执 |
| `touches` | 逐格触及历史 `(version,row,col,request_id)`，支撑陈旧冲突判定 |
| `requests` | 请求 id → 规范化指纹与首次成功回执，支撑幂等重试与异请求冲突 |
