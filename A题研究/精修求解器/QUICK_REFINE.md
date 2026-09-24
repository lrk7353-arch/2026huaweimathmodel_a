# 快速局部试错入口

`quick_refine.py` 只支持 P2/P3、1–5 核，从指定 incumbent 开始。它复用冻结 controller 的完整官方验真、精确去重与 pending 计费，独立执行 trace/cache 轮换。没有 component、operation、WCC 或初始失败 fallback。未改冻结源码，结果属于 **extra-budget rapid exploration**，不能混入 formal_v2/v3 公平实验。

默认参数：

|问题|trace 上限|cache 上限|总调用上限|每轮候选宽度|最多轮数|
|---|---:|---:|---:|---:|---:|
|P2|8|0|9|4|2|
|P3|4|4|9|4|2|

总上限包括初始复评、失败、超时和 exact-cache hit；重复计划不调用，故实际次数可能少于上限。各轮基于当前最好方案重取轨迹。一次停滞不停止搜索；stage cap 达到后不再运行该阶段。P3 总预算很小时，第一轮可能已经用尽两阶段 cap，不能宣称默认必定覆盖两轮新邻域。

从中文题目目录运行，例如：

```sh
python3 -B 'A题研究/精修求解器/quick_refine.py' \
  '选题分析/A题附件/data/case_071.json' -n 5 -p 2 \
  --incumbent-plan '/实际路径/已验证.plan.json' \
  --run-dir '/实际路径/全新快速试错目录' \
  --time-budget 120 --timeout 30 --max-evaluations 9
```

初始计划必须操作覆盖、计划结构和配置核数匹配，并在本问题重新官方评估成功；初始失败立即结束，只收费这次调用。`--evaluation-dir` 可选，传入现有 exact-cache 目录会按严格计划/图/配置/官方实现键复用成功结果，仍计一次逻辑调用。省略时使用本 run-dir 下的私有空缓存。`--config` 默认原图同目录的 `config.txt`，固定机器参数不可更改。

`--run-dir` 必须不存在，默认输出为其中的 `best.plan.json`。接口还支持 `--trace-cap`、`--cache-cap`、`--round-width`、`--max-rounds`、`--seed`。没有 `round-start` 或跨 run seen-list；重新传 best 启动新目录，会重新从 round0 开始。缓存命中旧候选不能算作新探索。

## 时间预算和完成标志

`--time-budget` 默认 120 秒，是**软总预算**，从 QuickRefiner 构造开始计时，包含输入检查、生成、评估和验真；不含 Python import/CLI 启动。生成前、生成返回后、每个候选开始前检查，到期不再开新动作。已经开始的生成、候选评估及最终验真/落盘允许超过截止时间，所以不能承诺每例精确 2 分钟。

`--timeout` 默认 30 秒，只限单次官方 worker，支持 `(0,60]`。它不是总墙时上限，也不会为了宣称限时而修改官方场景参数。没有外部进程组硬截止功能。

产物包含 manifest、逐候选 plan/record、生成诊断、checkpoint、summary、最好计划。manifest 明确绑定本文件、冻结真实依赖和 graph/config/incumbent 哈希。每次 worker 调用前先写 pending 并计费。失败与负结果完整保留；只有经官方 raw/gzip/场景/核数/原操作/时间/搬运量/缓存统计验真通过的严格词典序改善才能替换 best。

summary 的主要字段：

- `completed` / `profile_completed`：是否正常到达本次短 profile 的调用、阶段或轮次上限且没有生成异常/结构拒绝。软预算提前停止、初始失败、生成异常、候选结构拒绝、控制器或完整性错误均为 false；发生异常的 stage 也标记未完成。
- `configured_matrix_completed`：始终 false；单次快速入口不代表任何完整矩阵已完成。
- `stop_reason=soft_time_budget`：时间停止，记录具体 `deadline_boundary`；`partial_result=true` 且 `best_official_verified=true` 才表示有可用的部分最好结果。
- `source_and_input_hashes_verified`、`requires_review`：完整性与意外生成/候选错误信号；完整性失败的 best 只保留审计用途。
- `stop_reason=generation_failure/candidate_rejected/generation_and_candidate_failure`：相关异常导致 profile 未完成；`search_stop_reason` 另保留原调用/轮数/时间停止原因。已验真的 best 仍可作为 partial result 保留，不把后续改善掩盖前面的异常。
- `logical_calls`、`pending_calls`、`cache_hits`、`stage_calls`：完整调用记账。硬中断后 pending 仍已预留，不自动重试。
- `elapsed_seconds`、`soft_budget_overrun_seconds`：实测耗时和软预算尾部超出量。

正常完成且有有效 best、无待审异常时进程退出码为 0；软停止即便保留可用 best，也退出 1，避免上层把部分结果当完整运行。参数错误退出 2。若最后一个允许候选已经开始并在 deadline 后返回，且确实完成了整个配置 cap，允许 `profile_completed=true`，同时 `deadline_reached=true`、尾部超时量非零；这仍不是硬限时承诺。

现有 v3 `verify_summary` 绑定自己的源码闭包，不应直接拿它验证本新入口摘要。本入口运行时直接使用其 `Solver.verify_success` 对每次官方记录和最终 best 严格验真；完整验证结果及源哈希另记在本 summary 中。

## 当前验证范围

`python3 -B A题研究/精修求解器/test_quick_refine.py`：13 项 fake 测试通过，未产生官方调用。覆盖初始失败不继续、评估/生成越过时间边界后停止新候选、初始前已到期、cache/失败/重复计费、停滞后次轮改善、pending 中断、P3 继承本轮 trace 新 best、1–5 核、源漂移与 raw 统计污染拒绝、默认参数与边界，以及生成异常/结构拒绝绝不冒充完成（后续改善也仍是 partial）。

fake 结果显式标为测试证据，不能证明算法性能或真实耗时。官方小样本 smoke 由主任务另立目录、固定起点后执行；最终全量和公平验证仍独立保留。
