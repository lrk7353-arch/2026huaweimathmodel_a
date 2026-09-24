# 计算交付打包与便携验真

`make_delivery.py` 从 `collect_best_known.py` 生成的目录读取 `catalog.csv`、`manifest.json`、各方案旁的 `.provenance.json` 和原官方结果。只复制和验真，不调用官方评估器。

默认必须具备完整 **100图 × P1/P2/P3 × N1–5 = 1500** 个唯一组合。缺失、重复、未入索引的计划、错误 SHA、场景/核数/指标不符均拒绝。`--allow-partial` 才能生成开发快照，包内 `partial=true` 并逐项列出缺失组合；默认 verifier 也拒绝 partial，须再次显式允许。

```sh
python3 -B A题研究/实验记录/make_delivery.py --portfolio A题研究/当前最佳方案_v3_阶段快照 --output A题研究/计算交付_v3_开发快照 --allow-partial
python3 -B A题研究/计算交付_v3_开发快照/verify_delivery.py --allow-partial
```

完整收尾调用去掉两处 `--allow-partial` 并使用新输出目录。`--snapshot` 可额外保存原目录的 catalog、manifest、provenance、record 原文；其中绝对路径仅作追踪，主索引、原始结果定位和执行命令一律使用包内相对路径。

Python API：

```python
build_delivery(portfolio, output, *, allow_partial=False, snapshot=False, workspace=WORKSPACE) -> dict
verify_delivery(directory, *, allow_partial=False, manifest_sha256=None) -> dict
```

CLI 成功返回 0，拒绝或失败返回 2。成功统计含 `status`（`verified_complete` / `verified_partial`）、`selected_count`、`missing_count`、`inventory_files`、`manifest_sha256`。有限收尾队列应先等待求解器源码冻结与组合库收集完成，再调用打包器；它本身不等待、不调度、不补齐缺失成绩。

输出必须是不存在的新目录，官方目录、输入组合目录、运行源码树及其祖先/后代受保护。内容先在临时目录完整核验，再以独占 mkdir 保留最终目录，校验清单最后发布；如果发生发布中断，残留目录标为 `INCOMPLETE_DELIVERY` 且不会被 verifier 接受，也不会在重试时覆盖。

保留原相对树，运行代码精确取冻结的 36 文件依赖闭包加新 `精修求解器/batch.py`，共 37 文件；不扫描混入尚在研究的新模块。另保留100图、原config及必要说明文档。说明文档在主体复制结束时独立抓取稳定快照，后续编辑不改变已冻结代码/输入的核验口径。运行入口使用 `精修求解器/solve.py`。备用和旧实验 CLI 只按依赖需要附带，不承诺缺少历史档案的旧流程可复跑。原缓存不搬作新机器可续跑缓存；不打包会依赖缺失归档 fixture 的项目测试。CPU 模拟、无需 GPU，打包和验真要求 Python 3.12 或更新版本。

验真器是独立标准库脚本，不导入执行求解器。逐 inventory SHA/文件大小检查后，还独立检查：两字段计划覆盖、块依赖与核顺序无环；selected 文件 SHA 与紧凑评估输入 SHA 分开核对；graph/config/official/wrapper/worker 对应哈希；原始 gzip 场景、核数、固定配置和全部 wrapper 指标；原 compute op 在 raw timeline 中的覆盖、类型、pipe、时长、核/子图归属、Task 顺序、结束 makespan。显式检查运行依赖闭包，包含 source_hashes 动态读取但不直接 import 的备用文件。

同名字段的文件 hash 与计划内容 hash 不可互换：组合库 catalog 的 `plan_sha256` 是格式化文件的 SHA；交付 index 明确拆成 `plan_file_sha256` 与 `plan_sha256`（保持对象及列表顺序的紧凑评估输入 hash）。原官方 gzip 不重新压缩。

`test_make_delivery.py` 的小型合成树测试涵盖缺覆盖拒绝、重复、丢文件/篡改、重新计算 inventory 后的指标/时间线错配、动态依赖缺失、复制中源文件变化、目录碰撞/悬空符号链接/发布间隙目录竞争，以及删除原“机器”目录后的搬迁验真。没有真实评估调用。独立只读审查另核验了已有三场景/多核官方档案，未新增模拟。

包内 SHA 用于完整性与证据对应检查，不是数字签名；可在别处保存返回的 manifest SHA，交给 verifier 的 `--manifest-sha256` 做独立摘要校验。当前最好方案组合不等于同预算算法成绩，也不证明全局最优。
