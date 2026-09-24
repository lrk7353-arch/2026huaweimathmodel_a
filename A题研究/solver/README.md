# A题求解与官方评测工程

目前实现：整计算依赖分量装箱、共享输入感知候选、多起点与有界移动/交换、两个子图粒度、1至N活跃核候选；三个官方场景评估、官方整图单核基准、成功内容缓存、失败日志、断点续跑及汇总。大分量切分等后续模块在独立探索目录研究，尚不属于此基线求解器。

只需Python 3.9以上标准库。实测使用系统3.9.6和随Codex提供的3.12.14；正式一次运行固定Python版本。原始数据、config及官方源码不修改。所有命令从`/Users/liyu/Desktop/中文题目`运行。

## 单图求解

```bash
python3 -B A题研究/solver/solve.py 选题分析/A题附件/data/case_001.json -n 5 -p 2 --method affinity --timeout 300 --run-dir A题研究/solver/runs/example -o A题研究/solver/runs/example/case_001_multicore_res.json
```

输出JSON严格只有`node_to_subgraph`与`core_schedules`。相邻`.solve.json`记录全部已尝试候选与实际官方成绩。候选仅以成功评估后的makespan择优，完全同分才比较新增搬运字节。没有成功方案时返回非零退出码，不生成假成绩。

`--max-evaluations`限制每个求解结果槽尝试的候选数；缓存命中也占一个候选槽。`--budget-seconds`为计入候选生成时间的软截止时间：在发起下一次评估前检查，正在进行的候选生成不被强制中断。`--timeout`是单次官方worker的硬超时。当前没有宣称严格端到端墙钟限时。

## 批量评测与续跑

```bash
python3 -B A题研究/solver/benchmark.py --cases 093,001,011,019,071,008,064,051,049 --cores 1,2,5 --problems 1,2,3 --methods simple,affinity --timeout 300 --workers 3 --run-dir A题研究/solver/runs/pilot_example
python3 -B A题研究/solver/summarize.py A题研究/solver/runs/pilot_example
```

`--cases all`要求原始100图齐全。`--cores 1,2,3,4,5`覆盖所有配置。相同命令/源码/输入可以续跑；更改设置或源码后使用新run目录。单核失败、求解槽无可行方案、P3交叉对照失败都标记不完整并返回非零退出码。

run目录包含：

- `manifest.json`：原图、配置、相关代码哈希，Python版本、参数与声明覆盖范围。
- `source_snapshot/`：本轮实际源码快照，方便后续复查版本。
- `evaluations/attempts/`：每次调用的方案、记录、stdout/stderr和gzip完整官方结果。宿主RSS与模拟L1/UB峰值分别记录。
- `evaluations/cache/`：只索引完整成功结果；加载时核对压缩结果哈希。超时不是永久无效结论。
- `results/`：每图官方单核基准、最终方案、全部候选成绩与P3四格交叉对照。
- `reports/`：逐图CSV、候选状态、P3对照、机器可读汇总和Markdown报告。

整个图级任务串行评估候选，`--workers`控制并行图数；共享DDR带宽由每个官方模拟内部处理，与电脑CPU进程数量不是同一概念。

## 结果解释与当前边界

P1/P2加速比是官方整图单核时间除以多核时间，主曲线1核点按题面定义为1。平均采用逐图比值算术均值；缺失结果时不输出完整分组平均。P3在同核数下保存`T2(π2)`、`T3(π2)`、`T2(π3)`、`T3(π3)`，把固定方案的缓存收益与候选选择收益分开。P3的1核缓存效果实际测量。

`affinity`候选包含`simple`方案；不设候选上限时全部评估。设置上限时可能截断候选池。批量运行两方法时，共享方法还优先继承同配置下已验证的简单基线，防止截断把已知好方案丢掉。报告保留胜/平/负与预算，不能把有限候选组合比较当作最终同墙钟预算实验证据。

简单与共享方法共用成功结果缓存，本次耗时不能用于宣称共享方法冷启动更快；正式求解时间对比应在独立空缓存、相同机器和预算下进行。单种子探索结果也不能替代多种子消融。

整分量基线不会拆分WCC，因此单WCC图只能使用一个活跃核。这是当前方法边界，也是后续大分量切分研究的直接动机。

## 验证

```bash
python3 -B -m unittest discover -s A题研究/solver/tests -v
```

测试覆盖共享输入与计算依赖区分、非法收缩和联合次序环、装箱与输入去重、官方最小图、五个缓存机制案例、失败/超时/文件损坏重算，以及评分聚合和不完整运行状态。
