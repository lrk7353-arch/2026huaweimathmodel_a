# A题直接迭代入口

Python 3.12标准库，使用原官方评测器。每轮直接保留代码、成绩表、最好方案与官方记录。输出使用新目录，不覆盖旧方案。

最新交付见[第十三轮结果](第十三轮成果/第十三轮结果.md)：1500份方案，相对第十二轮新增7个P1降时配置，P2/P3累计最佳不变，无退步。P1五核全部100图平均配对降时0.6081%；完整改善清单和COPY变化在成果目录。

7项新纪录已独立官方复评一致，1500份方案全部通过合法性与成绩记录一致性检查，70项相关测试通过。本轮实验已结束，旧长队列仍暂停。

P1保持第十二轮integrated算法，在按规模分层预选的12图扩展组上6胜5平1负，相同预算上限平均降时4.70%；比较双方共同实际调用数处仍有4.20%。基线部分候选池提前耗尽，大图有超时，因此不声称所有运行都实际评测12次。可显式使用下列命令，已在不含历史结果的隔离目录复现87145周期：

```sh
python3 -B A题研究/直接迭代/solve.py --case 40 --problem 1 --cores 5 \
  --from-scratch --p1-method integrated --budget 12 --seconds 90 \
  --evaluation-timeout 25 --fresh-evaluations --out my_runs/p1_case40_integrated
```

第十三轮P2/P3新增实验性 `wide_legacy`、`budget_greedy`、`budget_beam`。分别用于检查初始候选范围、按实际收益与耗时分配调用、保留至多3个近优父方案。8图预留验证显示：budget_greedy相对强基线的平均降时为P2 −5.58%、P3 +0.44%，但又逊于同框架wide_legacy；beam也未体现整体优势，且成本更高。因此**不替换trace_routed默认方法**。新策略只用于研究，不建议作为统一生产配置。

已修正一个构造问题：新实验流程初始实际评测仍最多6次，但Component/WCC/Operation候选生成范围按总预算设置；两者不再混为一个限制。负例009还说明小幅改善后频繁重建候选、强制轮换方向会挤掉后续重要候选，需要下一轮独立验证新的刷新策略。完整负例、成本、当前运行父方案来源保留在报告和实验对照中。

历史交付见[第十二轮结果](第十二轮成果/第十二轮结果.md)：相对第十一轮新增7个降时配置（P1 0、P2 4、P3 3）。以下保留各方法的历史对照和运行说明。

第十二轮将局部精修接入从原图开始的统一预算流程 `integrated`。五核预留验证组、每法12次新评测：P1在4图上3胜1平，平均降时16.57%；P2/P3在各6图上分别平均降时0.25%/0.38%，但更耗时。P3与同框架旧邻域 `local_legacy` 六图全部打平。此处是方法比较，**不是累计最好方案的提升**；不据此统一替换默认方法。

下面两个公开入口命令已在不含历史结果的隔离目录实跑，分别得到162275、16243周期，每个12次新评测。所有初始、失败与缓存调用都计入预算，所有阶段共享软时间上限；输出目录必须是新的。

```sh
python3 -B A题研究/直接迭代/solve.py --case 48 --problem 1 --cores 5 \
  --from-scratch --p1-method integrated --budget 12 --seconds 90 \
  --evaluation-timeout 25 --fresh-evaluations --out my_runs/p1_case48_integrated
python3 -B A题研究/直接迭代/solve.py --case 94 --problem 3 --cores 5 \
  --from-scratch --p23-method integrated --budget 12 --seconds 90 \
  --evaluation-timeout 25 --fresh-evaluations --out my_runs/p3_case94_integrated
```

新组合方法目前固定seed17；接口支持1—5核，本轮实际比较仅覆盖五核。迁移源码必须保留 `A题研究/solver`、`advanced_solver`、`精修求解器`、`直接迭代`、`探索` 五个目录以及 `选题分析/A题附件/code` 和 `data` 路径结构；无需携带历史运行目录。隔离复现曾因漏带 `探索/advanced_solve.py` 导入失败，补齐后通过。若继续优化已交付作品，直接传入第十二轮的 `--incumbent-plan`；短预算从头结果不等于累计作品最好结果。

