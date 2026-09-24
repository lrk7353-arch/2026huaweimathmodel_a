# Trace 邻域模块

`trace_refine.py`仅生成候选，不读取路径、写文件或调用官方评估器。主控持有最新的正式成功计划与其原始官方结果，每轮生成、评估候选，选中后重新加载该候选的官方时间线，再调用下一轮。不要拿旧计划的时间线给新计划继续迭代。

```python
from advanced_solver.trace_refine import generate_trace_candidates

candidates, diagnostics = generate_trace_candidates(
    ir, incumbent_plan, official_result,
    num_cores=5, max_candidates=12, round_index=0, seed=0,
)
# candidate严格只有name、plan、metadata；plan严格只有官方两个字段。
# 主控调用官方P2/P3评估，只有success才可按makespan择优。
```

支持正式图1–5核，P2/P3通用。**官方P2和P3的scene都为`"B"`，P3额外有`problem: 3`**；P3可选的`memory_path/cache_hit/cache_tensor_id`不替代原图tensor ID，也不取消跨核500周期释放约束。默认参数可重复，输入对象不变；`max_candidates=0`生成空列表，其他非法预算/核数/计划或时间线不一致会抛出`ValueError`。无跨核transfer、只有一个活跃核心时，仍可生成负载或优先序候选；N1只生成可用的控制与优先序候选。

## 主控必须做的检查

模块检查compute覆盖、当前核/子图映射、每核子图顺序、COPY时间线与释放、原tensor路由完整集合。它不能仅凭无哈希的官方result字典证明文件来源。调用前，主控必须验证：原记录status=success，problem正确，**精确计划文件SHA256**与该记录一致，graph/config/official源码与当前运行一致，压缩官方结果SHA256一致。字典的插入顺序可能影响编码，不宜只按无序JSON内容判断同一输入。

模块输出均通过`solver.plan.validate_plan`的操作覆盖、子图商图与核心顺序合并无环检查。官方编译后的Pipe FIFO、内存复用、动态搬运仍可能造成失败；这一步不能被结构检查替代。失败候选应记录并舍弃，原incumbent必须保留。任何结构不合法的来源计划直接拒绝。

## 顺序恢复与可比较性

先检查原计划`node_to_subgraph`的插入顺序是否为合法计算拓扑，且同核子图顺序不倒退。满足时使用该全局序；否则以原IR做稳定ID的Kahn拓扑序，记录`order_fallback`与`order_source`。

迁移和交换后按该全局序重建连续run子图，并保留原子图边界，防止将原来单核的20个优先块默默压成1块。若基础重编码仍与输入计划不同，最先生成独立`reencoding_control`，明确这可能已改变优先序。不能把相对于原计划的全部收益归因于迁移。

固定分核的priority族会有意修改全局顺序及子图粒度。最长同核run操作数为`ceil(compute_ops / (num_cores * 32))`，至少1；这一分割给单活跃核提供真实的子图优先级控制，而不是仅改变JSON键顺序。

## 候选族

三族轮流占用候选预算，去重后继续补齐，避免某一族占满。重编码控制若存在会先占一个名额。

|族|具体邻域|排序依据与限制|
|---|---|---|
|communication|生产者/消费者单点迁移；最多3个操作的同原核、无分叉依赖链迁移|COPY_IN较晚结束、释放后排队等待、字节数；每轮轮换生产者/消费者/链策略和late/wait排序|
|load|同Pipe负载热点操作迁往较空核心；热点与较轻操作交换核心|静态Pipe工作量峰值下降，跨核路由字节变化，再结合观测结束时间；不是makespan预测|
|priority|固定核分配的计算依赖尾长、跨核500释放依赖尾长、稳定ID顺序|原compute DAG上的优先序；跨核尾长每条跨核依赖加500，未还原完整执行关键路径|

通信映射从原图Op→Tensor→Op建立，按实际source/target core过滤。只使用能唯一识别原图源核producer的路由，保留目标核全部消费者；移动一个消费者不保证删除整个路由。未知synthetic tensor或非唯一producer会记录在`skipped_transfers`，不猜映射，其余族仍可工作。已知tensor的尺寸/路由/COPY时间矛盾会直接拒绝。

`structural_cross_traffic_delta_bytes`是直接原图tensor路由字节差，每条路由计一次。它不等于官方额外COPY字节差：COPY_OUT/IN通常各计一次，原始输入复制、最终写回和spill也会变化。P3 L2与动态DDR/MTE争用均交给正式评估。

**late-copy及等待只是候选排序特征，不是精确关键路径识别。** 当前没有使用完整内存复用边，也没有从保存结果恢复完整编译执行DAG。优先序族主要依赖原图和现有核分配；观察到它获益，不能据此宣称trace归因算法准确。

## 已完成验证

`tests/test_trace_refine.py`的13项测试通过，覆盖1–5核、预算、去重、确定性、输入不变、扇出多消费者、短链、负载迁移与交换、单核priority控制、顺序fallback、旧子图时间线拒绝、非有限时间拒绝、P2/P3同schema与真实保存结果。

独立审查另对384份候选重算核分配变化、交换、静态负载差、路由差与`seed_route_removed`，与metadata一致。其发现的时间线子图映射/顺序及NaN验证缺口已修复，并增加回归测试。

使用`tests/run_trace_refine_probe.py`进行了固定四图P2/N5、每图两轮6+4次的验证。40/40正式调用成功，单worker，评估调用累计墙时21.45秒；上限40次、60秒/次、总评估时间300秒，未触及上限。基线是事先冻结、核哈希的历史成功记录；历史搜索不计入本轮40次，不能作同预算比较。

|图|冻结起点|第一轮|第二轮选中|相对起点降时|额外COPY字节变化|
|---|---:|---:|---:|---:|---:|
|071|9374，operation_smoke_v1|9304|9304|0.75%|362354→362354|
|044|82329，operation_smoke_v1|74711|74588|9.40%|0→8480|
|049|113685，critical_transfer_v1|104839|104464|8.11%|5301580→5303628|
|069|25144，全量simple基线|21113|21113|16.03%|0→0|

这些是四个已探索图的局部验证，不是全100图结果。第一轮主要收益来自priority族；044第二轮接受负载单点迁移，049第二轮接受消费者迁移，071/069第二轮保留前轮incumbent。044/049新增搬运换来更低makespan，采用的目标明确以makespan优先。

所有负结果和预算未评估项已保存。例如049的stable_id priority候选恶化至326974，044的stable_id候选产生4438112 B额外COPY并恶化至154407。必须靠正式评估与incumbent保留防止这些候选被错误接受。

证据目录：`runs/trace_refine_v1/`。`manifest.json`、`source_snapshot/`记录源码及固定来源，`completion.json`确认源码未变；各图保存起点、每轮所有候选/未评估原因/正式记录、选中计划；`attempts.csv`和`summary.json`保存全部40次结果。正式结果压缩文件保存在本轮独立evaluations目录。没有修改solver、旧探索或官方附件。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s A题研究/advanced_solver/tests -p test_trace_refine.py -v

# 会产生新的正式评估调用，需另用空输出目录；不是生成器本身的一部分。
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  A题研究/advanced_solver/tests/run_trace_refine_probe.py \
  --run-dir A题研究/advanced_solver/runs/trace_refine_recheck
```
