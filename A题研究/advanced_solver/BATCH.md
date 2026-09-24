# 统一批量入口

`batch.py`负责批量编排；每个slot是一个`case × problem × num_cores`。同一图的slot串行运行，不同图使用ThreadPool并行；每个slot启动独立的`solve.py`子进程。有`--wall-budget`时改为启动`supervisor.py`，由它限制包括解析、生成、评分、检查点写入的整体墙时。

实现时没有新增官方评分调用。11项协议测试通过，测试使用明确标记的fake child产物；另只读验收root已完成的9份P1/P2/P3×1/3/5核smoke摘要、计划和全部成功官方结果哈希，全部通过。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B A题研究/advanced_solver/batch.py \
  --cases 044,069,071 --problems 2 --cores 2,3,4,5 \
  --profile full --seed 17 \
  --component-cap 4 --operation-cap 8 --trace-cap 12 --cache-cap 0 \
  --max-rounds 3 --max-evaluations 24 --timeout 60 \
  --workers 2 --run-dir A题研究/advanced_solver/runs/example_batch
```

`--cases all`指定正式100图；支持`1,044,case_071`。问题可选`1,2,3`，核数可选`1,2,3,4,5`。重复编号去重并保留输入顺序。默认全部图、问题、核数，默认1个图worker；生产批次应显式填写范围与预算。`--config`默认是`--data-dir`中的config.txt。

四个stage cap仅透传，不按profile或核数硬编码预算。`--max-evaluations`是每次搜索的总逻辑调用上限，默认67；四阶段默认6/12/24/24。实际候选池、去重、停止条件可能使真实调用数较少。**同调用上限不等于同实际消耗**，4/12/24次anytime比较应从各slot完整`evaluations`前缀构造，不能用末尾最好分数冒充等消耗结果。

`--timeout`限制单次官方评估；省略时保留solve按图规模选择的默认值。`--wall-budget`限制整个slot进程，包括候选生成；这两个参数不是同一预算。`--evaluation-dir PATH`可共享exact-plan缓存，每个逻辑trial仍然计费；使用已有缓存的墙时不是cold运行墙时。默认没有外部历史incumbent；各profile的共同初始化由engine的component候选决定。

## 文件与状态

每个slot获得独立新目录，例如：

```text
run-dir/
  manifest.json                 # 图/配置/执行依赖/批量脚本/设置冻结
  progress.json                 # 包含未运行slot列表
  summary.json                  # 全部slot最终行，不漏失败
  slots/case_044/p2_n5/
    attempt_0001.launch.json    # 命令、输入签名、返回码、耗时
    attempt_0001.stdout.log
    attempt_0001.stderr.log
    attempt_0001/               # solve创建，调用前不存在
      summary.json
      best.plan.json
      evaluations/...
    slot.json
```

日志和launch文件放在attempt目录外，保证solve收到fresh目录。新attempt不会覆盖旧失败证据。解析全部路径后拒绝slot路径经符号链接逃出批次目录；拒绝写入官方附件目录。

批次报告分别给出`feasible`与`search_completed`。超时但恢复到正式成功best时是`unfinished_feasible`，保留计划，**不称搜索完成**。启动失败、无摘要、损坏摘要等都有独立slot行。`planned_slots`与`reported_slots`应一致，`progress.json`显示尚未报告的slot。

无checkpoint时supervisor可能只给最小失败摘要。这种情况下必须有匹配的supervisor.json，只保留`minimal_failure_no_result`，不认可任何best，评估数量标未知；下一次resume会新开attempt。

## Resume

原命令增加`--resume`，其余设置必须一致（包括workers、缓存路径、warm设置）：

```sh
# 原命令其余参数不变
... --run-dir 原批次目录 --resume
```

首先核对root manifest的全部图SHA、配置SHA、执行依赖SHA、batch.py SHA、Python版本及完整设置。执行依赖表与engine一致，忽略tests、Markdown和runs；batch.py作为额外编排源码单独冻结。修改代码后应创建新批次目录，不可把旧结果冒充新版本续跑。

每个历史attempt都核对其launch签名与当前slot参数；完整摘要还检查graph/config/source、问题/核数/profile/seed/caps、rounds、总调用、超时、initial计划，以及所有成功记录的plan文件和官方gzip结果SHA。官方原始makespan/核数必须与摘要一致，选中计划必须等于其正式评分计划与输出best.plan.json。

只有`completed=true`且有经验证成功best的历史attempt可以直接复用。已验证的失败或unfinished搜索会新增attempt编号；来源/设置不符、记录损坏、孤立未追踪目录等会标`resume_rejected`，不覆盖、不自动绕过。每次重试有自己的预算，旧尝试保留；总消耗应汇总各attempt，不能仅报告最后一次。

## P2热启动P3

可选`--warm-p2`要求本批次同时包含P2和P3。每个case/N先做P2，再将其成功best.plan传给P3；P2超时但具有经验证可行best时也可供P3启动。P3必须重新正式评估该计划，且摘要中必须有且仅有一个匹配计划hash的`initial` trial，**计入P3总逻辑调用**。如果P2没有可行结果，P3独立开始。

默认关闭warm。仅full profile允许直接启用；其他profile还需显式`--exploratory`，所有warm批次标记`exploratory_not_matched_ablation`。这不是与无warm profile公平的同预算消融，不应混在统一方法比较里。

## 检查

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s A题研究/advanced_solver/tests -p test_batch.py -v
```

测试覆盖矩阵完整性、图内顺序、warm同核依赖与收费、fresh目录、已完成复用、损坏gzip/plan拒绝、图/源码/设置变化拒绝、超时可行结果与新attempt、最小supervisor失败、共享缓存与墙时透传、未追踪目录/符号链接拒绝。上述测试不会调用官方评估器。
