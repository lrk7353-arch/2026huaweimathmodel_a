# 精修批量入口（v3）

`batch.py` 调用当前冻结的 `solve.py`，默认从零求解，不读取历史最优计划。默认选择 100 图、P1/P2/P3、N=1..5；正式 v3 主批必须显式传 `--cores 2,3,4,5`，对应 1200 槽。它属于每槽最多 90 次逻辑调用的强化实验，不与 v2 每槽 24 次的同预算消融混为一谈。

## 启动与预算

在工程根目录用 bundled Python 运行，例如：

```sh
/Users/liyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B A题研究/精修求解器/batch.py --cases all --problems 2 --cores 2,3,4,5 --workers 3 --seed 17 --run-dir A题研究/精修求解器/runs/formal_v3/full_p2_seed17 --evaluation-dir A题研究/精修求解器/runs/formal_v3/shared_evaluations --max-evaluations 90
```

这是命令示例；实际启动及依赖队列由主流程安排，本模块的交付验证没有启动全量实验。

- `--cases all` 或逗号分隔编号（支持 `071`、`case_071`）；`--problems 1,2,3`；`--cores 1,2,3,4,5`。按用户输入顺序保留去重后的选择。
- `--workers` 只并行不同图；每张图内按 problem、cores 顺序串行。不能把各图内部的多个求解器同时放大到 workers 倍。
- 默认 `--component-cap 6 --operation-cap 12 --selective-cap 18 --wcc-cap 9 --trace-cap 30 --cache-cap 18 --round-width 6 --max-rounds 5 --max-evaluations 90 --seed 17 --wcc-policy mixed`。场景不适用阶段由求解器置零；逻辑调用总上限包含初始计划，阶段上限的和不是必用预算。
- `--timeout` 不传时，按原图非 COPY 操作数 ≤10000 为 60 秒，其余 180 秒；显式传入可覆盖。批量 manifest 保存用户原始选择，slot 冻结签名及子进程保存实际超时值。
- `--data-dir`、`--config` 可指定输入；默认官方附件图目录及其 `config.txt`。
- `--evaluation-dir` 可指定共享精确缓存；不传时各 attempt 独立缓存。缓存命中仍由 solver 按逻辑调用计费。共享缓存目录与批量 run 目录必须互不包含，并且不得位于官方附件目录。
- `--incumbent-plan` 只允许恰好一个图/场景/核数的显式实验；默认没有历史初值。正式 v3 从零求解不传此参数。
- 无无限等待接口；如需等待其他队列，使用主流程的有期限驱动器。

重复同一命令并增加 `--resume` 可继续。所有参数（包含 workers、路径、预算）、源文件及输入必须与首次 manifest 相同；更换协议必须使用新 run 目录。

## 证据与恢复

`manifest.json` 冻结完整设置、所有选中原图 SHA256、官方配置 SHA256、controller 声明的 36 文件源依赖闭包、单独的 batch/solve 入口 SHA256、Python 版本及路径。batch 自身不放入 controller 源闭包，避免签名循环，但仍独立冻结并核验。

每槽路径为 `slots/case_071/p2_n2/attempt_0001`。对应 `.launch.json` 在启动前冻结签名、精确命令和 manifest 摘要；`.process.json`、`.exit.json`、stdout/stderr 记录真实启动与退出。attempt 中保留 solver 输出、每次候选及官方原始结果。失败后的 resume 新建 `attempt_0002`，不覆盖旧 attempt 或旧 launch。

只有 completed + success、源与输入验证通过、没有生成异常，并且计划/输出文件哈希及官方原始时间线验真通过的槽才可复用。核验包含：每次成功候选的计划和 gzip 结果 SHA、内容摘要、场景/核心数/配置/源参数、原始计算节点覆盖与时长、最终 makespan、真实最优选择、最终输出与获选计划一致，以及冻结 controller 的公开验真函数。旧记录损坏或失败保留证据并新建 attempt；源/图/配置/设置/launch 篡改属于硬失败，不静默改变协议继续。

若发现旧子进程记录 PID 仍活跃且无退出记录，保守记为 pending，避免重复启动。该判据可能受 PID 复用影响，需人工检查相关进程；不会据此把未完成槽算作成功。同 run 采用进程锁，防止两个批量主控同时续跑。

## summary 契约

`progress.json` 从启动前就含完整预定槽矩阵；最终 `summary.json` 同结构。每槽给出 outcome、state、feasible、search_completed、requires_review、source_status、attempt、logical_calls、makespan、计划证据和旧 attempt 历史。

汇总字段包括：

| 字段 | 含义 |
|---|---|
| planned_slots / reported_slots | 预定槽总数 / 已不再 pending 的槽数 |
| completed_feasible / failed / pending | 完整验证完成 / 失败或待查 / 未完成数量；不删除失败槽 |
| feasible_count / requires_review_count | 可行计划数（可含待查搜索）/ 生成异常或结构拒绝待查数 |
| all_slots_feasible | 每槽均有验证通过的可行最终计划 |
| all_searches_completed | 每槽均 completed_feasible；待查可行计划不满足此项 |
| source_hashes_unchanged | 最终源、图、配置、显式初值检查未发生变化 |
| selected_attempt_logical_calls | 当前选定且验证通过 attempt 的逻辑调用和；resume 不代表新增调用 |
| failed_attempt_reported_calls_unverified | 失败 attempt 自报调用数，明确未完成验真，不能当准确资源账本 |
| slots_with_unknown_current_call_count | 无法可靠读取当前调用数的槽数 |
| reused_count | 本轮从已完成证据恢复的槽数 |
| fatal_errors / slots | 全局完整性错误 / 全量槽列表 |

`generation_failures`、`rejected_candidates`、`controller_error` 任一存在都会进入 requires_review；即使保留成功 best，也不允许 `all_searches_completed=true`。候选的正常官方失败或超时计入搜索反馈，不自动当作生成器异常。

仅全部预定槽完成并验真、源未变、无全局错误才返回 exit 0。没有部分样本性能均值。预算按每个 attempt 限制；重试不是免费，旧 attempt 调用历史必须保留并在分析总耗费时一并考虑。共享缓存和并发下的实测墙钟不是冷缓存公平运行速度。

## 交付验证与冻结

- 14 项独立 batch 机制测试通过，覆盖失败矩阵、重试保留、源与设置不匹配、损坏证据、活跃旧进程、图级并行、参数透传与待查状态。
- `runs/batch_smoke_v1`：固定 case_071、P1/P2/P3、N2、每槽 cap4，共 12 次官方逻辑调用，3/3 完整成功。相同命令 resume 后 3/3 复用，launch 总数仍为 3，没有新增评测。
- `batch.py` SHA256：`1d5fcd87d5c3f6b5cadaa5432e058a10a3d5110a61e6a9b772043c3cc0465dc6`。
- `controller.py` SHA256：`77aae8d6f49cbb33d39e015a40beb872dd90a1e04f46cde193a5494cf6e5df7a`。
- `solve.py` SHA256：`e2af679194aa2ea6846b7100f76f2196206603b785fe760a24b9c9796ac4bdef`。

详细结果见 `runs/batch_smoke_v1/结果说明.md`。这仅验证工程入口及恢复，不构成 100 图性能结论。
