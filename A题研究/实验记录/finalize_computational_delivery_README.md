# 有限计算交付收尾流程

`finalize_computational_delivery.py` 是当前实验的一次性收尾驱动器。导入模块不会启动任何任务。经过21项假流程测试、真实只读预检查及独立接口审阅，已于2026年9月24日启动正式依赖等待，参数为等待最多48小时、每执行阶段最多6小时。实时状态以 `完整计算交付收尾_v3/status.json` 为准；启动时依赖尚未齐全，未开始真实收集、继承评测或打包。它不调用自动化服务、不生成论文、不上传。

## 等待和完成条件

默认最多等待 60 小时，间隔 15 秒。必须同时满足：

1. v2 三条队列完成，14 个预定批次合计 3100 槽均完整、可行、源未变；包含 N1 诊断、同预算对照和种子稳定性。
2. v3 三条 `queues/{a,b,c}.done.json` 完成，7 个预定批次合计 1600 槽均完整、可行、源未变。
3. `正式P3四格_v2/summary.json` 含精确的 100 图 × N2..5 共 400 个完整四格，源未变、无未解决调用或控制器错误；还须 `progress.json` 的 phase 为 finished，确认 summary 之后输出的 CSV 已关闭。`controller_error.json` 存在时立即停止。

不仅相信汇总布尔值，还检查队列任务名称、退出码、每批的图/问题/核数/种子、逐槽精确覆盖、可行性及搜索完成标志。等待期间出现失败槽、needs_review 标记、非零退出或四格调用失败即停止，不把现有可行计划误作全部实验完成。某条队列缺少最终文件时仍是 pending，超过等待期限记 timed_out，不自动重试。

启动前核对文件稳定 2 秒后冻结：controller 声明的 36 文件源闭包、新 batch、driver 自身、6 个汇总/收集/打包工具、core_inheritance、实际本地导入依赖、v2/v3 协议及队列/四格驱动器。当前只读检查共识别 51 个源/协议文件。另冻结 100 张原图、官方配置、旧阶段快照 manifest、6 份队列 manifest、四格 launch 和已有 v2 主 manifest，共 110 个不可变输入。后续源或输入变化会硬失败。

## 顺序执行的八阶段

| 阶段 | 输出或门槛 |
|---|---|
| summarize_v2 | 3100/3100 验证完成，零审计错误，所有分组全 100 图 |
| summarize_v3 | 1600/1600 验证完成，零审计错误，所有分组全 100 图 |
| collect_before_inheritance | 沿用旧阶段快照 benchmark/exploratory 根，增加正式四格；新目录必须精确 1500 份、零 reject |
| core_inheritance | 对完整 catalog 运行冻结脚本，逻辑 cap1200，单进程串行，共享 formal_v2/evaluations |
| collect_complete_portfolio | 将该继承目录明确加入根列表，再生成完整新目录；再次核对 1500 份和零 reject |
| summarize_portfolio | 15 组各 100 图均齐全，按题面口径计算指标 |
| make_delivery | 默认完整模式，不传 allow-partial；构建器返回 verified_complete |
| verify_delivery | 用包内冻结 verifier 复核，并传入刚保存的 manifest SHA；不传 allow-partial |

继承每个候选必须实际经过原官方评测。不会假设补空核后时间不变；成功但变慢/无改善的负例允许保留并继续。官方失败或超时会停止后续链，保留真实结果；不会重复失败来耗费额外预算。collector 在调用前后比较候选 record 集合及其哈希，避免在收集时悄然吸收仍在变化的实验。

继承预算独立于 v2/v3。候选少于 1200 时不补重复调用；每个目标最多选一个最佳低核来源，具体选择由冻结 `core_inheritance.py` 决定。最终是历史及额外预算的 best-known 方案集，不是一个新的同预算算法成绩，也不证明全局最优。共享缓存命中仍计逻辑调用；混合缓存和并发墙钟不能宣称公平冷启动速度。

## 启动命令与输出

主流程审阅后可运行：

```sh
/Users/liyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B A题研究/实验记录/finalize_computational_delivery.py --max-wait-hours 48 --max-stage-hours 6
```

默认路径：

- 状态与逐阶段日志：`实验记录/完整计算交付收尾_v3`
- 继承前完整 catalog：`当前最佳方案_继承前完整v3`
- 继承实验：`实验记录/完整v3低核继承`
- 最终完整 catalog：`当前最佳方案_完整v3`
- 完整计算包：`计算交付_v3_完整`

`--run-dir`、`--pre-portfolio`、`--portfolio`、`--package` 可显式指定新目录。所有这些目录及固定继承目录都必须不存在，空目录也不接受；与官方附件、算法源码、候选输入根或彼此包含的路径会被拒绝。没有 resume，也没有自动重跑；失败后的进一步处理必须另行决定，不能用同命令覆盖旧证据。

每阶段执行前保存 `pending.json`，包含命令、冻结 launch SHA、日志路径和时间上限；执行中保存子进程 PID，结束后保存 `result.json`、退出码、日志 SHA、验证结果。代码默认每阶段最多 24 小时；主流程约定按上述命令收紧为等待 48 小时、每阶段 6 小时，以实际 launch 记录为准。这些是停止上限，不是预计耗时。超过上限终止对应进程组；源变化同样终止当前子进程组。继承若触及期限，保留实际账本等待讨论，不重复运行。

`status.json` 给出 state/completed、各阶段、依赖 receipts、路径、精确覆盖、调用账本和错误。未完成依赖不生成性能均值。对继承中断，已经落盘的 pending 计划按保守调用预留计数，区分完成记录与未解决预留；未解决不代表确认有官方 worker 执行。已完成 v2/v3 的调用另列为历史成功 attempt 的调用数，明确不含其早先失败 attempt，不能冒充历史总资源消耗。

任何失败都会保留 driver 的阶段目录、日志和已产生的 catalog/继承/已发布包。冻结打包器内部在失败时会删除自己尚未发布的临时 staging 目录，这是现有打包器行为；driver 不修改该代码，也不声称保存了被打包器自行删除的临时文件。

## 验证状态

21 项测试使用临时目录、假结果与假子进程，真实官方调用数为 0。覆盖完整链、1499 份阻断、零 reject 要求、错误矩阵、失败和异常保留、继承超时账本、padding 负例、源变化、等待期限、鲜新目录、包内拒绝 partial、重复槽识别、运行时一致性及四格发布时序。

只读真实预检查记录在 `finalizer_readonly_preflight.json`；这是接口/目录与依赖状态检查，不是启动 launch，不表示正式实验已经完成。最终启动时会重新捕获并冻结全部实际 SHA。
