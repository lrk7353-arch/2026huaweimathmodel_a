# 从原始图开始的统一求解链

`solve.py` 支持 P1/P2/P3、1–5 核，从原始图生成方案并交给未修改的官方评估器验证。没有图号分支、历史目录查询或内置历史优解。它整合已有组件、操作分配、选择性切分和精修模块；正式 `formal_v2` 的代码和成绩独立保留。

```sh
python3 -B A题研究/精修求解器/solve.py \
  选题分析/A题附件/data/case_071.json -n 5 -p 3 \
  --run-dir 新建运行目录 -o 最终方案.json
```

省略 `-o` 时输出 `run-dir/best.plan.json`；输出 JSON 严格只有 `node_to_subgraph`、`core_schedules` 两个字段。运行目录必须尚不存在，输出文件也必须是新路径。`--config` 默认图目录下的 `config.txt`，全部官方机器参数必须保持固定。只使用 Python 3.9+ 标准库。

可选 `--incumbent-plan 已有方案.json`：首先在本次问题/核数下重新官方评分并计费；初始失败仍可从图继续生成。可选 `--evaluation-dir 独立缓存目录`：复用精确图/计划/配置/官方实现/runtime 键一致的成功缓存；命中仍占一次逻辑调用，不等价冷启动耗时。程序不自动复用历史 plan。

## 默认阶段与预算

|参数|默认值|用途|
|---|---:|---|
|`--component-cap`|6|全问题：完整弱连通分量装箱与粒度候选|
|`--operation-cap`|12|P2/P3：操作级分核、通信/内存优先序候选|
|`--selective-cap`|18|P1：重分量局部连续块及 phase-band 诱导分量切分|
|`--wcc-cap`|9|P2/P3：当前 best 的同核多分量窗口交织|
|`--trace-cap`|30|P2/P3：轨迹驱动通信、负载、优先序邻域|
|`--cache-cap`|18|P3：缓存事件与优先序邻域|
|`--round-width`|6|每轮每精修阶段的提案上限|
|`--max-rounds`|5|trace/cache 反馈轮次，不因单轮停滞停止|
|`--max-evaluations`|90|所有阶段共享的逻辑调用上限|
|`--seed`|17|确定性的候选种子|
|`--timeout`|自动|原 non-COPY 操作 ≤10000 时 60 秒，否则 180 秒；显式传值优先|

顺序为：可选初始复评 → 组件 → 无可行 best 时一次单活跃核 fallback → P1 selective 或 P2/P3 operation → P2/P3 WCC → 逐轮 trace、P3 cache。每轮都从最新 best 重新读取官方轨迹。计费包括初始、fallback、缓存命中、失败和 pending 预留；重复计划与严格 P1 下界剪枝不调用评估器。

stage cap 是调用上限，也是该阶段一次生成时的提案上限；重复、不可用或剪枝后不会暗中扩大候选池。总 cap 是上限，不承诺用满，不表示不同方法实际消耗相同。上述默认配置是工程选择，尚不能据此宣称全量、同预算优势。

P1 的必要下界只用于本题 selective 候选：按真实任务内直接计算依赖和各 pipe 工作量，叠加官方跨核 Task 1000、同核相邻 Task 100 的约束。只在 `LB > 当前最好 makespan` 时剪枝，等号保留，因为仍可能减少复制字节。不会把这一 Task 下界用于 P2/P3。

## WCC 混合候选

`--wcc-policy mixed|protected|unrestricted` 默认 `mixed`。protected 是冻结 v2，保留不适合窗口化的核心的控制优先序；unrestricted 是冻结 v1，也允许这些核心的合法内部重排。mixed 首先输出共享重编码控制，再交替选择两类的唯一计划；一类耗尽时由另一类补齐，最多使用 WCC cap。每项记录实际 variant 和同一计划的所有来源。选择不读取官方分数或图号。

它们是固定分核的表达顺序候选，不保证保持原 Step1 执行序或动态容量。当前 best 含跨核切开的 WCC 时，两版均明确返回不适用；控制器保留诊断并继续其他精修阶段。

## 审计和失败处理

每次调用之前先原子写 `trials/NNNN.plan.json`、pending 记录和 `checkpoint.json`，返回后填入结果。因此硬终止留下的未知调用仍有编号并计费，不能声称已经完成。`--timeout` 只限制单个官方 worker；此入口没有整体硬墙时监督或自动 resume。

