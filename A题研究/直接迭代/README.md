# A题直接迭代入口

Python 3.12标准库，使用原官方评测器。每轮直接保留代码、成绩表、最好方案与官方记录。输出使用新目录，不覆盖旧方案。

最新交付见[第六轮结果](第六轮成果/第六轮结果.md)：1500份方案、全部成绩、同调用上限比较、P3缓存归因、完整冷启动计时及负例。本轮实验已结束，旧长队列保持暂停。

## 运行单张图（支持1—5核，默认五核）

在“中文题目”目录执行，Python不在PATH时换成你自己的Python3.12路径：

```sh
python3 -B A题研究/直接迭代/solve.py --case 44 --problem 2 --out my_runs/p2_case44
python3 -B A题研究/直接迭代/solve.py --case 62 --problem 1 --budget 4 --seconds 90 --out my_runs/p1_case62
python3 -B A题研究/直接迭代/solve.py --case 49 --problem 3 --out my_runs/p3_case49
python3 -B A题研究/直接迭代/solve.py --case 2 --problem 1 --cores 3 --out my_runs/p1_case2_n3
```

默认读取本机已知最好方案，继续精修；P1/P2/P3分别使用结构选择性切分、轨迹重排、P2方案复用与缓存引导候选。只有官方成功且目标更好的候选才被保留，搜索超时保留已有可行方案。

P1也可从头求解，用于算法比较：

```sh
python3 -B A题研究/直接迭代/solve.py --case 2 --problem 1 --from-scratch --budget 8 --out my_runs/p1_case2_cold
python3 -B A题研究/直接迭代/solve.py --case 71 --problem 1 --from-scratch \
  --p1-method hybrid --seed 42 --budget 12 --seconds 180 --out my_runs/p1_case71_hybrid
```

从头模式在短时间预算下可能来不及找到可行结果；普通开发优先从已有方案继续。原有1—5核统一求解器仍在“精修求解器/solve.py”。

从头方法可选 `component`、`adaptive`（默认）、`hybrid`、`portfolio`、`portfolio_diverse`。hybrid在同一个总预算中为任务精修预留 `min(4, budget//3)` 次调用，前段额度未用满时也可转给精修。任务邻域穷尽后回到未试的结构候选。结果缓存命中也消耗逻辑调用额度；全部方法都通过官方评测择优，不能用缓存运行秒数比较冷启动速度。

第五轮建议显式使用 `portfolio_diverse`，将结构切分、张量区域和任务精修纳入同一个预算：

```sh
python3 -B A题研究/直接迭代/solve.py --case 24 --problem 1 --from-scratch \
  --p1-method portfolio_diverse --budget 12 --seconds 180 --evaluation-timeout 60 \
  --out my_runs/p1_case24_portfolio
```

12次总调用中，前段至多8次用于整分量与结构候选，再尝试至多2个通过代理门槛的张量候选，将余额用于任务精修或更多结构候选。仅有一个Task时跳过任务精修；任务很多且很小时优先尝试8/16上限合并。重分量图按切分方法和粒度轮换；超过6000个计算算子的大图，先尝试最大Task较小的基线，以争取在限时内得到可行方案。这些规则由图特征决定，不按图号选择。

张量代理小于当前官方周期的85%只是候选筛选启发式，不是安全下界；必要Task与DDR下界另取最大值检查。`--evaluation-timeout`控制从头模式的单次评测上限，总搜索还受`--seconds`约束。默认方法仍为adaptive；新方法在24图回归中对hybrid有4胜19平1负，尚不能认为全面占优。第五轮大图比较复用成功缓存；第六轮另补测了下方3张图的完整冷启动。

第六轮新增 `--fresh-evaluations`：从头模式的评测写入本次新输出目录，不复用历史结果缓存。用于真实冷启动计时，也可在新电脑运行：

```sh
python3 -B A题研究/直接迭代/solve.py --case 43 --problem 1 --from-scratch \
  --p1-method portfolio_diverse --budget 12 --seconds 180 --evaluation-timeout 60 \
  --fresh-evaluations --out my_runs/p1_case43_fresh
```

本机043/075/085已完整实跑，各12次新评测、无超时，分别约48/36/142秒，不能当作所有图的运行时保证。

