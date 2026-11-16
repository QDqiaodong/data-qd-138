# 盲复标采集管道（collection v1）

把初始二值栅格分发给互不知晓答案的复标员，收齐**两名不同合格人员的两份独立原始标注**
后交付，并以哨兵（gold）题驱动抽检债务与永久失格。整个管道只依赖 Python 标准库与
`sqlite3`，不计算多数票、不拼接空间窗口、不维护跨样本目标身份；既有单文件、ledger、
mosaic 接口与语义完全不变。

## 命令行

```sh
python -m raster_objects.collection_cli init   --db DB --source CAMPAIGN
python -m raster_objects.collection_cli claim  --db DB --worker W --now T
python -m raster_objects.collection_cli submit --db DB --worker W \
    --lease TOKEN --now T --source ANSWER
python -m raster_objects.collection_cli status --db DB
python -m raster_objects.collection_cli export --db DB --output JSON
```

* `init` 只接受**不存在**的库路径（符号链接也拒绝）；失败时清理半成品及
  `-wal/-shm` 后可重试。CAMPAIGN 严格为 `{lease_seconds,items,sentinels}`。
  `items` 每项严格含 `item_id,connectivity,grid`；`sentinels` 每项另含 `truth`。
  两组 `item_id` 跨组唯一并沿用 `ident` 约束；普通样本 1..2000 个、哨兵 1..100 个，
  `lease_seconds` 为 1..3600 的严格整数（拒绝布尔）。每个 `grid` 与 `truth` 都复用
  单栅格契约（0/1/null、矩形、边长 ≤500、单栅 ≤100000 格、connectivity 4/8），
  `truth` 与同项 `grid` 同形且至少一格非 null；全部输入矩阵（含每个 truth）合计
  不超过 100 万格。
* `claim --worker W --now T`：人员首次调用自动登记。工作响应**只有**
  `{token,deadline,connectivity,grid}`，绝不出现 item_id、普通/哨兵类别、truth、
  他人答案或完成情况。无可领项返回 `{"status":"no_work","reason":...}`，reason 为
  `gold_exhausted`（欠债但哨兵已耗尽）或 `none_available`（普通项名额满）。
* `submit --worker W --lease TOKEN --now T --source ANSWER`：ANSWER 严格为
  `{grid}`，须与所领样本同形并通过原栅格校验。
* 输入沿用仓库统一 JSON 边界：8MB 上限、重复键拒绝、非有限数值拒绝；`export` 经
  `common.save` 原子发布，且显式拒绝覆盖数据库及其 `-wal/-shm/-journal` 文件。
  输入/协议类错误退出 2，可重试忙错误退出 4；任何错误都保留原库与旧报告。

## Python API

```python
from raster_objects.collection import (
    Collection, CollectionError, CollectionBusy, UnknownLease,
    LeaseConflict, LeaseExpired, WorkerDisqualified, StaleClock,
    validate_campaign, validate_answer,
    init_collection, claim_work, submit_answer,
    collection_status, export_dataset,
)

collection = Collection.initialize('collect.db', campaign)
work = collection.claim('alice', 100)
receipt = collection.submit('alice', work['token'], 105,
                            {'grid': work['grid']})
print(collection.status())
dataset = collection.export()
```

`Collection(path, precommit_hook=..., observer=...)`：`precommit_hook(phase, worker,
token, receipt)` 在写事务提交前触发，抛异常则整笔回滚（故障注入入口）；`observer`
接收 `stmt` 等可观测事件，用于读取证据测试。

## 持久表示

| 表 | 内容与不变量 |
| --- | --- |
| `meta` | `lease_seconds`（固定租约时长）、`clock`（已成功调用的最大逻辑 T，初始 -1）、`token_seq`（全库单调、永不复用的令牌序号） |
| `items` | 不可变任务目录：`kind` 区分 `normal`/`sentinel`，存初始 `grid_json` 与哨兵的 `truth_json`（普通项 truth 为 NULL） |
| `workers` | `disqualified`（永久失格标志）、`normal_count`（通过上一哨兵以来的成功普通提交数）、`sentinel_due`（抽检债务，1=欠哨兵） |
| `leases` | 每次派单一行：`token` 主键、人员、item、kind、`issued_at`、`deadline`、`state` ∈ `open/submitted/expired` |
| `submissions` | 每次成功提交一行：答案 `grid_json` 与规范化 `fingerprint`、哨兵 `passed`、**`valid` 有效位**、首次 `receipt_json` |

关键索引与约束：

* 部分唯一索引 `idx_lease_one_open_per_worker ON leases(worker_id) WHERE
  state='open'` 是“每人至多一个未到期租约”的硬表示：重派前先把到期租约转为
  `expired`，新租约才能插入，并发下由数据库而不是应用代码保证不超额。
* 部分唯一索引 `idx_sub_normal_once ON submissions(worker_id,item_id) WHERE
  kind='normal'` 硬保证“每人对同一样本至多一份成功提交”。
* `idx_leases_item(item_id,state,deadline)` 与 `idx_sub_item_valid(item_id,kind,
  valid)` 让名额判定走索引标量查询，不触碰负载列。

## 有效票、占额、到期与失格如何关联（正确性论证）

* **有效票**不是导出时的临时过滤，而是 `submissions.valid` 这一持久列。普通样本
  “恰有两份”的判据恒为 `COUNT(valid=1) = 2`，派单器、`status`、`export` 读的是同一
  个事实，不可能出现“导出已过滤但派单仍认为收齐”的分叉。
