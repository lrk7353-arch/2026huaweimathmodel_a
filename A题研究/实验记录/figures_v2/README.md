# 第二轮结果诊断图

这里只汇总已有真实实验，不修改冻结求解源码，不启动官方评估。用户转向性能优先后，图表按已有成果收尾，未继续做论文模板或额外版式。

- `figure1_all100_portfolio.png/.pdf`：历史全100图 P2/N5 的同轴逐图比较和官方 mean(B/T)。明确保留裸操作级44胜/5平/51负、官方均值略降，以及组合保优后的44胜/56平/0负、均值上升。没有把 mean(Tcontrol/T) 当成官方成绩。
- `figure2_p3_attribution.png/.pdf`：10个含开发用例的机制图 H/S/R；071负策略结果、093策略正例均保留。071/093的细小周期差用数字表，未截轴放大柱长。纯控制的强制候选链与严格保优 incumbent 分开。
- 四个CSV保存全部绘图数据；`captions_and_alt_text.json`给出完整解释及替代文本；`manifest.json`保存输入、脚本、字体SHA256、变换和软件版本。

复现：从项目根目录运行 `PYTHONDONTWRITEBYTECODE=1 E题研究/.venv/bin/python A题研究/实验记录/plot_second_round.py`。

PNG为约300dpi、不透明RGB；PDF文字字体已嵌入，检查未发现图像XObject，图形保留矢量。输出尺寸分别180×126mm、180×184mm，不声称满足某一投稿模板。没有置信区间、合成性能曲线、平滑拟合或数据剔除；未把尚未完成的formal结果画成全量正式曲线。

已逐张打开最终PNG，确认缺字修正、图例/标题不再重叠、图注未裁切。数字另经只读独立复算。颜色同时用形状/纹理/文字区分，蓝、橙、灰对白色的对比分别约5.19、3.87、7.46；这不是完整可访问性认证。元数据检查见 `metadata_qa.json`。

绘图使用 scientific-visualization 技能的证据/刻度/来源审计流程。软件方法参考：Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*. https://doi.org/10.48550/arXiv.2609.00065 。已于2026-09-24核验arXiv当前记录（最近修订2026-09-02）。该引用只记录绘图辅助流程，不是本题算法或数值结论的依据。
