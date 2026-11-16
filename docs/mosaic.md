# 活动多源瓦片融合仓（mosaic v1）

多个标注来源交付相互覆盖的栅格瓦片。融合仓在一个 SQLite 数据库里持久保存
**每一个来源的每一格贡献**（包括暂时被高优先级遮蔽的观测），支持来源撤下后
重新显露低优先级观测，并在同一读取快照内给出可追溯的空间窗口报告。仅使用
Python 标准库与 `sqlite3`；不保存更正版本、不分配跨查询对象身份，也不读取
更正台账（ledger）作为数据源。

## 命令行

```sh
python -m raster_objects.mosaic_cli init     --db DB
python -m raster_objects.mosaic_cli put      --db DB --source TILE
python -m raster_objects.mosaic_cli withdraw --db DB --tile-id ID
python -m raster_objects.mosaic_cli window   --db DB --row R --col C \
                                             --height H --width W \
                                             --connectivity K --output JSON
```

* `init` 只创建不存在的库（已存在路径或符号链接一律拒绝），失败时清理半成品
  及 `-wal/-shm` 后可重试。
* `put` 的 TILE 严格为 `{tile_id,row,col,priority,grid}`，不接受任何额外字段。
  `tile_id` 复用现有 `ident` 约束（非空白、≤128 字符、无控制字符）；`row/col`
  为瓦片全局左上角，`priority` 为整数，三者范围均为 `[-10^9, 10^9]`，布尔拒绝。
* `grid` 复用现有 0/1/null、矩形、边长 ≤500、总格 ≤100000 的限制，且**没有
  connectivity 字段**；瓦片覆盖到的每个全局格坐标同样必须落在
  `[-10^9, 10^9]` 内。
* 输入沿用仓库统一 JSON 边界：严格 8MB、重复键拒绝、非有限数值拒绝；窗口输出
  通过 `common.save` 原子发布，且显式拒绝覆盖数据库及其 `-wal/-shm/-journal`
  辅助文件。非法请求退出 2；并发竞争的无副作用忙错误退出 4（CLI 有界退避重试，
  耗尽才返回 4）。

## Python API

```python
from raster_objects.mosaic import (
    Mosaic, MosaicError, MosaicBusy, TileConflict, UnknownTile,
    init_mosaic, put_tile, withdraw_tile, window_query,
)

mosaic = Mosaic.initialize('mosaic.db')
mosaic.put({'tile_id': 'a', 'row': -4, 'col': -4, 'priority': 1,
            'grid': [[1, 0], [None, 1]]})
report = mosaic.window(-4, -4, 2, 2, 4)
mosaic.withdraw('a')
```

`Mosaic(path, precommit_hook=...)` 在提交前注入回调；回调抛异常时整笔事务回滚，
目录、空间索引、逐格贡献与 `catalog_seq` 一起回到提交前状态。

## 持久表示、索引与一致性边界

数据模型是**逐格贡献表 + 显式空间桶索引**，不是整栅格赢家版本，因此可以在
20 亿级坐标空间里稀疏存放，绝不按全局包围框分配栅格，也不只持久化赢家。

| 表 | 内容 |
| --- | --- |
| `meta` | 单行 `catalog_seq`（从 0 开始） |
| `sources` | 每个来源一行：不可变规范化 `grid_json`、行列原点、宽高、优先级、内容指纹 `fingerprint`、活动标志 `active` |
| `tile_buckets` | 来源覆盖的空间桶 `(tile_id, bucket_row, bucket_col)`，桶跨 500 全局行列 |
| `cells` | 逐格贡献 `(tile_id, grow, gc, value)`，`value` 可空，主键即覆盖索引 |

* `sources` 与 `cells` 使用 `WITHOUT ROWID` 聚簇表，主键就是覆盖索引。
* `tile_buckets` 上建有 `(bucket_row, bucket_col, tile_id)` B 树索引
  （`idx_bucket_space`）：窗口查询先按桶的矩形范围在该 B 树中定位候选来源，
  再 JOIN 活动目录，从不全目录扫描、不解码无关瓦片，也从不读取 `grid_json`
  负载列。瓦片边长 ≤500、桶跨 500，单瓦片至多落在 4 个桶内。
