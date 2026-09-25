"""Plot exact original-compute intervals on a shared time axis (Matplotlib optional)."""
import argparse
import csv
import gzip
import json
from pathlib import Path


def main(evidence,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from PIL import Image
    evidence=Path(evidence);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    s=json.loads(evidence.read_text());case=s['case'];core_id=0;rows=[]
    for label in ('before','after'):
        record=s[label]
        plan=json.loads(Path(record['plan']).read_text());ids={int(o) for o in plan['node_to_subgraph']}
        with gzip.open(record['result'],'rt') as f:raw=json.load(f)
        core=next(c for c in raw['per_core_timeline'] if c['core_id']==core_id)
        for op in core['ops']:
            if op['op_id'] in ids and op['pipe'] in ('PIPE_M','PIPE_V'):
                rows.append(dict(plan=label,core=core_id,op_id=op['op_id'],pipe=op['pipe'],start=op['start'],end=op['end']))
    with (out/'case095_timeline_data.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    font_path=Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path));family=font_manager.FontProperties(fname=font_path).get_name()
    else:family='DejaVu Sans'
    with plt.rc_context({'font.family':family,'font.size':11,'pdf.fonttype':42,'svg.fonttype':'path','axes.unicode_minus':False}):
        fig,axes=plt.subplots(2,1,figsize=(10.6,5.1),sharex=True,layout='constrained')
        limit=max(s['before']['makespan'],s['after']['makespan'])/1000*1.045
        for ax,label,title in zip(axes,('before','after'),('优化前','WCC 交织后')):
            for pipe,y,color in [('PIPE_M',1.,'#0072B2'),('PIPE_V',0.,'#D55E00')]:
                intervals=[(r['start']/1000,(r['end']-r['start'])/1000) for r in rows if r['plan']==label and r['pipe']==pipe]
                ax.broken_barh(intervals,(y,.65),facecolors=color,linewidth=0)
            timing=next(c for c in s[label]['core_timing'] if c['core']==core_id)
            ax.set_title(f"{title}  |  总周期 {s[label]['makespan']:,}  |  M/V 重叠 {timing['mv_overlap']:,} 周期",loc='left',fontsize=12,pad=10)
            ax.axvline(s[label]['makespan']/1000,color='#333333',linestyle='--',linewidth=1)
            ax.set_yticks([1.325,.325],labels=['M 管线','V 管线']);ax.set_ylim(-.18,1.9)
            ax.set_xlim(0,limit);ax.grid(axis='x',color='#D9D9D9',linewidth=.65);ax.set_axisbelow(True)
            ax.tick_params(axis='y',length=0)
            for edge in ['top','right','left']:ax.spines[edge].set_visible(False)
        axes[-1].set_xlabel('官方模拟时间（千周期；两图使用相同坐标）')
        fig.suptitle('case_095 · 核 0：相同计算量，更多同时执行',fontsize=16)
        for suffix in ('png','svg'):
            target=out/f'case095_timeline.{suffix}'
            if target.exists():raise ValueError('Use fresh figure paths')
            fig.savefig(target,dpi=240,facecolor='white',transparent=False)
        plt.close(fig)
    with Image.open(out/'case095_timeline.png') as im:
        image_info=dict(pixels=list(im.size),mode=im.mode,dpi=im.info.get('dpi'),opaque=im.mode!='RGBA' or im.getchannel('A').getextrema()==(255,255))
    metadata=dict(source=str(evidence.resolve()),core=core_id,case=case,simulation_runs=2,
        included='All original M/V compute intervals on core 0; COPY intervals not displayed; no smoothing, binning or interpolation.',
        axes='shared linear x-axis from zero; cycles divided by 1000 only for display',
        uncertainty='Deterministic official simulator observations, not a sample estimate; no error bars.',
        audience='Project mechanism verification; no journal formatting claim',
        packages=dict(matplotlib=matplotlib.__version__),image=image_info,
        alt_text='case095核0的原始M/V时间线。优化前两类计算交替且不重叠；WCC交织后出现大量重叠，总周期从420852降至220188。M/V计算量、分核、搬运和缓存统计未改变。')
    (out/'figure_metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(metadata,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();main(a.evidence,a.out)
