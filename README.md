# data-qd-138 · 栅格目标连通域提取

Bounded binary raster component labeling and object geometry。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/raster-objects --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/raster_objects/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 持久更正台账与对象身份

标注更正需要跨修订追踪同一目标时，使用仅依赖标准库与 SQLite 的台账（设计与语义见
[台账契约](docs/ledger.md)），原单文件命令与全部结果语义保持不变：

```sh
PYTHONPATH=src python -m raster_objects.ledger_cli init   --db run/work.db --source fixtures/input.json
PYTHONPATH=src python -m raster_objects.ledger_cli apply  --db run/work.db --source run/request.json
PYTHONPATH=src python -m raster_objects.ledger_cli export --db run/work.db --version 1 --output run/v1.json
```

- `init` 只接受不存在的库，按现有 connectivity/grid 契约建立 version=0，对象 id 从 1 按
  扫描顺序分配；尺寸、连通性初始化后固定，每版目标数上限 128。
- `apply` 严格接受 `{request_id,base_version,changes}`，changes 每项严格含
  `row,col,before,after`（0/1/null 且不同、坐标不重复）；before 对照基线版本校验，逐格
  触及历史拦截陈旧冲突（即使改回原值也冲突），冲突返回按行列排序的全部坐标且无副作用。
- 身份匹配在当前 head 与候选栅格间进行：正权边（共同前景格数）一对一匹配，先最大化总
  重叠格数、再最大化匹配对数、同分取旧 id 向量字典序最小；新对象领取全库递增 id，退休
  id 永不复用，拆分/合并、全消失、null 变前景均覆盖。
- 栅格、触及历史、匹配、id 分配器、请求回执在单事务提交；并发 apply 等价于某一串行顺序，
  竞争返回可重试的忙错误；同 request_id 的规范化重试返回首次回执，异请求冲突被拒绝。
- `export` 输出保留原 result 的标签、几何、三类格数与精确分数字符串，标签与 objects.id
  使用持久 id，并带 version、parent_version 与该版本回执（version 0 为 null）；数据库是
  权威数据，旧版本导出不混入当前几何或后来的 id。

可测试的 Python API 位于 `raster_objects.ledger`（`Ledger.initialize/apply/export` 及
模块级 `init_ledger/apply_request/export_version`）与 `raster_objects.matching`
（`match_objects`，带独立穷举 oracle 测试）。

## 活动多源瓦片融合仓

多个来源交付相互覆盖的瓦片时，使用仅依赖标准库与 SQLite 的融合仓（设计、持久表示、
索引与资源代价见 [融合仓契约](docs/mosaic.md)）。它持久保存每个来源的逐格贡献，
撤下高优先级来源后可准确恢复被遮蔽的低优先级观测；不保存更正版本、不分配跨查询
对象身份，也不读取更正台账。

```sh
PYTHONPATH=src python -m raster_objects.mosaic_cli init     --db run/mosaic.db
PYTHONPATH=src python -m raster_objects.mosaic_cli put      --db run/mosaic.db --source run/tile.json
PYTHONPATH=src python -m raster_objects.mosaic_cli withdraw --db run/mosaic.db --tile-id a
PYTHONPATH=src python -m raster_objects.mosaic_cli window   --db run/mosaic.db \
  --row -4 --col -4 --height 2 --width 2 --connectivity 4 --output run/window.json
```

- `init` 只创建不存在的库；`put` 严格接受 `{tile_id,row,col,priority,grid}`，
  tile_id 复用现有 ident 约束，row/col/priority 与全部全局格坐标均在
  `[-10^9,10^9]` 内且拒绝布尔；grid 复用 0/1/null、矩形、边长 500、总格 100000
  限制但无 connectivity 字段。8MB/重复键/原子输出等安全边界沿用仓库约定，窗口
  输出不得覆盖数据库或其 `-wal/-shm` 辅助文件；非法请求退出 2。
- 来源目录持久保存不可变规范化瓦片与活动状态：相同内容重复 put 无副作用（撤下后
  也不复活），异内容同 ID 拒绝；撤下永久生效、重复撤下无副作用、未知 ID 报错。
  `catalog_seq` 从 0 起，仅首次登记或首次撤下加 1，与目录、空间索引、逐格贡献在
  单事务共同提交。每库上限 20000 来源、累计 500 万输入格，超限整次 put 失败。