接受 best 要求官方 success 且 worker 退出码为 0，原始 gzip SHA、精确计划字节、图/配置/官方实现/wrapper/worker 哈希全部一致；原始 scene、核数、机器参数、目标、P3 cache_stats 及每个原计算操作的核心/Task/子图、覆盖和时长也要匹配。图的身份以字节哈希为准，合法缓存命中允许同字节图曾使用其他文件名。目标为 `(makespan, added_copy_bytes)` 严格词典序。失败与更差候选不会替换 best。

`manifest.json` 固定输入、显式源码依赖和参数；`generation/` 保留全部提案与诊断；`evaluations` 列表保留每次 pending/returned 状态；`stages`、`pruned`、`duplicates`、`generation_failures` 留在 summary。每阶段前后及结束时核查输入/源码，结束前重验 best；证据变更中止，历史 best 仅供审计，不发布新输出。

输出与摘要、输入、配置、源码、官方附件路径交叉检查，并拒绝已存在文件、缓存写入关键目录 symlink 和存储重叠。路径保护在写入之前完成，结束时再次避免覆盖运行中出现的输出文件。

## 程序接口与 batch 验真

`controller.Solver(...)` 与 CLI 参数名对应；主要输出字段：

- `best = {plan, record, plan_sha256, stage, name, metadata, ...}`。
- `completed` 表示这次搜索协议结束；`status` 区分 success、no_feasible_result、integrity_or_controller_failure。
- `source_and_input_hashes_verified`、`logical_calls`、`returned_calls`、`pending_calls`、`stage_calls`。
- `requires_review`：意外生成异常、结构拒绝或控制器错误需要审阅。可行 best 可以保留，但正式 batch 不得把这种运行默认为正常完整成功。
- `plan_sha256` 是保留插入顺序的 compact JSON 内容哈希；`plan_file_sha256` 是归档的缩进计划文件哈希。官方 record 的计划文件采用 compact 格式。
- `stage_order` 是 manifest 中的阶段协议；`stages` 是执行日志，两者不可混用。

`controller.source_hashes()` 包含运行模块及所有借用依赖，不递归加入 batch、文档或测试。`controller.verify_summary(path, expected_settings=None, require_completed=True)` 不调用评估器：默认要求已完成且成功、无 requires_review，核对当前源码、输入、manifest、全部成功 trial 的原始证据、预算及输出，返回完整 summary 并增加 `verified=True`。`expected_settings` 按顶层字段严格比较，可包括完整 requested/effective caps 字典、问题、核数、seed、有效 timeout、WCC policy、初始方案哈希等。它拒绝明确标记使用测试 hook 的运行。

失败或未完成的目录不得直接当作成功 resume；batch 应保留原目录，再创建新 attempt。硬终止的 checkpoint 只用于计费和诊断，不能省略 pending 后虚称完整。

机制测试：`python3 -B A题研究/精修求解器/test_delivery_solve.py`，19 项通过，全部为合成/只读测试，零官方调用。真实 smoke 的冻结协议、全部评分和验真结果另存 `runs/delivery_smoke_v1/`；它只证明接口与小规模机制，不是全量性能结论。

|smoke|从图生成的首个方案 makespan|最后 makespan|实际官方调用|说明|
|---|---:|---:|---:|---|
|P1 / 071 / 5 核|18919|13396|6|selective 改善，另有 3 条严格必要下界剪枝|
|P2 / 008 / 5 核|100603|55981|9|组件、操作、mixed WCC、trace 均执行；WCC 候选改善|
|P3 / 071 / 5 核|18919|8212|6|操作方案胜出；trace/cache 完整记录未改善；WCC 明确不适用|

总共 21 次、单 worker、全部官方 success；三份运行均 completed、requires_review=false，公开 `verify_summary` 全部通过，源码哈希保持不变。这里没有外部 incumbent 或外部缓存，首个方案不是该图已知历史最好分母。

冻结 `controller.py` SHA256：`77aae8d6f49cbb33d39e015a40beb872dd90a1e04f46cde193a5494cf6e5df7a`。
冻结 `solve.py` SHA256：`e2af679194aa2ea6846b7100f76f2196206603b785fe760a24b9c9796ac4bdef`。