第十一轮的历史结果见[第十一轮结果](第十一轮成果/第十一轮结果.md)：相对第十轮新增30个降时配置，另1个等时减搬运配置。以下保留各模式与历史对照的复现说明。

第十一轮新增 `refine_regions.py --policy data`，支持P2/P3和1—5核，显式读取方案后可在另一台电脑继续，无需历史成绩库。默认策略不变。以下命令从第十轮方案复现本轮P3验证案例：

```sh
python3 -B A题研究/直接迭代/refine_regions.py --case 46 --problem 3 --cores 5 \
  --policy data --incumbent-plan A题研究/直接迭代/第十轮成果/方案/p3/n5/case_046_multicore_res.json \
  --budget 13 --seconds 60 --evaluation-timeout 25 --fresh-evaluations \
  --out my_runs/p3_case46_data
```

13次总调用包含1次起点评测和12次精修，本机全新评测实跑得到62,490周期。最终组合方案通过额外定向搜索和四核方案继承达到58,678周期，不能把该最终值当作上述13次命令的结果。完整复现实验见[第十一轮实验方案](第十一轮实验方案.md)和结果报告。

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

从头模式在短时间预算下可能来不及找到可行结果；普通开发可从已有方案继续。第七轮已在当前入口接入实验性P2/P3从头流程，说明见下方。

从头方法可选 `component`、`adaptive`（默认）、`hybrid`、`portfolio`、`portfolio_diverse`、`integrated`、`local_legacy`。hybrid在同一个总预算中为任务精修预留 `min(4, budget//3)` 次调用，前段额度未用满时也可转给精修。任务邻域穷尽后回到未试的结构候选。结果缓存命中也消耗逻辑调用额度；全部方法都通过官方评测择优，不能用缓存运行秒数比较冷启动速度。

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

## P2/P3从原图开始求解

`--from-scratch --p23-method component_wcc`评测整分量布局和WCC核内交织；`staged`再加入算子分配、轨迹修正和P3缓存精修。第一份方案的官方评测即占第1次额度，全部阶段共享总预算、截止时间和已试方案集合，不读取历史incumbent。使用`--fresh-evaluations`隔离历史结果缓存。

第八轮增加`--p23-method routed`：只根据原图最大分量的管线工作量与理想每核管线工作量之比选路线。多个分量且比值≤1.5时，完整运行Component/WCC；否则运行staged。只执行选中的一条路线，共用原总预算；不会把两法都跑完再挑赢家。该比值不考虑DDR、依赖、COPY和缓存，是启发式而非最终周期预测。routed需要显式指定。

第九轮增加`--p23-method trace_routed`，现作为P2/P3从头模式的默认方法。保留上述原图判断，完成本次Component/WCC初始探测后，读取当前最佳方案的官方时间线。当最大单核M/V忙碌时间占总周期超过90%时，转向staged；否则继续WCC。切换前至多用一次调用检查尚未尝试的交织候选，按内存超容量代理风险、计算结束时间代理排序。初始评测与额外探测均计入同一个总预算。这里的10%余量是工程启发式；它不能证明最优性，代理也不能证明内存可行。P2/P3按各自本次实测轨迹决定。

P2/P3从头模式现在默认12次调用、120秒软预算（旧CLI为8次/180秒）；显式指定参数仍优先。P1与所有warm模式保留8次/180秒，P3 warm默认仍为legacy。新策略经过23张五核图的分组比较，但没有对所有图、所有核数作最优性保证；单独新验证12图和全部负例见第九轮报告。

```sh
python3 -B A题研究/直接迭代/solve.py --case 78 --problem 3 --cores 5 \
  --from-scratch --p23-method trace_routed --fresh-evaluations \
  --budget 12 --seconds 120 --evaluation-timeout 60 --out my_runs/p3_case78_trace_routed
```

```sh
python3 -B A题研究/直接迭代/solve.py --case 95 --problem 3 --cores 5 \
  --from-scratch --p23-method routed --fresh-evaluations \
  --budget 12 --seconds 120 --out my_runs/p3_case95_routed
```

