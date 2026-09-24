# A题阶段2：连续拓扑块切分探索

本目录与正式 `solver/` 隔离，未修改正式求解器、原始输入、固定配置或官方评估器。

## 复现

在项目根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 A题研究/探索/partition_candidates.py --run-dir A题研究/探索/runs/partition_v1 --timeout 60
```

默认四图：071、064、051、049；默认P1/P2/P3，5核、单个官方评估worker。仅生成不评估：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 A题研究/探索/partition_candidates.py --generate-only --run-dir A题研究/探索/runs/partition_v1_generation
```

`generate_partition_candidates(ir, num_cores=5)` 返回 `{name, plan, metadata}` 列表。每份 `plan` 只有官方要求的两字段；辅助元数据不注入计划。

## 固定候选集合

1. 全局计算拓扑序：稳定ID Kahn序、按剩余计算关键路径优先的Kahn序。
2. 分块数：5、10、20。用归一化M/V累计工作量的分位点切连续非空区间；所有计算依赖均留在块内或指向后续块。
3. 两种分核代理：含P1式100/1000周期等待的通信EFT；二维计算负载与输入/跨核复制亲和。
4. 每块独立sg，每核按全局块序排列。结构检查验证商图与同核顺序联合无环；P2/P3的最终执行合法性完全由官方判断。

总计每图至多12份切分计划；每份分别评估3个问题，另各问题独立重评1个对照。因此本次共144次切分评估、12次对照，不能把“12份计划”写成“每图只调用12次评估器”。

代理未完整描述共享DDR的动态公平分配、spill、Pipe FIFO、L2命中；其分数不作为官方成绩。

尤其 `communication_eft` 是P1式静态代理：使用同核100、跨核1000周期等待。它不模拟P2的COPY完成后500周期释放机制；本轮把同一候选跨P1/P2/P3实测，P2取得改进不能证明该代理对P2预测准确。

## 产物

- `partition_candidates.py`：候选生成、串行评估和报告脚本。
- `runs/partition_v1/manifest.json`：脚本、正式求解器、官方源码与配置哈希。
- `runs/partition_v1/summary.json`：全部问题汇总与所选最好方案。
- `runs/partition_v1/attempts.csv`：156次调用记录，含状态、Makespan、额外搬运、耗时和原始记录路径。
- `runs/partition_v1/evaluations/`：隔离worker请求、计划、日志、原始官方gzip结果及缓存。
- `runs/partition_v1/case_*/plans/`：所有切分计划。
- `runs/partition_v1/case_*/p*_all_evaluations.json`：逐问题全部候选成绩，失败也保留。
- `runs/partition_v1/case_*/p*_selected.plan.json`：允许保留原对照后的所选可行计划。
- `runs/partition_v1/摘要.md`：自动表格。
- `runs/partition_v1/机制解读.md`：归因边界及后续建议。
- `mechanism_ablation.py` 与 `runs/partition_v1_ablation/`：071/P2的三个单活跃核条件，单独补充机制表，不计入原搜索主表。

对照是现有pilot中已评估的simple5核最好方案，经过图、配置、官方源码哈希核验并在本目录重新评估；case049原pilot受候选预算截断，这不等于穷尽simple搜索空间。本探索也不是同预算算法排名或全部100图结果。

## 操作级HEFT式补充探测

```bash
PYTHONDONTWRITEBYTECODE=1 python3 A题研究/探索/operation_heft_probe.py --run-dir A题研究/探索/runs/operation_heft_v1 --evaluation-budget 180
```

仅051、071、064、049、5核、P2；按此顺序，单worker，单次最多45秒，官方评估阶段总预算180秒。先生成全部候选，再开始评估计时。两种拓扑顺序×0/0.25/1三个通信代理权重，每图至多6份计划；051有一份重复，实测总计23份。

每个计算操作按M/V可用时刻、前驱完成和跨核COPY近似选择核心，按全局拓扑中同核连续runs合sg。真实500周期同步与60 bytes/cycle从未修改；权重仅影响候选生成。双对照引用此前核验过哈希的pilot整分量结果、连续块探索结果。全部官方记录、候选、选中方案、摘要及边界说明位于 `runs/operation_heft_v1/`。

## 实验性统一入口（P2/P3，1–5核）

`advanced_solve.py` 将已有候选生成方法接入统一接口，**仍是实验入口，尚未完成全量100图与严格同预算验证**。正式基线代码和基线报告独立保留，未被此入口替换。它不支持P1。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 A题研究/探索/advanced_solve.py \
  选题分析/A题附件/data/case_071.json -n 4 -p 2 \
  --config 选题分析/A题附件/data/config.txt \
  --run-dir A题研究/探索/runs/advanced_interactive \
  --max-evaluations 10 --timeout 45 \
  -o A题研究/探索/runs/advanced_interactive/case_071_p2_n4.plan.json
```

P3将 `-p 2` 改为 `-p 3`。`--config`省略时从输入图同目录读取；参数必须与题目固定值一致，注释或路径变化允许，容量/带宽/同步值变化拒绝。

可加 `--incumbent-plan <已有方案.json>`。该计划优先重新经过对应问题的官方评估，成功后才进入最好结果；若已有方案的硬件核心列表较短，会在末尾补空列表，保留原核心编号。硬件核心列表长于目标核数时明确拒绝，不擅自迁移操作。

预先固定的候选顺序为：已有incumbent（可选）、安全单活跃核、simple目标活跃核数的两种粒度、HEFT关键路径/稳定ID×通信权重1/0.25、两种同核优先序、一个连续块EFT备选。精确提交JSON指纹去重；提供incumbent可能增加候选数。默认最多评估10份，未评估候选与截断原因会记录，不假装穷尽搜索。

- 只按官方成功结果更新incumbent：Makespan优先、额外搬运字节破同分。失败、超时和执行环均保留记录。
- `--timeout`是每份计划上限；单worker。当前没有全局秒数截止，最大调用数由`--max-evaluations`限制，预算语义明确写入报告。
- `-o`输出只含官方两字段。旁边的`.advanced.json`保存选择结果、全部尝试、预算、生成失败和源哈希；`--run-dir/solves/`保留每次唯一会话目录、候选计划、incumbent与manifest，`evaluations/`保存官方worker证据。
- 摘要路径采用输出路径替换最后一个扩展名为`.advanced.json`，例如`result.plan.json`对应`result.plan.advanced.json`；`foo.advanced.json`对应`foo.advanced.advanced.json`，两者并不相同。求解前会解析所有路径及符号链接，并检查方案/摘要互不重合、都不覆盖图、配置或incumbent读取源。输入为`foo.advanced.json`时，`-o foo.json`会被拒绝；摘要符号链接指向输入或方案输出时也会被拒绝。覆盖已有incumbent输入不支持，需另选输出名。
- 源哈希包括本入口、依赖的两份探索脚本、正式solver和官方评估器。所有生成产物被限制在原始附件目录之外。
- P3候选的代理没有显式优化L2，最终结果仍由官方P3评估决定。`-n 1`也可能优化同核优先序，但不能替换题面指定的`singlecore_evaluate.py`加速比分母。

验证范围：071/064的1–5核全部生成结构、确定性及方案覆盖检查；12项小测试通过（含6项路径检查）；另有5条真实CLI smoke覆盖1/2/3/4/5核、P2/P3、已有incumbent重评及候选预算截断。共25份候选官方成功，结果保存在`runs/advanced_smoke/`。路径修复没有重复官方评估，也没有为此入口运行100图评测。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s A题研究/探索 -p test_advanced_solve.py -v
```
