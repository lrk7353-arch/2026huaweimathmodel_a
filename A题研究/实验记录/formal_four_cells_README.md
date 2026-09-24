# 正式 P3 四格补评控制器

脚本 `formal_four_cells.py` 只处理正式 `full_p2_seed17`、`full_p3_seed17` 的最终选中结果：100 图 × N=2、3、4、5，共 400 对。N=1 不在本次范围，后续单独补充。它不生成新方案、不改变官方参数，也不把交叉场景结果回流到正式选优。

## 冻结与评价

等待两个主批次的最终 `summary.json` 同时满足 400 个搜索全部完成、源码未变；启动时已冻结源码、图数据、配置和两个批次 manifest 的 SHA256。有限依赖等待默认最多 24 小时，每 30 秒读一次状态，不创建自动化任务。

主批次齐全后，逐项检查选中方案与官方 record 的精确计划 hash、图、配置、场景、核数、原始 gzip、makespan、COPY 指标和 cache_stats。主批次 summary、每个选中 slot summary、发布计划、输入计划、record 和官方结果的 SHA256 都进入 `selection_manifest.json`；自有方案副本同样冻结。仅在整个选择清单落盘后开始补评。

- T2(π2) 与 T3(π3) 引用正式批次已经验证的选中记录，不称为新回放。
- T3(π2) 与 T2(π3) 逐一调用标准 `evaluate`；共享 `formal_v2/evaluations` 的精确缓存。即使两计划相同或命中缓存，两个逻辑调用也都计费。
- 默认单 worker；CLI 最多允许 2 worker。本次按单 worker 启动。
- 按 GraphIR 计算算子数：不超过 10,000 个时单次 60 秒，超过时 180 秒。原始 COPY 算子不计入这个阈值。
- 全任务所有尝试累计最多 800 个逻辑交叉调用，包含失败、缓存命中和中断预约。每次先保存 reservation 再调用 evaluate。
- 正式结果无可用方案、验证失败、运行失败和超时分别留存，不能把缺失作为通过。完整主批次中的无可行解 slot 不重新优化，也不会换用探索方案。

## 断点恢复和失败

输出必须是 `实验记录` 下的新建/空专属子目录，与正式实验树不重叠。已有实验必须传 `--resume`，恢复时验证启动参数、源码和全部冻结文件；已完成交叉结果再次检查精确输入、record 与原始结果，默认不重复任何已尝试的失败格。

若进程中断时只保存了 reservation，该次仍计预算并标为 `interrupted`，不假设 worker 未启动、不自动重跑。若 wrapper 返回失败但未能保存自己的 record，返回对象仍写入控制器日志，保留启动前失败证据。确认新 worker 数只统计非缓存且有进程 returncode 的 record；另列非缓存调用数、未恢复结果数，避免把启动前错误当成运行过的 worker。

显式 `--new-attempt case_001_n2/t3_pi2` 才请求重试已有失败格；历史尝试不覆盖，每次继续计总预算。800 次耗尽后不能再评，重试也不扩大预算。若在尚有未评格时重试，它会占用同一总预算，末尾未评格会明确标记预算耗尽。因此默认完整覆盖协议不主动重试。

## 输出口径

`pairs/<case_n>/four_cells.json` 保存四个完整 record、各方案精确 hash、每格 makespan、静态新增 COPY、动态 cache hit/miss bytes、字节命中率及评价缓存复用标记。`cross_calls/` 保存每次交叉尝试，`progress.json` 保存独立进度，`four_cells.csv` 便于后续分析。

逐图定义：H=T2(π2)/T3(π2)，S=T3(π2)/T3(π3)，R=T2(π2)/T3(π3)。大于 1 表示加速；S 小于 1 的负结果原样保留。H×S=R 只是同一图的代数恒等式，不能把各图均值相乘，也不能据此宣称独立因果效应。另外输出 π3 下的硬件比值和 P2 下的策略比值，便于识别跨场景调度变化。

只有某一 N 的全部 100 图四格成功，才输出该 N 的 H/S/R 算术均值、几何均值和中位数。官方平均加速比另按 mean(B/T) 计算，B 只引用 `full_initial_v1` 中已经存在且通过 hash/原始结果核验的官方单核 problem=0 记录；不新增单核调用。任一 B 缺失则该 N 的官方均值为 null，不以部分图替代。单核分母存在不代表本次覆盖 N=1 的多核计划实验。

交叉结果即使更好，也不替换冻结的 π2/π3。它可以由后续、独立声明的 best-known 交付库使用，本次正式四格保持原样。

## 启动与恢复

使用原正式实验的 bundled Python；源码冻结后不要编辑控制器。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/liyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -u -B A题研究/实验记录/formal_four_cells.py --wait-for-main --workers 1
```

恢复同一实验：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/liyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -u -B A题研究/实验记录/formal_four_cells.py --resume --wait-for-main --workers 1
```

## 启动前验证

`test_formal_four_cells.py` 使用临时 fake evaluator 检查预算、缓存逻辑计费、失败保留、显式重试、reservation 中断、record 持久化失败、身份/目录保护、100 图完整性门槛和超时边界；另只读核验已有 10 图四格的 40 份官方 gzip，确认 071 的负策略效应和 093 的正效应没有被抹掉。测试不调用官方 evaluate、不新增真实评测。测试证据另存 `formal_four_cells_validation.json`。