```sh
python3 -B A题研究/直接迭代/solve.py --case 94 --problem 3 --cores 5 \
  --from-scratch --p23-method component_wcc --fresh-evaluations \
  --budget 12 --seconds 120 --evaluation-timeout 60 --out my_runs/p3_case94_component_wcc
python3 -B A题研究/直接迭代/solve.py --case 9 --problem 2 --cores 5 \
  --from-scratch --p23-method staged --fresh-evaluations \
  --budget 12 --seconds 120 --evaluation-timeout 60 --out my_runs/p2_case9_staged
```

094命令可复现16,819周期；改成`--problem 2`对应17,755周期。095的routed命令已通过CLI冷启动复跑，12次全新评测、5.58秒，得到220,188周期。从头管线当前固定seed17，接口支持1—5核，第八轮真实比较覆盖五核，另有三核入口扩展。P3的staged可选`--p3-refinement feedback`，此时单次超时要求60秒。

这是初步统一预算的完整管线，不是累计最好方案库的等价替代。16组新缓存验证全部可行，staged在009/085优于component_wcc，在046/094反而更差。固定阶段配额仍需改进；不能把“阶段更多”当作“结果更好”。WCC始终从本次保留的Component方案生成，避免算子分核打散连通分量后失去适用性。全部无解时如实输出`best_record=null`，不借用历史方案。

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

## 联合推进实验：在接受改进后重新生成候选

基于上游 `18e0957` 的独立补充实验见[审阅与推进结果](联合推进成果_20260925/审阅与推进结果.md)。这些改动尚未进入默认算法或从头 portfolio；下面两个模式都是从给定方案继续精修。

`task_iterative` 复用现有 Task 候选生成器；每次接受官方成绩更好的方案后，读取新方案的官方轨迹，再生成合并、拆分、分核排程候选。整个过程共享一次调用和时间预算，精确重复方案不重评，失败消耗一次调用并保留原可行方案。这里的 `local_duration` 和 `observed_duration` 会重新分配 Task 所属核心，不能仅理解为同核重排。

```sh
python3 -B A题研究/直接迭代/solve.py --case 62 --problem 1 --cores 5 \
  --p1-refinement task_iterative \
  --incumbent-plan A题研究/直接迭代/第六轮成果/方案/p1/n5/case_062_multicore_res.json \
  --budget 8 --seconds 180 --out my_runs/p1_case62_iterative_new
```

本次得到942950→793705→789139周期，包含起点复评共8次新调用。第一步来自队友原有 `local_duration`，新控制流程的额外改善是793705→789139；它用了更多实际调用，不能把整体16.31%降时归功于新流程。case_063同样入口得到252654周期，但没有超过原单轮Task精修。四张定向开发图尚不足以证明普遍优势。

`region_joint` 是局部区域迁移加安全合并的实验入口，包含必要时先拆大Task再迁移的候选；在本次4图对照中没有超过现有Task路线，暂不作为推荐默认值：

```sh
python3 -B A题研究/直接迭代/solve.py --case 100 --problem 1 --cores 5 \
  --p1-refinement region_joint \
  --incumbent-plan A题研究/直接迭代/第六轮成果/方案/p1/n5/case_100_multicore_res.json \
  --budget 2 --seconds 60 --out my_runs/p1_case100_region_new
```

完整定向对照可在仓库根目录运行 `python3 -B A题研究/直接迭代/run_region_panel.py --cases 16,62,63,100 --budget 8 --seconds 180 --workers 2 --out my_runs/region_comparison_new`。每个方法均从相同第六轮方案开始，起点复评计入每个方法的逻辑预算；物理上同图共用一次起点评测，候选评测缓存分别为空。运行时间包含并发干扰，不用来报告单进程加速倍数。

复评交付方案时使用 `solve.py --case 62 --problem 1 --cores 5 --incumbent-plan A题研究/直接迭代/联合推进成果_20260925/方案/case_062_p1_n5.json --budget 1 --seconds 180 --out my_runs/replay_selected62_new`。替换图号和方案文件即可复评其余三份。复评不依赖历史记录中的本机绝对路径。

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