* **占额**在派单时按 `有效票数 + 未到期普通租约数 < 2` 判定。未到期租约是
  `state='open' AND deadline > T`；因此一个名额在租约存活期间被占位，过期后自动
  重新可派，无需任何清理任务。同一查询还排除该人员已投过票的 item，配合
  `idx_sub_normal_once` 保证两名投票者必不同且每人至多一票。
* **到期**：提交仅在 `T < deadline`、人员与 token 相符且租约仍 `open` 时被首次接受；
  到期提交是无副作用的 `LeaseExpired`，不消耗 token、不推进时钟。旧 token 在重派后
  状态已离开 `open`，其迟交只能命中 `LeaseConflict`，无法污染新名额（新名额由全新
  `token_seq` 令牌标识）。
* **失格**：哨兵 truth 的 null 格不评分，其余格要求逐格完全相等（答案为 null 也算
  不等）。任一哨兵失败即在同一事务内：置 `workers.disqualified=1`、把该人员**全部**
  普通提交 `valid=0`、租约转 `submitted`、写失格回执、推进时钟。样本完成态由有效票
  计数即时回落为待补采，缺口对其他合格人员开放；原提交行保留作审计但永不充数。
  失格人员的 `claim` 与首次 `submit` 一律拒绝。
* **同事务原子性**：质量判断、人员状态、票有效性、样本完成态（派生自有效票）、租约
  占额与回执全部在一条 `BEGIN IMMEDIATE` 事务内提交；提交前注入异常会整体回滚，
  重启后重试得到的仍是全有或全无的状态。

## 抽检债务节奏

新人 `sentinel_due=1`，先通过一个哨兵；之后每成功提交**两份**普通标注即重新欠债
（`normal_count` 到达 2 时置 `sentinel_due=1`）。派哨兵时选择该人员**尚未成功作答**
的最小 item_id 哨兵（过期未提交不算作答）；通过哨兵后债务清零且普通计数归零。欠债
且没有可作答哨兵时返回 `no_work/gold_exhausted`，此时不能派发任何普通样本。

## 幂等、时钟与并发

* 令牌一旦成功消费，同人员、同 token、同**规范化**答案的重试永远返回首次回执
  （规范化 = 排序键、紧凑分隔的 JSON 指纹，键序/空白不影响身份）；即使租约后来过期、
  时钟前进或人员失格也不复活、不改写任何票。同 token 异答案是永久 `LeaseConflict`。
* 逻辑时钟 `meta.clock` 记录已成功 claim/submit 的最大 T（`no_work` 也算成功）。
  成功调用要求 `T >= clock` 并持久推进；非法、冲突、过期、忙错误均无副作用，不推进
  时钟、不消耗 token。T 限定在 `[0, 10^12]` 的严格整数。
* 写路径统一 `BEGIN IMMEDIATE`，跨进程/线程竞争等价于某个串行顺序，输家得到明确、
  无副作用的 `CollectionBusy`（CLI 有界退避重试，耗尽返回 4）。WAL 下读路径在单条
  `BEGIN` 快照事务内完成，`status`/`export` 的人员状态、有效票与完成态必属同一快照。

## status 与 export 契约

`status` 返回 `clock`、每个人员的 `{worker_id, qualified, disqualified,
normal_count, sentinel_due}`，以及每个普通样本的 `{item_id, state
(complete/pending), valid_workers, valid_votes, open_leases}`。

`export` 只含当前**恰有两份有效标注**的普通样本，按 item_id 排序；每项含初始
`connectivity` 与 `grid`，以及按 worker 排序的两份独立 `annotations`，每份带
`worker_id`、原始 `grid` 和对该答案运行 `analyze` 的完整结果（labels、objects、
三类格数等原契约字段）。两份答案永远并列保留，绝不合成新栅格；哨兵及其 truth 不进入
交付数据集。

## 读写与恢复代价

* **init**：一次性校验并写入不可变任务目录，O(输入总格数)；之后源文件删改与任务无关。
* **claim**：单条写事务，派单是索引上的存在性/计数子查询（O(log n) 级），只点查并
  解码所派那一个 item 的初始 grid；不读任何答案负载、不读 truth。
* **submit**：点查租约/人员/任务（O(log n)），普通提交只做计数更新；哨兵提交额外
  读取**本哨兵** truth 并逐格评分。失格失效是一条按 worker 定位的标量
  `UPDATE submissions SET valid=0`，不 SELECT、不解码任何人员（含本人历史）的答案。
* **status**：纯标量列读，单快照事务内零负载解码，语句数与答案矩阵大小无关。
* **export**：先用标量相关子查询筛出有效票恰为 2 的 item，再对每个交付 item 执行一条
  按 item_id 的点查取回其两份答案；未交付 item 与所有 truth 永不读取。`analyze` 仅对
  交付答案运行，代价随交付量而非库总量增长。
* **恢复**：全部状态在 SQLite 表内，WAL 持久化；进程重启后新句柄凭租约行、有效位与
  时钟即可恢复占额、债务与幂等语义，无内存态、无后台清理。

## 测试

`tests/test_collection_*.py` 全部离线可运行（含一个两进程文件屏障争抢最后名额的
用例），覆盖：契约与安全边界、派单/租约/占额/到期/重启、null 感知评分与债务周期、
令牌幂等与失格、A/B/C 联动与故障注入回滚、以及基于 SQL trace 的读取边界证据。
