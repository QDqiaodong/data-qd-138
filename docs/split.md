# 防泄漏训练/评测划分仓（split v1）

把独立栅格样本持久化入仓，按**等价家族**划定不可跨越的 train/eval 边界，并以
不可逆的样本用途承诺记录实际交付。整个仓库只依赖 Python 标准库与 `sqlite3`；
不读取更正台账、瓦片仓或复标库，既有单文件、ledger、mosaic、collection 入口与
语义完全不变。

## 为什么需要家族

同一采集实体（`specimen_id` 相同）的图像，以及同一幅 `grid` 经过矩形任意
90 度旋转或镜像后的副本，一旦分别进入训练集与评测集，离线指标就会被泄漏污染。
本仓维护两类**可解释边**及其传递闭包：

* `specimen`：两个样本 `specimen_id` 相同；
* `duplicate`：两幅 `grid` 在二面体群 D4 的 8 种变换（0/90/180/270 度旋转，
  各可叠加一次镜像）下存在完全相同的视图。

比较包含变换后的尺寸（2×3 旋转为 3×2 后仍同族，但 2×2 与 1×4 永远不同族）；
`null` 与 `0` 是不同记号，不会被判为相同；`connectivity` 与 `stratum` 不影响
副本关系。家族不是栅格内的前景连通域：空目标、全 `null`、跨类别副本都合法。
家族 ID 恒为当前成员中最小的 `sample_id`；一个新样本可同时用实体边和副本边
桥接多个旧家族。

## 命令行

```sh
python -m raster_objects.split_cli init   --db DB
python -m raster_objects.split_cli ingest --db DB --source BATCH
python -m raster_objects.split_cli plan   --db DB --source REQUEST
python -m raster_objects.split_cli commit --db DB --plan-id ID
python -m raster_objects.split_cli export --db DB --plan-id ID --output JSON
python -m raster_objects.split_cli status --db DB
```

* `init` 只接受**不存在**的库路径（符号链接也拒绝）；失败清理半成品及
  `-wal/-shm` 后可重试。
* BATCH 严格为 `{samples}`，非空数组；每项严格含
  `sample_id,specimen_id,stratum,connectivity,grid`。三个标识符沿用 `ident`
  约束；`grid` 复用 `analyze` 的完整单栅格校验（0/1/null、矩形、边长 ≤500、
  单栅 ≤100000 格、connectivity 4/8、拒绝布尔）。批内 ID 不重复。全库上限
  2000 样本、4 种 stratum、累计 100 万格；任一超限或任一项非法，**整批失败**，
  不留下同批其他样本。
* `sample_id` 的全部字段不可变：同 ID 同规范化内容重复入仓无副作用
  （`repeated` 计数、不分配新批次号）；同 ID 异内容拒绝。入仓后源文件删除或
  修改不影响数据，栅格以规范化 JSON 持久在库内。
* 输入沿用仓库统一 JSON 边界：8MB 上限、重复键拒绝、非有限数值拒绝；`export`
  经 `common.save` 原子发布，并显式拒绝覆盖数据库及其 `-wal/-shm/-journal`
  文件。协议/输入类错误退出 2，可重试忙错误退出 4；
  `contaminated`/`infeasible`/`stale_plan` 是**明确业务结果**，退出 0 且保留
  已有库与报告。

## Python API

```python
from raster_objects.split_store import (
    SplitStore, SplitError, SplitBusy, UnknownPlan,
    validate_batch, canonical_form, solve_quotas, request_fingerprint,
    init_store, ingest_batch, plan_request, commit_plan,
    export_plan, store_status,
)

store = SplitStore.initialize('split.db')
store.ingest({'samples': [...]})
proposal = store.plan({'plan_id': 'p1', 'sample_ids': [...], 'quotas': {...}})
if proposal['result'] == 'planned':
    outcome = store.commit('p1')
dataset = store.export('p1')
print(store.status())
```

`SplitStore(path, precommit_hook=..., observer=...)`：`precommit_hook(phase,
payload)` 在写事务提交前触发（`phase` 为 `ingest`/`plan`/`commit`），抛异常
则整笔回滚，是明确的故障注入入口；`observer` 接收 `stmt`（SQL 文本）与
`payload_decode`（仅 export 解码栅格时发出）等事件，用于读取证据测试。

## 持久表示与归族索引

* `samples`：不可变样本行，保存标识符、连通性、尺寸、格数、规范化栅格
  `grid_json`、内容指纹 `content_fp`，以及入仓时一次性算出的
  `canon_key/canon_transform`。
* `canon_key` 是 8 个 D4 视图中字典序最小的严格 JSON 文本（含变换后尺寸），
  人工可取回复核；两栅格 `canon_key` 相同当且仅当它们构成副本关系。
* `edges`：解释性无向边（`specimen`/`duplicate`）。每个新样本至多各连一条
  实体边与一条副本边到等价类最早成员，构成张成森林；完整连通关系由
  `family_member` 关系索引承载。
* `family_member(sample_id, family_id)`：并查集在入仓事务内重算受影响连通
  分量并整体物化，家族 ID 取分量内最小样本 ID。
* `plans`（`proposed`/`committed`）、`deliveries`、`sample_use`、`receipts`：
  方案、不可逆用途承诺与成功回执。

归族检索、规划与状态只读取标量列与关系索引，**永不重新解码全库 grid**；
只有 `export` 按已提交方案读取所需样本的 `grid_json`，并发出
`payload_decode` 可观测事件。