第七轮增加实验模式 `interleave` 和 `feedback`，共享严格相同方案的去重表及剩余调用额度，候选耗尽后自动回流。interleave在两个分支间平衡调用；feedback先分别探测最多4个不同候选，再按接受收益/估计官方评测成本分配。legacy仍保留第二轮搜索；feedback在读序生成起点累计降时达到0.5%时，最多刷新一次读序轨迹。二者都不是完整搜索，也不保证优于旧方法。默认仍为legacy。

P3精修也支持 `--fresh-evaluations`，每次使用新的候选评测目录。下面的13次预算含输入方案复评1次，以及最多12次精修：

```sh
python3 -B A题研究/直接迭代/solve.py --case 46 --problem 3 --cores 5 \
  --p3-refinement feedback --fresh-evaluations \
  --incumbent-plan A题研究/直接迭代/第五轮成果/方案/p3/n5/case_046_multicore_res.json \
  --budget 13 --seconds 120 --out my_runs/p3_case46_feedback_fresh
```

这是从给定方案继续精修，输入计划复评和后续评测均真实运行；不是P3从头生成方案。新控制器每次官方调用最多60秒，总时间仍包含生成和IO的软上限。缓存命中也占逻辑调用额度；完全相同且本轮已试方案才免费跳过。来源评测耗时只作路由启发，不等于本次运行耗时；不同运行负载可能改变feedback路线。若需比较统一上限的五种方法，使用`run_p3_feedback_panel.py`和固定`--before`快照，不能直接拿不断更新的历史最佳方案作为各方法起点。

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
- run_p3_feedback_panel.py：五种P3精修方法的共同起点面板，统一总调用、总时间与单次60秒上限；记录失败及缓存使用。
- summarize_seventh.py：分别汇总回归与结构验证，输出逐图成绩、成对胜负、最差退步及实际运行成本。
- run_p3_portable_panel.py：使用导出方案串行执行CLI，计入初始复评，隔离历史评测缓存并保留完整路线。
- p23_pipeline.py / run_p23_pipeline_panel.py：P2/P3从原图开始、共享预算的实验管线及新缓存对照面板。
- verify_export.py：检查1500份导出方案与成功官方记录逐项一致，验证本轮变化方案的DAG和核心数；`--full-dag`可重查全部方案。
- summarize_eighth.py：按P2/P3分别统计结构路线在开发和验证组的胜负，保留选中分支的一致性检查。
- analyze_wcc_transition.py：对比两份官方时间线的算子分核、M/V重叠、搬运和缓存统计，不将相关变化冒充因果拆解。

进度可双击上级目录“查看最新迭代.command”。本目录从头实验与追加预算方案库分别报告，不把累计最好成绩宣称为统一预算算法成绩。

## 第十轮区域联合精修

`refine_regions.py`支持P1/P2/P3从给定方案继续优化。P1联合重切Task与重新分核；P2/P3迁移或交换相关算子区域并调整顺序；P3额外尝试多个关键读取的联合时序。每次接受改善后重新读取官方时间线，必要时探索少数接近最好的其他结构。旧邻域可用`--policy legacy`对照；P3还可用`region`或`reads`分别运行区域调度及联合读取。

新精修入口省略policy时，P1/P2使用joint，P3使用legacy旧邻域的迭代组合。P3验证组中新联合方法未超过该对照，因此joint/reads保留为显式实验选项；原有solve.py默认值不变。

```sh
python3 -B A题研究/直接迭代/refine_regions.py --case 75 --problem 1 --cores 5 \
  --incumbent-plan A题研究/直接迭代/第九轮成果/方案/p1/n5/case_075_multicore_res.json \
  --policy joint --budget 25 --seconds 180 --fresh-evaluations \
  --out my_runs/p1_075_regions
```

此命令不依赖本机历史运行目录：初始方案占1次复评，其余最多24次搜索。输出`best.plan.json`和`summary.json`；搜索中逐候选更新`search/progress.json`。算法从图结构及本次轨迹生成方案，不根据题号选择策略。候选生成计入软时间上限，正式评测单次默认45秒；最终仅接受官方目标更优方案。

开发和验证由`run_region_panel.py`读取固定输入记录，记录调用曲线、失败和耗时；`summarize_tenth.py`汇总比较。区域搜索是追加预算的精修能力，不将其成绩宣称为从头12次调用可达到，也不预设优于所有旧方法。
