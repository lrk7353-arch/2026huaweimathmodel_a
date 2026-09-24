# A题直接迭代入口

Python 3.12标准库，使用原官方评测器。每轮直接保留代码、成绩表、最好方案与官方记录。输出使用新目录，不覆盖旧方案。

## 运行单张图（当前新入口针对五核）

在“中文题目”目录执行，Python不在PATH时换成你自己的Python3.12路径：

```sh
python3 -B A题研究/直接迭代/solve.py --case 44 --problem 2 --out my_runs/p2_case44
python3 -B A题研究/直接迭代/solve.py --case 62 --problem 1 --budget 4 --seconds 90 --out my_runs/p1_case62
python3 -B A题研究/直接迭代/solve.py --case 49 --problem 3 --out my_runs/p3_case49
```

默认读取本机已知最好方案，继续精修；P1/P2/P3分别使用结构选择性切分、轨迹重排、P2方案复用与缓存引导候选。只有官方成功且目标更好的候选才被保留，搜索超时保留已有可行方案。

P1也可从头求解，用于算法比较：

```sh
python3 -B A题研究/直接迭代/solve.py --case 2 --problem 1 --from-scratch --budget 8 --out my_runs/p1_case2_cold
```

从头模式在短时间预算下可能来不及找到可行结果；普通开发优先从已有方案继续。原有1—5核统一求解器仍在“精修求解器/solve.py”。

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

进度可双击上级目录“查看最新迭代.command”。本目录从头实验与追加预算方案库分别报告，不把累计最好成绩宣称为统一预算算法成绩。