- 融合逐格区分 `uncovered/unknown/resolved/conflict`：null 是无意见，不压制任何
  已知值；最高优先级同时有 0 和 1 即 conflict，不按 ID 或到达顺序裁决。每格给出
  最高级贡献计数与按字符串序取最小 ID 的双向见证；窗口有 conflict 时 `result` 为
  null，否则复用 analyze 的标签/对象/三类格数语义，标签仅窗口内编号，bbox 与
  分数质心换全局坐标，周长按窗口裁切计算，且不受 128 目标上限约束。
- 空间检索通过 500 跨桶 B 树索引定位候选，逐候选流式读取窗口内贡献，不扫描全目录、
  不读取无关瓦片的 grid 负载；WAL 单快照事务保证窗口报告与目录状态、序号完全一致，
  两进程交错读写只会得到撤下前或撤下后的完整状态。

可测试的 Python API 位于 `raster_objects.mosaic`（`Mosaic.initialize/put/withdraw/
window` 及模块级 `init_mosaic/put_tile/withdraw_tile/window_query`）；测试中的
`tests/mosaic_oracle.py` 是与生产存储和几何实现无关的逐格 oracle。

## 盲复标采集管道

需要把初始栅格分发给互不知晓答案的复标员、每人收齐两份独立原始标注，并在抽检发现
不合格人员时自动补采时，使用仅依赖标准库与 SQLite 的持久化采集管道（设计、持久表示、
失格事务与读写代价见 [采集管道契约](docs/collection.md)）。它交付带人员来源的双份
原始标注，不计算多数票、不拼接空间窗口、不维护目标身份，哨兵真值不进入交付数据集。

```sh
PYTHONPATH=src python -m raster_objects.collection_cli init   --db run/collect.db --source run/campaign.json
PYTHONPATH=src python -m raster_objects.collection_cli claim  --db run/collect.db --worker alice --now 100
PYTHONPATH=src python -m raster_objects.collection_cli submit --db run/collect.db --worker alice \
    --lease TOKEN --now 105 --source run/answer.json
PYTHONPATH=src python -m raster_objects.collection_cli status --db run/collect.db
PYTHONPATH=src python -m raster_objects.collection_cli export --db run/collect.db --output run/dataset.json
```

- `init` 只创建不存在的库；CAMPAIGN 严格为 `{lease_seconds,items,sentinels}`，普通
  样本 1..2000、哨兵 1..100、租约 1..3600 秒，标识符沿用 ident 且两组 item_id 跨组
  唯一；所有 grid/truth 复用单栅格 0/1/null、尺寸与连通性校验，truth 同形且至少一格
  非 null，输入矩阵合计不超过 100 万格；入库后源文件删改不影响任务。
- 人员首次 claim 自动登记；每人同时至多一个未到期租约，重复 claim 原租约原封返回、
  不续期。新人先过一个哨兵，此后每成功提交两份普通标注再欠一次哨兵；欠债且哨兵耗尽
  返回 `no_work/gold_exhausted`，不能绕过债务领取普通样本。同类按 item_id 字符串序
  派首个可领项；普通样本恰需两名不同合格人员，有效票加未到期租约不得超过 2，过期名额
  自动重派且旧 TOKEN 无法污染新名额。工作响应只有 `{token,deadline,connectivity,grid}`。
- 哨兵 null 真值格不评分，其余格逐格完全相等（答案 null 也算不等）；任一失败即永久
  失格，该人员既有普通票在同一事务内立即失效、已齐样本回落待补采并向他人开放，原标注
  留作审计。同 TOKEN 规范化重试恒返首次回执（过期、时钟前进、失格后也不复活票），
  异答案拒绝；逻辑时钟随成功调用单调推进，非法/冲突/过期/忙错误零副作用。
- `status` 在单读取快照内给出每个普通样本的有效人员、完成/待补采态及每人合格状态、
  普通计数与抽检债务；`export` 只含恰有两份有效标注的样本（按 item_id 排序），每项
  保留初始样本与按 worker 排序的两份原始答案及各自完整 `analyze` 结果，绝不合成栅格。
  协议错退出 2、忙错误退出 4，输出不得覆盖数据库或其 SQLite 辅助文件。

可测试的 Python API 位于 `raster_objects.collection`（`Collection.initialize/claim/
submit/status/export`、`validate_campaign/validate_answer` 及模块级
`init_collection/claim_work/submit_answer/collection_status/export_dataset`），
支持 `precommit_hook` 故障注入与 `observer` SQL 可观测读取证据。

## 防泄漏训练/评测划分仓

