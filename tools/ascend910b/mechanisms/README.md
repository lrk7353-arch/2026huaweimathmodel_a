# 910B3机制实验

在真实NPU执行自定义Ascend C内核，研究题目可借鉴的依赖、粒度和访存规律。硬件数值不用于替换题目config。

已完成7类机制、488个配置批次、14,640次正式测量。完整结果与负例见[实验报告](../../../A题研究/真机机制研究_20260926/实验结论与赛题映射.md)，合法候选设计见[算法交接](../../../A题研究/真机机制研究_20260926/统一算法攻坚交接.md)。离线可在仓库根运行`python3 tools/ascend910b/audit_evidence.py A题研究/真机机制研究_20260926/机制实验`，校验归档哈希、样本、数值标记、统计与核心源码快照。

## 构建和运行

```bash
source /home/developer/Ascend/cann-9.0.0/set_env.sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DASCEND_CANN_PACKAGE_PATH=/home/developer/Ascend/cann-9.0.0 \
  -DSOC_VERSION=Ascend910B3
cmake --build build -j4
python3 run_suite.py --out smoke_01 --suites smoke --samples 3 --warmup 2
python3 run_suite.py --out full_01
python3 analyze.py full_01
```

输出目录要求不存在。只在单张已分配设备、单个测量进程上运行；不安装驱动或改写平台工程。

## 冻结的首轮矩阵

|实验|配置数|控制变量与解释边界|
|---|---:|---|
|pipe|108|2种规模、3种block数、3种tile、单双队列、3种计算强度；每次4个kernel|
|bandwidth|36|固定60MiB单输入，9种block数、2种tile、2种计算强度；block数不等于物理核数|
|barrier|72|独立分支A/B计算轻重互换；全A完成后执行B，对照各分支A完成即可B；kernel数、数学工作与显式GM流量相同|
|reuse|72|多阶段中间量GM落地，对照UB内完成；计算量相同，但launch数与显式GM流量同时改变，不能将全部收益归因于搬运|
|cache|48|相同输入池和访问次数，连续复用一个区域，对照遍历全池后复用；读写地址多重集相同，顺序不同|

共336配置、每配置10次预热、30次计时；每轮随机打乱配置顺序，种子20260926。每次计时后检查全部输出，检查不计入时间。全部输入是精确可表示的二进制分数，CPU参考为`x + rounds*y`。失败立即终止当前批次，保留已写日志。

## 扩展与证据入口

随后增加`--suites sharing`的64配置：相同数值输入与计算量，比较各块读取独立副本和真正共享同一GM地址；输入长度对齐31元素周期，使两布局的完整输出也相同。`mixed_resources.py`另测矩阵/向量串行及并发，可加`--graph`减少逐个Python提交造成的空隙。

- `profile_selected.py`：普通整应用msprof采集，保持原先kernel顺序，不采用逐kernel重放；保留命令、返回码和原始CSV。
- `extract_profiles.py`：从最后一次测量序列提取A/B重叠、kernel间隙及按请求数加权的L2读命中率。
- `confirm_selected.py`：对探索阶段最好、最差配对，在新进程中复测原规模和双倍规模；属于确认性复测，不是未见图泛化。
- `extract_mixed_profiles.py`：确认MatMulV2实际为AI_CORE、Add为AI_VECTOR_CORE，并量化两类kernel时间区间的交集。
- `plot_results.py`与`plot_evidence.py`：生成独立PNG/SVG图。

最初336配置及32配置复测采用提交`c5abcba`的C++测量器。扩展版将完整数值验证改为周期参考填充与逐字节比较，差异时再逐元素诊断；仍比较全部输出，仍在计时外进行。旧二进制保留在远端`build`，扩展版用`build_v2`。各批次记录二进制与源文件哈希，精确源码包保留在结果目录的`source_snapshots`。不将不同测量器版本当作同一配对的两边。

`device_envelope_us`是ACL事件间隔，包含设备等待和可能的host提交空隙，不冒充纯kernel时间；另列host总时间。profiler采集与正常计时分别执行。`programmed_gm_bytes`根据程序的两次读、一次写计数；`logical_GBps`不是实测HBM带宽，L2可能服务这些访问。

缓存组不主动清空L2。每次验证的D2H读取可能影响下一轮状态；参数较小的配置尤其可能受提交开销影响。缓存结论需要独立PMU计数和复测，不能仅凭时间判断。首轮缓存组改变的是整个输入池的复用间隔，尚未隔离跨核共享同一输入的效应。

`pairs.csv`报告配对轮次的时间比和1000次bootstrap中位数置信区间。它是固定设备/配置下的描述，不是跨图泛化证据；探索性多组比较没有做多重检验校正。后续挑出的最大收益需要独立确认。

## 依据

构建和调用采用[官方Ascend C kernel launch接口](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/programug/Ascendcopdevg/atlas_ascendc_10_0056.html)，并以当前安装SDK的头文件及CMake为准。性能计数解释参照[官方msopprof字段说明](https://github.com/Ascend/msopprof/blob/master/docs/en/user_guide/msopprof_performance_data.md)；真实L2的请求命中率不等于题目P3的FIFO缓存字节命中率。