换电脑后，可以直接用导出的方案继续，不依赖原电脑的历史实验目录：

```sh
python3 -B A题研究/直接迭代/solve.py --case 71 --problem 3 --cores 2 \
  --incumbent-plan A题研究/直接迭代/第二轮成果/方案/p3/n2/case_071_multicore_res.json \
  --budget 4 --seconds 60 --out my_runs/p3_case71_n2
```

显式 `--incumbent-plan` 模式只使用该方案，先在目标场景/核数下官方复评，计入调用和时间预算，再精修。P3此模式跳过历史P2方案复用，继续调度控制和缓存候选。预算仅1时只复评并输出该方案。输出目录内 `best.plan.json` 是最终方案，`summary.json` 是记录；常规历史模式与从头模式也保留各自原有输出。

P1 新增任务粒度与实测时间排程精修，适合继续改善已有方案：

```sh
python3 -B A题研究/直接迭代/solve.py --case 62 --problem 1 --cores 5 \
  --p1-refinement tasks \
  --incumbent-plan A题研究/直接迭代/第三轮成果/方案/p1/n5/case_062_multicore_res.json \
  --budget 7 --seconds 150 --out my_runs/p1_case62_tasks
```

`--p1-refinement tasks` 尝试同核相邻任务合并、大任务拆分、按官方局部耗时或实际持续时间重排。合并保持算子所属核心，并在包含数据依赖和核心顺序的拓扑序上分组，防止合并后形成环。必要 Task 下界与必要 DDR 下界取最大值，仅剪去下界已超过当前成绩的候选；代理排程不直接当作成绩。默认 `partition` 仍是原结构切分精修，两个模式分别使用给定的调用与时间预算。

任务精修的“合并上限”仅限制新合并任务；原方案中超过上限的大任务会单独保留，不会在合并阶段自动拆分。拆分由独立候选处理。全部候选生成自同一个输入方案，本次搜索内不递归扩展新优解。

## 按张量边界切分，再合并小任务

第四轮新增 `tensor` 精修：保留大张量计算链，合并相连低成本计算，对商图作强连通分量收缩防止环，再分配核心。候选的必要Task/DDR下界用于剪枝，官方实测才决定是否保留。第五轮已将其加入portfolio及portfolio_diverse的统一预算流程；hybrid本身仍保持原有方法。

下面两步可在另一台电脑从第三轮方案复现case_016的突破，不需要历史评测目录：

```sh
python3 -B A题研究/直接迭代/solve.py --case 16 --problem 1 --cores 5 \
  --p1-refinement tensor \
  --incumbent-plan A题研究/直接迭代/第三轮成果/方案/p1/n5/case_016_multicore_res.json \
  --budget 2 --seconds 180 --out my_runs/p1_case16_tensor
python3 -B A题研究/直接迭代/solve.py --case 16 --problem 1 --cores 5 \
  --p1-refinement tasks --task-merge-caps 8,16 \
  --incumbent-plan my_runs/p1_case16_tensor/best.plan.json \
  --budget 3 --seconds 180 --out my_runs/p1_case16_merge
```

已实跑得到4,026,098→3,304,638周期。每步预算都包含输入方案的官方复评，第一步额外尝试1个张量区域候选，第二步额外尝试2个合并候选。慢电脑可增加秒数；这不改变官方硬件配置。`--task-merge-caps` 仅用于 `tasks` 模式，指定后本次只尝试这些合并上限。

## P3关键读取顺序与联合精修

`--p3-refinement read_order`保持算子分核，先生成观测顺序的单算子子图控制，再保持该控制的精确mapping，仅调整合法核心顺序。候选包括提前关键消费者、提前已有首读者、前置现有独立工作以错开重复读取；不添加等待或预取。`joint`先用至多4次调用尝试旧邻域，再将总预算余额用于读序精修。旧方法`legacy`仍是默认值。

```sh
python3 -B A题研究/直接迭代/solve.py --case 94 --problem 3 --cores 5 \
  --p3-refinement joint \
  --incumbent-plan A题研究/直接迭代/第五轮成果/方案/p3/n5/case_094_multicore_res.json \
  --budget 13 --seconds 120 --out my_runs/p3_case94_joint
```