同一采集实体的图像以及旋转/镜像副本若被分入训练集与评测集，离线成绩会失真。
需要带家族归族、不可逆用途承诺与提交重检的防泄漏划分时，使用仅依赖标准库与
SQLite 的划分仓（设计、持久表示、索引与恢复代价见
[划分仓契约](docs/split.md)）。它只接受独立样本清单，不读取更正台账、瓦片仓
或复标库；既有入口全部保持兼容。

```sh
PYTHONPATH=src python -m raster_objects.split_cli init   --db run/split.db
PYTHONPATH=src python -m raster_objects.split_cli ingest --db run/split.db --source run/batch.json
PYTHONPATH=src python -m raster_objects.split_cli plan   --db run/split.db --source run/request.json
PYTHONPATH=src python -m raster_objects.split_cli commit --db run/split.db --plan-id p1
PYTHONPATH=src python -m raster_objects.split_cli export --db run/split.db --plan-id p1 --output run/dataset.json
PYTHONPATH=src python -m raster_objects.split_cli status --db run/split.db
```

- `init` 只创建不存在的库；BATCH 严格为 `{samples}`，每项严格含
  `sample_id,specimen_id,stratum,connectivity,grid`，标识符沿用 ident，栅格复用
  `analyze` 完整校验。每批非空、批内 ID 不重复；全库最多 2000 样本、4 种
  stratum、累计 100 万格，超限或一项非法则整批失败。同 ID 规范化同内容重复
  无副作用，异内容拒绝；入仓后源文件删改不影响数据。
- 归族维护两类可解释边及传递闭包：相同 `specimen_id` 的 `specimen` 边，与整幅
  grid 经任意 90 度旋转/镜像后相同的 `duplicate` 边；比较含变换后尺寸，null 与
  0 不同，connectivity/stratum 不影响副本关系。家族 ID 为最小成员 sample_id，
  新样本可同时桥接多个旧家族。入仓时生成可复核 D4 规范键，后续归族与规划只走
  持久标量/关系索引，不重新解码全库 grid，只有 export 读取所需样本负载。
- `plan` 的 REQUEST 严格为 `{plan_id,sample_ids,quotas}`，配额键恰好覆盖当前
  全部 stratum 且为非负整数（拒绝布尔）；请求先扩展为完整当前家族（≤20），
  整族只能同属 train/eval/unused，已交付家族强制沿用旧用途。污染返回
  `contaminated` 并附逐边标明 specimen/duplicate 的真实路径；精确配额无解返回
  `infeasible`；多解取样本序下用途向量字典序最小（train<eval<unused）。记忆化
  +后缀剪枝的精确搜索不穷举 `3^20`，方案带 `search` 证据；plan 只保存提案不
  承诺，同规范化重试返回首方案，失败不占 plan_id。
- `commit` 在当前状态重检原方案全部扩展家族的成员集合与用途兼容性：后桥接
  改成员、家族污染或相反用途先占均返回 `stale_plan` 且零新增承诺，无关样本不
  使方案失效。通过后 train/eval 承诺与回执单事务落库，重复 commit 返回原回执；
  未 commit 不能 export。污染样本仍入仓成功，历史交付不可重写、可审计。
- 协议错误退出 2、忙错误退出 4；`contaminated/infeasible/stale_plan` 为退出 0
  的明确业务结果。8MB/严格 JSON/原子输出边界沿用仓库约定，输出不得覆盖库及
  `-wal/-shm/-journal` 文件；`precommit_hook` 提供提交前故障注入，`observer`
  提供读取证据。

可测试的 Python API 位于 `raster_objects.split_store`
（`SplitStore.initialize/ingest/plan/commit/export/status`、
`validate_batch/canonical_form/solve_quotas` 及模块级
`init_store/ingest_batch/plan_request/commit_plan/export_plan/store_status`）；
`tests/split_oracle.py` 是与生产求解独立的穷举 oracle。


## 当前业务与样例
# Binary raster objects v1

为影像标注整理人员提取二值目标，输入connectivity=4或8与非空矩形grid，宽高<=500，总格<=100000。值为0背景、1目标或null未知，严格整数不接受布尔。所有坐标为0起点行列，不声称真实地理坐标。
行优先扫描赋连通域编号，4/8邻接决定连通性。每目标输出格数、整数边界框、精确分数质心和正交边长计数周长；即使8邻接也始终按4条暴露边计算周长。网格外、背景与未知邻格都视暴露，但null保持未知数，不并入已知背景。输出标签栅格null保留null。
样例3目标，面积3/2/1格、周长8/6/4；共6目标格、5背景格、1未知格。空目标栅格合法。不是地形汇流、矢量多边形叠加、孔洞拓扑或物理面积估计；不读取GeoTIFF。