## 规划与精确求解

REQUEST 严格为 `{plan_id,sample_ids,quotas}`：

* `plan_id` 沿用 `ident`；`sample_ids` 为非空、无重复的已知 ID 数组；
* `quotas` 的键必须**恰好**等于全库当前全部 stratum，每项严格含非负整数
  `train,eval`（拒绝布尔）。

处理顺序与规则：

1. 把请求样本扩展为其**完整当前家族**；扩展后超过 20 个家族是输入错误。
2. 同一家族全部成员只能同属 `train`、`eval` 或 `unused`。
3. 已有交付用途的家族被强制取该用途并全部纳入，旧承诺不能变回 `unused`。
4. 扩展集合含污染家族时返回 `contaminated`，附一条连接异用途样本的真实边
   路径，逐边标明 `specimen`/`duplicate`；不写方案行、不占用 `plan_id`。
5. 否则要求每个 stratum 的 train/eval 样本数分别**精确**等于配额，余下可
   `unused`；无可行方案返回 `infeasible`，不近似、不拆族、不放松旧承诺。
6. 多解时把扩展样本按 `sample_id` 排序，取用途向量字典序最小者
   （`train < eval < unused`）。

求解器是带记忆化与后缀容量剪枝的深度优先搜索：按家族（最小成员 ID 序）
递归，候选用途按 train/eval/unused 顺序尝试，`(index, 剩余配额)` 状态记忆化，
任一 stratum 的剩余需求超过后缀可用成员即在根部剪枝。它精确支持 20 家族、
4 类别的边界，搜索节点数远小于 `3**20`，方案回执中的 `search` 块给出
`nodes/memo_hits/pruned/full_enumeration` 证据；测试用独立小规模穷举 oracle
（`tests/split_oracle.py`）逐项比对，并含“逐家族贪心会走错、回溯才有解”的
构造用例。

`plan` 成功只保存提案（含完整成员、用途、配额、强制依据与搜索证据），不建立
任何用途承诺。同 `plan_id` 同规范化请求（`sample_ids` 顺序、JSON 键序不影响
请求身份）重试恒返第一次方案；异请求同 ID 拒绝；`infeasible`/`contaminated`/
输入错误均不占用 `plan_id`。

## 提交重检与污染处理

`commit` 不照抄规划时的分组，而在当前状态重新核对原方案的**全部扩展家族**：

* 后来的桥接使某家族成员集合变化 → `stale_plan`；
* 家族已被另一方案占用相反用途，或家族已污染 → `stale_plan`，附原因与路径；
* 无关样本入仓不影响方案；同家族新增的相同用途承诺不会误拒绝。

`stale_plan` 零新增承诺；无需重新求解或替换原方案，提案保留为 `proposed`。
通过重检后，方案中的 train/eval 样本在同一 `BEGIN IMMEDIATE` 事务内写入
`deliveries`/`sample_use` 并生成成功回执；`unused` 不产生承诺。同方案重复
commit 返回原回执（`idempotent: true`），不重复记账；未知方案拒绝，未
commit 的方案不能 export。

污染样本**仍然入仓成功**：泄漏事实不会通过拒收桥接样本被掩盖。`status` 按
家族列出成员、各 stratum 计数、已交付 train/eval 用途；污染家族带
`contaminated: true` 与真实边路径。后续桥接发现污染时，历史交付内容不可重写、
仍可经 `export` 审计，状态报告只如实告警，绝不声称撤回了外部已用数据。

## 并发、恢复与快照

* 所有写操作在 WAL 库上以 `BEGIN IMMEDIATE` 单事务串行化；锁竞争立即返回
  明确无副作用的忙错误（CLI 有界重试，API 直接抛出 `SplitBusy`，退出码 4）。
* 归族索引、样本负载、方案、承诺、回执全部在同事务提交；`precommit_hook`
  注入异常时样本、归族、回执、承诺一起回滚，重启重试不会产生幽灵泄漏或半份
  交付。
* `status` 与每次规划都在单一读取快照（`BEGIN`…`COMMIT`）内完成家族、计数、
  承诺与污染路径的读取，视图内部自洽。
* 跨进程屏障测试验证：同族相反用途的两个方案竞争只有一个成功承诺；桥接
  ingest 与 commit 交错时，结果等价于某个串行次序。

## 测试

```sh
PYTHONPATH=src:tests python3 -m unittest tests.test_split_ingest \
    tests.test_split_families tests.test_split_solver tests.test_split_linkage -v
```

* `test_split_ingest`：严格契约、边界与整批原子性、不可变与源文件脱离、
  8MB/原子输出/禁覆盖库文件、CLI 退出码。
* `test_split_families`：非正方形旋转、镜像、尺寸差异、null/0 区分、连通性
  无关性、实体边+副本边传递家族、边标签持久化与重启逐边核验。
* `test_split_solver`：独立穷举 oracle 逐项比对、精确配额、强制纳入、真无解、
  字典序多解、贪心失败构造、20 家族/4 类别可行与无解及非全枚举证据、
  plan 生命周期。
* `test_split_linkage`：A/B/C 污染联动与两种边路径、stale_plan 重检（相关/
  无关入仓对照）、两进程屏障竞争、ingest/commit 故障注入回滚与重启恢复、
  单快照与“无关矩阵未读取”的可观测证据。