* 数据库以 WAL 模式工作。写操作在单条 `BEGIN IMMEDIATE` 事务内同时提交目录行、
  桶索引行、逐格贡献和 `catalog_seq`；任一步异常或竞争失败统一回滚，不可能留下
  半份来源、漏格或被消耗的序号。读操作在单条 `BEGIN` 事务内完成全部语句，WAL
  保证整份报告对应一个不可变快照：目录活动状态、空间贡献、序号与几何要么全是
  撤下前状态，要么全是撤下后状态。

## 来源目录与 catalog_seq 语义

* 首次 `put` 成功后来源为活动状态；同 ID 相同规范化内容的再次 `put` 是无副作用
  重复——即使该来源已经撤下也**不会复活**，不消耗序号。
* 同 ID 异内容（原点、优先级、尺寸或任一栅格值不同）以 `TileConflict` 拒绝，
  库不变。
* `withdraw` 把已知活动来源永久置为非活动；重复撤下无副作用；未知 ID 报
  `UnknownTile`。撤下不删除任何 `cells` 行，低优先级观测因此始终可恢复。
* `catalog_seq` 从 0 开始，仅在**首次登记**或**首次成功撤下**时加 1；幂等重复、
  冲突拒绝和竞争失败都不消耗序号。
* 每库最多 20000 个来源、累计 5000000 个输入格（含已撤下来源，按
  `SUM(height*width)` 统计）；超限整次 `put` 失败。
* 规范化后的瓦片内容存于库内，`put` 成功后删除或修改原始文件不影响仓内数据。

## 融合语义

逐格独立融合，窗口内每格按贡献集合判定：

1. 没有任何活动来源覆盖 → `uncovered`；
2. 仅被 `null` 覆盖 → `unknown`。`null` 表示“无意见”，记录覆盖但**不能压住**
   任意优先级的已知值；
3. 在所有活动的 0/1 贡献中取最大 `priority`：
   * 最高级只有一种值 → `resolved`，值为 0 或 1；
   * 最高级同时存在 0 和 1 → `conflict`，不按 tile_id 或到达顺序私自裁决。

每格 `provenance` 为 `null`（非 resolved/conflict 时）或
`{priority, top_count, zero_witness, one_witness}`：

* `top_count` 只统计最高优先级的已知（0/1）贡献数；
* 两个见证字段分别是最高级中给出对应值的**最小 tile_id**（Python 字符串序），
  没有该值则为 `null`。

报告含窗口 `row/col/height/width`、`connectivity`、`catalog_seq`、等尺寸的
`values` 与 `states` 两个矩形网格、`provenance` 网格和四种状态计数；
`uncovered/unknown/conflict` 三态的 `values` 恒为 `null`。

只要窗口内存在任一 `conflict`，`result` 即为 `null`。否则 `result` 复用现有
`analyze` 的标签、对象与三类格数语义：`uncovered/unknown` 作为 `null` 进入；
标签与对象 ID 仅在本窗口内按行优先重新编号（不跨查询分配身份，且不受台账每版
128 目标上限约束）；bbox 与精确分数字符串质心换算为**全局坐标**；周长仍按窗口
裁切边界计算暴露正交边（与单文件工具同一套规则）。

## 资源代价

设窗口桶范围命中的候选来源数为 C、某来源落在窗口内的格数为 gᵢ、窗口格数为 W。

* **put**：一次立即写事务，写入 1 行 `sources`、≤4 行/维的桶记录与 h×w 行
  `cells`，代价 O(h×w) 次索引插入与一次 fsync 提交；上限检查为 O(1) 聚合。
* **withdraw**：一次立即写事务，主键点查 + 1 行活动标志更新 + 序号更新，
  O(log n)。
* **window**：一次只读快照事务；候选定位是桶 B 树的区间探测
  O(log n + C)，随后对每个候选单独流式执行一条带坐标区间的主键查询，总读取
  O(Σ gᵢ)，只处理与窗口相交的来源，且逐候选折叠、不同时装入所有候选的格数据；
  Python 侧工作集为 O(W) 的融合累加器（10000 稀疏瓦片 + 25 重叠来源的离线用例
  实测峰值约 32 KiB，见 `tests/test_mosaic_scale.py`）。
