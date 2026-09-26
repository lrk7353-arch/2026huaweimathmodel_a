"""Paper figures: all 100 cases, per-case ratios before means; no smoothing."""
from pathlib import Path
import csv,json,statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
R=Path(__file__).resolve().parents[2];O=R/'论文主体版本_v1_20260926';F=O/'图表';F.mkdir(exist_ok=True)
def read(p):return list(csv.DictReader(p.open(encoding='utf-8-sig')))
c=read(O/'数据表/正文_核数曲线.csv');p=read(O/'数据表/附录B_P3同核500组.csv');s=read(O/'数据表/正文_P3同核汇总.csv');cold=read(R/'A题研究/直接迭代/真机启发攻坚_20260926/从头完整1500/从头1500配置成绩.csv')
plt.rcParams.update({'font.size':10,'pdf.fonttype':42,'svg.fonttype':'none','axes.spines.top':False,'axes.spines.right':False})
def save(fig,name):
 for ext in ['png','pdf','svg']:fig.savefig(F/(name+'.'+ext),dpi=220)
 plt.close(fig)
fig,axs=plt.subplots(1,3,figsize=(11.5,3.8),layout='constrained')
for j,ax in enumerate(axs,1):
 q=[r for r in c if int(r['problem'])==j];coldy=[1. if k==1 else statistics.mean(float(r['speedup']) for r in cold if int(r['problem'])==j and int(r['cores'])==k) for k in range(1,6)]
 ax.plot(range(1,6),coldy,'o-',label='Published cold workflow');ax.plot(range(1,6),[float(r['statement_speedup']) for r in q],'s--',label='Cumulative selected')
 ax.set(title=f'P{j}',xlabel='Cores',ylabel='Mean speedup vs original single-core',xticks=range(1,6),ylim=(0,5.5));ax.grid(alpha=.2)
axs[0].legend(fontsize=8);fig.suptitle('100 cases per point; single-core reference = 1; different search histories');save(fig,'图1_成熟从头与累计精选')
fig,axs=plt.subplots(1,2,figsize=(9,3.8),layout='constrained')
for problem in [2,3]:
 q=[r for r in c if int(r['problem'])==problem];axs[0].plot(range(1,6),[float(r['mean_makespan'])/1e6 for r in q],marker='o' if problem==2 else 's',label=f'P{problem}: '+('no L2' if problem==2 else 'read-only L2'))
axs[0].set(xlabel='Cores',ylabel='Mean makespan (million cycles)',xticks=range(1,6));axs[0].legend()
axs[1].plot(range(1,6),[float(r['mean_same_core_speedup']) for r in s],'o-',color='#0072B2');axs[1].axhline(1,color='gray',ls='--');axs[1].set(xlabel='Cores',ylabel='Mean per-case T(P2) / T(P3)',xticks=range(1,6),ylim=(.985,1.055))
fig.suptitle('Separately optimized selected plans: scheduling + cache effects');save(fig,'图2_P3同核对比')
fig,ax=plt.subplots(figsize=(6,4),layout='constrained');q=[r for r in p if int(r['cores'])==5]
ax.scatter([float(r['p3_cache_hit_rate'])*100 for r in q],[float(r['p2_over_p3']) for r in q],alpha=.65,edgecolors='black',linewidths=.3);ax.axhline(1,ls='--',color='gray');ax.set(xlabel='P3 byte hit rate (%)',ylabel='T(P2) / T(P3)',title='100 five-core cases; association, not cache-only causality');save(fig,'图3_缓存命中与同核收益')
(O/'图表/图注与数据来源.md').write_text('''# 图注与统计口径

图1：每点100张图，先算逐图比值再算术平均；单核正式参考点固定1。成熟从头包含已披露的重跑/恢复，累计精选包含多轮不同预算，二者距离不能作为同预算方法净收益。P3此图仅作统一单核参照展示，第三问主结果见图2。

图2：左图为各配置绝对makespan均值；右图为逐图T2/T3均值，不能用左图两条均值线直接相除替代。两场景分别优化，收益混合算法与缓存变化。右图纵轴不是从0开始的柱状图，不用面积编码；保留1参考线。

图3：五核全部100图，不剔除无命中与退步；按字节命中率。此图只说明关联，不能据此推断缓存命中的独立因果收益。独立归因须同方案开关L2。

来源：同目录上级数据表及成熟从头1500配置表；生成脚本工具/build_figures.py。不加平滑，不伪造误差条；100图是给定全集，并非100次随机重复试验。PDF/SVG可放论文，PNG供预览；图号与终稿排版可调整。
''',encoding='utf-8')
print('figures',len(list(F.iterdir())))
