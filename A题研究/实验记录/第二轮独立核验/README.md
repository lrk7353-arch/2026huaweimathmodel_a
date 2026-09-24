# 第二轮独立核验

本目录只读取已有方案并独立回放，不生成新候选、不改变参数或算法。选择在 selection.json 中固定；六图为预定的 044、049、051、071、082、093。

使用单 worker、单次 120 秒上限，在全新独立目录完成 8 次调用；8/8 success，全部 cache_hit=false。makespan、静态复制指标、缓存统计、内存峰值、完整时间线以及 graph/plan/config/官方代码/wrapper/worker 哈希均一致。官方文件、原图、配置、solver 与 advanced 顶层源码前后哈希均未变化。

## 回放结果

| 图 | 场景 | 现存最佳方案来源 | 原/重放周期 |
|---|---|---|---:|
| case_044 | P2 | trace_refine_v1 | 74588 / 74588 |
| case_049 | P2 | operation_all100_n5_v1 | 97671 / 97671 |
| case_051 | P2 | operation_all100_n5_v1 | 164392 / 164392 |
| case_071 | P2 | trace_refine_v1 | 9304 / 9304 |
| case_071 | P3 | cache_refine_v1 | 7952 / 7952 |
| case_082 | P2 | operation_all100_n5_v1 | 238028 / 238028 |
| case_093 | P2 | cache_refine_v1 | 15876 / 15876 |
| case_093 | P3 | cache_refine_v1 | 15794 / 15794 |

044/049/051/082 在这五个已完成高级实验中没有现存 P3 选中结果，本核验明确未覆盖；没有用其他计划的 P3 结果补齐。

**071 的两个场景不是同一计划。** P2 最优 trace 计划为 9304，尚没有它在 P3 的现存结果；P3 最优 cache 计划为 7952，这个计划已有的精确 P2 配对是 9688，不能写成 9304→7952 的同计划缓存加速。093 此次选中的 P2/P3 恰为同一精确计划，分别是 15876/15794。完整对应保存在 verification.json 的 exact_plan_cross_scene_table；没有对未知格作推断。

## 全 100 图 operation campaign 汇总

以下只汇总 operation_all100_n5_v1 本身，不混入随后 trace/cache 的选图改善。100/100 完成，调度层失败 0；共 1094 个候选记录，success 1094、invalid/cycle/capacity/timeout/runtime_error 全为 0。已有控制来自 full_initial_v1 的 simple-WCC P2/N5 成功计划；它不等同于所有方法中的最强控制。

候选记录中 24 条复用了 evaluator 缓存，1070 条未复用。没有将历史控制成本或缓存命中时间当成公平的同预算新运行。

| 指标 | 新候选单独 | 保留 WCC 的组合 |
|---|---:|---:|
| 胜/平/负 | 44 / 5 / 51 | 44 / 56 / 0 |
| 算术均值 T_control/T | 1.335821 | 1.480619 |
| 几何均值 T/T_control | 0.906993 | 0.753984 |
| 中位数 T/T_control | 1.005723 | 1.000000 |
| 官方口径均值 B/T | 3.285441 | 3.946238 |

控制的官方逐图单核加速均值 B/T_control = 3.337083。B 一律来自对应图的官方 singlecore 入口。裸 operation 虽有少数图大幅加速，仍在 51 图更慢，官方均值从 3.337083 降至 3.285441；保留成功 WCC 的组合才提高到 3.946238。不能只引用 1.335821 的平均控制比而忽略官方指标下降。两种平均不是同一加权口径。

保留方案的 44 胜/56 平/0 负是按官方 makespan 取较优的组合性质，不是任一单独候选必然更优。生成规则仍需跨核数与 P1/P3 全矩阵验证；当前全量证据仅覆盖 P2/N5。

## 来源与复现

- verification.json：8 次精确方案独立回放、结果记录、哈希、缺失格及同计划跨场景映射。
- selection.json：评估前冻结的候选来源与选择，覆盖 component/operation smoke/operation full/trace/cache 五个已完成运行。
- campaign_aggregate.json：100 图逐图数值、源路径/哈希、统计定义与记录状态。
- verify.py：只读选择/汇总与独立回放脚本；要求 fresh 目录不存在，防止误把复用当 fresh。

本核验不声称这六图是随机或独立测试集，也不把探索耗时与历史基线成本作等预算比较。