显式输入方案先占1次官方复评，所以上例留下12次用于搜索；从第五轮094方案可得到20,156周期。换成`read_order`可复现20,308周期。支持1—5核，只有官方更优候选才替换输入。联合方法18图回归对旧方法为7胜7平4负，没有普遍最优保证。默认legacy可复用历史P2方案；新读序及联合模式不自动加入P2来源，便于保持调用预算和起点一致。

命中率仅用于诊断。`run_p3_attribution.py`把原始、编码控制、读序候选分别交给P2/P3，区分排程收益和缓存交互收益；选择候选时已经使用P3分数，报告明确标注这一选择过程。

## 主要改动

- 跨核继承：低核好方案补空核后，在目标五核配置中官方复评。
- P1：轻分量结构保留EFT粗切分，夹入整分量排序候选；重分量结构采用局部工作量切分/时间带切分。只用必要Task下界排除不可能更优的候选。代理时间仅供排序。
- P3：先评估P2计划，再将固定分配的子图/顺序重组与缓存引导修改分开记录；缓存动作轮流尝试不同机制。
- 计时：搜索在候选边界检查总预算，单次官方评测有剩余时间上限；候选生成/IO使总时间可能小幅超出。

## 批次与读结果

“运行结果”内每批有results.csv和summary.json。开发组、扩展组、压力组、追加精修分开保存；缓存命中不冒充新评测。历史记录复用后的秒数不能当作冷启动提速倍数。

- run_inheritance.py：跨核继承。
- run_p1_panel.py：P1固定调用上限比较。
- run_p1_warm.py：保留已有方案的P1追加精修。
- run_p3_refine.py：P3追加精修。
- run_component_n5.py：补P2五核强分量对照。
- run_four_cells_n5.py：P2/P3固定方案五核交叉评测。
- export_results.py：汇总当前最好方案与改善清单。
- run_transfer.py：全核继承、P2/P3方案互用、缺失单核补齐；单核 `--single-layout bounded` 控制任务大小，所有结果仍由官方评测。
- run_refine_panel.py：多个核数的短预算精修，先跑小图；`--exclude-completed` 可保留上一批已返回配置，剩余配置单独继续。
- export_round.py：相对指定轮次开始记录导出方案、改善和补齐清单。
- run_task_panel.py：P1 任务粒度与实测时间排程的小批量精修，逐配置设调用/时间上限。
- run_quality_panel.py：16 图、3 方法、3 种子的同调用上限比较，无历史方案热启动；使用共享结果缓存。
- analyze_quality_panel.py：按图取三种子中位数比较，同时记录不同种子获胜方案的实际差异。
- run_hybrid_panel.py：12次总调用上限的component/adaptive/hybrid比较，开发16图与扩展8图分别报告。
- run_tensor_panel.py：张量区域分区的定向追加精修，保留所有负例及下界剪枝；不是等预算方法排名。
- summarize_fourth.py：整理第四轮同预算对照、大图机制、候选与官方独立复评记录。
- run_portfolio_panel.py：从头比较adaptive/hybrid/portfolio/portfolio_diverse，逐图限制总调用、总时间和单次评测时间。
- run_portfolio_grid.py：在指定图和多个核数上运行统一预算求解流程。
- combine_portfolio_panels.py：按完整方法组汇总已完成批次，不跨运行挑选最好种子。
- summarize_fifth.py：导出第五轮回归比较、真实运行成本、候选路线及独立复评证据。
- diagnose_current_p3.py：读取指定方案库的完整P3时间线，验证缓存事件并筛选读瓶颈，不新跑评测。
- run_p3_read_panel.py：以指定共同起点比较legacy/read_order/joint，逐图限制调用和时间。
- run_p3_attribution.py：原始/控制/读序方案的P2/P3交叉评测，保存缓存交互与排程贡献。
- run_cold_panel.py：每图使用空评测目录，记录完整从头搜索耗时。
- replay_selected.py：选定当前最好方案，在新目录作独立官方复评。
- summarize_sixth.py：整理第六轮方法对照、缓存归因、大图扩展、冷启动及运行成本。

进度可双击上级目录“查看最新迭代.command”。本目录从头实验与追加预算方案库分别报告，不把累计最好成绩宣称为统一预算算法成绩。
