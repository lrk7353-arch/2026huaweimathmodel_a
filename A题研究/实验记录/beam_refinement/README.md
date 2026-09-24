# 有限beam的独立机制探索

本目录与`fairv2`正式方法比较分开。只调用冻结的`generate_trace_candidates`与官方`evaluate`，不修改advanced_solver、solver或既有探索依赖。

运行前协议保存在`protocol.json`，SHA256为`e12a4ab1a2d71c919307c2e191b0bd1cacc7b00686aa1156202650dd5346420a`。`preflight_v1/manifest.json`先核验了六个固定起点；正式运行在`run_v1/`，再次冻结协议、源码、配置、输入与原始成功记录，并保存源码副本。

## 搜索与预算

- 固定P2、5核，图044、049、051、069、071、082。起点取已完成operation_all100与trace_refine两份来源中目标值最低的正式成功计划；不随运行期间的新结果改变起点。
- 单路径greedy宽1，最多30次逻辑trial；beam宽3，最多66次。每图总上限96，全部六图上限576；最多5轮，每个父节点生成最多6份候选。
- greedy即使某轮不改善也继续预定轮次；beam始终保留全局incumbent，另外允许makespan不超过其108%的正式成功状态。
- 多样状态按与已选状态的最小距离做farthest-first选择；距离权重为核心分配Hamming 0.7、核内子图相对顺序0.2、全局字典相对顺序0.1。平局按makespan、额外COPY字节、计划hash确定。
- 全局incumbent只接受正式success且`(makespan, added_copy_bytes)`更小的计划。失败、超时不进beam；相同makespan但字节更少属于次指标改善。
- 每个策略内部跨父节点/轮次去重；两策略共享同一exact-plan缓存，但跨策略命中依旧各计一个逻辑trial。
- 单worker，60秒/次、每图累计评估墙时300秒、六图累计1800秒。生成和记录时间另计。调用数或时间未用满，不转移给其他图。
- 本协议只做P2，没有执行可选P3阶段，也没有扩大图或追加参数。

## 如何判读beam证据

每图使用实际共同前缀`m=min(30, greedy实际trial数, beam实际trial数)`比较同一初始计划下的incumbent。m=0表示不可比较。这个前缀比较匹配逻辑调用，**不匹配冷启动墙时或算力**：greedy先运行、共享缓存和失败耗时都会影响墙时。

beam最终66次上限的结果单独报告。最终结果优于greedy、但m处不优，不能据此声称beam结构在等预算下更强。轮数、每父候选数、生成器算子轮换固定；多分支也会改变深度和算子覆盖，这六图的机制样本不是泛化评测。

每个首次真实评估状态保留实际父节点、扩展轮次、目标值、当时incumbent及进入beam时的8%门槛。重复候选另记别名来源，不能事后换父链。两个证据分别报告：

1. 某祖先劣于当时incumbent。
2. 实际父→子目标严格变差，后续后代又改善。

第二种证据更直接说明本次运行实际经过了非单调路径，但不证明所有其他可能的单路径算法都到不了该解。

## 文件

- `run_v1/manifest.json`、`source_snapshot/`：冻结来源与协议。
- 各图`initial.json`：两类起点对照、选中理由与hash。
- 各图`greedy/`、`beam/`的`events.jsonl`：每次扩展、生成、重复、trial、失败、预算截断、每层beam选择及原因。
- 各策略`plans/`：所有实际尝试计划；`result.json`：完整记录、每调用前缀incumbent、首个真实父链。
- 各图`selected.plan.json`：greedy与beam中正式目标值最低的最终计划，可能来自任一策略；来源可以由summary的选中计划hash与两策略result核对。
- `summary.json`、`简报.md`：全部六图共同前缀与不同预算最终值；不只汇报改善图。
- `completion.json`：调用数、真实官方调用数、缓存命中可据逻辑数相减、时间与源码终检。

## 测试与重跑

`test_trace_beam.py`的7项独立测试使用人工搜索树，不调用官方评估器，覆盖：真实扩展变差祖先、保优单调、8%窗口、去重、预算截断、缓存与失败计费、零时间预算/生成失败回退，以及“beam多花一次才改善，共同预算仍判平局”的回归。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s A题研究/实验记录/beam_refinement -p test_trace_beam.py -v

# 仅做来源和hash预检，不执行正式评分。
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  A题研究/实验记录/beam_refinement/trace_beam.py \
  --prepare-only --run-dir A题研究/实验记录/beam_refinement/new_preflight

# 会重新产生最多576次逻辑trial；必须使用新/空目录。
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  A题研究/实验记录/beam_refinement/trace_beam.py \
  --run-dir A题研究/实验记录/beam_refinement/new_run
```

停止后的已有证据不会被覆盖；此研究脚本没有resume开关。新正式运行前应重新检查冻结协议与来源，不手工改已有运行的manifest来掩盖变化。
