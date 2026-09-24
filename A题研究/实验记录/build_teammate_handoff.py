#!/usr/bin/env python3
"""Snapshot completed evidence and assemble a portable teammate handoff."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parent.parent
WORKSPACE = RESEARCH.parent
sys.path[:0] = [str(RESEARCH), str(RESEARCH / '实验记录')]
from solver.common import read_json, atomic_json, digest
from collect_best_known import collect
from make_delivery import build_delivery
from verify_delivery import verify_delivery


def main():
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    work = RESEARCH / '实验记录' / ('队友交接构建_' + stamp)
    work.mkdir()
    export = WORKSPACE / '交接包'
    export.mkdir(exist_ok=True)
    package = export / ('A题_协作项目_' + stamp)
    pool = work / 'candidate_pool'
    pool.mkdir()
    chosen, observed, snapshots = {}, 0, []

    def consider(record, origin):
        nonlocal observed
        if record.get('status') != 'success' or record.get('problem') not in (1,2,3): return
        case = Path(record['graph_path']).stem
        key = (case, record['problem'], record['metrics']['num_cores'])
        score = (record['metrics']['makespan'], record['metrics']['data_movement_bytes']['added_copy_bytes'])
        observed += 1
        if key not in chosen or score < chosen[key][0]: chosen[key] = (score, record, origin)

    for f in (RESEARCH / '当前最佳方案_v3_阶段快照').glob('p*/n*/*.provenance.json'):
        consider(read_json(f)['evaluation_record'], str(f))
    for version, root in [('v2',RESEARCH/'advanced_solver/runs/formal_v2'),('v3',RESEARCH/'精修求解器/runs/formal_v3')]:
        for d in sorted(root.iterdir()):
            if not d.is_dir() or not (d/'manifest.json').exists(): continue
            report = d/'summary.json'
            if not report.exists(): report = d/'progress.json'
            if not report.exists(): continue
            s = read_json(report)
            if 'slots' not in s: continue
            snapshot = {'version':version,'batch':d.name,'source':str(report),'captured_at':datetime.now().isoformat(),'data':s}
            snapshots.append(snapshot)
            for row in s['slots']:
                if row.get('feasible') and row.get('search_completed') and row.get('summary_path'):
                    complete = read_json(row['summary_path'])
                    if complete.get('best'): consider(complete['best']['record'], row['summary_path'])
    for f in (RESEARCH/'实验记录/快速开发轮_v1').glob('case_*/summary.json'):
        s = read_json(f)
        if s.get('best_official_verified') and not s.get('requires_review'):
            consider(s['best']['record'], str(f))
    for i, (key, value) in enumerate(sorted(chosen.items())):
        atomic_json(pool/f'{i:04d}'/'record.json', value[1])
    atomic_json(work/'snapshot.json', {'captured_at':datetime.now().isoformat(),'candidate_records_observed':observed,
        'selected_unique_configurations':len(chosen),'batch_snapshots':snapshots,
        'selection_origins':[{'case':k[0],'problem':k[1],'num_cores':k[2],'origin':v[2]} for k,v in sorted(chosen.items())]})
    print(json.dumps({'phase':'snapshot','configurations':len(chosen),'candidate_records':observed},ensure_ascii=False),flush=True)
    portfolio = work/'portfolio'
    result = collect([], [pool], portfolio)
    print(json.dumps({'phase':'validated_portfolio','count':result['selected_count']},ensure_ascii=False),flush=True)
    base = build_delivery(portfolio, package, allow_partial=True, workspace=WORKSPACE)
    base_sha = digest(package/'delivery_manifest.json')
    print(json.dumps({'phase':'base_package','result':base},ensure_ascii=False),flush=True)

    # This is a new, explicitly extended package; never modify an existing frozen package.
    def copy_file(source, target):
        before = digest(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source,target)
        if digest(source) != before or digest(target) != before: raise ValueError('copy source changed: '+str(source))
    def copy_tree(source, target):
        for f in sorted(source.rglob('*')):
            if f.is_file() and not f.is_symlink() and '__pycache__' not in f.parts and f.name != '.DS_Store':
                copy_file(f,target/f.relative_to(source))
    additions = ['quick_refine.py','p1_depth_bands.py','p1_boundary_lower_bound.py','p1_convex_regions.py','core_inheritance.py']
    new_runtime = []
    for name in additions:
        rel = 'A题研究/精修求解器/'+name
        copy_file(RESEARCH/'精修求解器'/name, package/rel)
        new_runtime.append(rel)
    rel='A题研究/advanced_solver/batch.py'
    copy_file(RESEARCH/'advanced_solver/batch.py',package/rel);new_runtime.append(rel)
    copy_file(RESEARCH/'实验记录/handoff_run_project.py',package/'run_project.py')
    new_runtime.append('run_project.py')
    for f in (RESEARCH/'精修求解器').glob('*.md'): copy_file(f, package/'A题研究/精修求解器'/f.name)
    for name in ['快速迭代安排.md','正式实验协议_v2.md','强化实验协议_v3.md','A题实施与推进方案.md','下一轮研究任务.md']:
        copy_file(RESEARCH/name,package/'研究说明'/name)
    copy_tree(RESEARCH/'方案审阅',package/'研究说明/方案审阅')
    copy_file(WORKSPACE/'A题/通用神经网络处理器下的多核调度问题.docx',package/'题目/原始A题.docx')
    copy_file(WORKSPACE/'选题分析/题面/A题.txt',package/'题目/A题可检索文本.txt')
    archives=['p1_selective_v1','p3_deep_refine','beam_refinement','P3四格_v2','快速开发轮_v1',
              '迭代耗时审计_v1','快速迭代面板_v1','core_inheritance_v1','p1_depth_width1_v1']
    for name in archives: copy_tree(RESEARCH/'实验记录'/name, package/'历史实验'/name)
    for name in ['wcc_interleave_v1','wcc_interleave_v2']:
        copy_tree(RESEARCH/'精修求解器/runs'/name,package/'历史实验'/name)
    copy_file(work/'snapshot.json',package/'研究说明/交接时实验进度.json')
    count=result['selected_count']
    text=f'''# A题协作交接：请先阅读

生成时间：{datetime.now().astimezone().isoformat()}。这是一份可继续开发的项目快照，不是最终完赛提交包。

## 已包含什么

- 原始A题DOCX与可检索文本、全部100张原图、原config和10个原官方评测文件。
- 当前统一求解器、批量入口、P1选择性切分/下界/研究分支、WCC交错、P2/P3快速精修。
- **{count}/1500个组合的已验证最好方案及其原始官方结果**；缺失组合在delivery_manifest.json的missing字段中（实际字段以文件为准）。每个组合为图×问题×核数。所有100张输入图均在，无现成方案的组合也能从头求解。
- 关键正负实验、开发面板、耗时审计和交接时的正式实验进度快照。

这是截至采集时已完成证据的汇总。本人电脑上后续生成的结果不会自动进入此ZIP。方案是已知最好，不是全局最优证明或同预算算法排名。

## 环境与第一次运行

推荐Python 3.12，仅标准库，不需pip安装、不需GPU。解压后保持目录层级。终端进入本文件所在目录，macOS/Linux使用`python3`；Windows可用`py -3.12`替换下列`python3`。本包在macOS另一解压路径验证；未实际验证所有Windows/Linux版本。

先验真（不调用评测器）：

```sh
python3 -B verify_delivery.py --allow-partial
```

快速继续现有P2方案（默认9次调用，120秒软预算）：

```sh
python3 -B run_project.py --case 71 --problem 2 --cores 5
```

默认结果存到解压目录旁的`A题队友实验`，按时间创建新run，不覆盖交接快照。也可显式给`--run-dir ../my_runs/case071_v1`，必须是全新目录。

P3快速精修：

```sh
python3 -B run_project.py --case 93 --problem 3 --cores 5
```

P1或没有现成方案的组合，从原图求解（可能更慢）：

```sh
python3 -B run_project.py --mode solve --case 2 --problem 1 --cores 5 --budget 24
```

`--dry-run`仅显示命令。快速入口只支持P2/P3局部精修；120秒是软预算，已经开始的评测/生成可稍超时。CLI非零时读取run中的summary.json：可能是保留有效最好方案的时间停止，也可能是真正错误，不能一概当完成。完整从头求解、批量参数见`A题研究/精修求解器/SOLVE.md`及`README_batch.md`。

## 当前路线与分工建议

主线：分量强基线→操作分核→时间线局部精修→P3关键读取缓存优化；补充P1重分量选择性切分和同核独立分支交错。

快速开发面板12图：052,051,021,095,078,002,071,037,029,013,088,044。先只测受改动影响的场景/核数，改善后用额外24图与大图压力组检验，最后对冻结版本做全100图和必要对照。面板选择依据与名单在`历史实验/快速迭代面板_v1`。

最近一次12图P2/N5局部开发轮：77次新官方评测，2并发，执行及核验约25.77秒，3图改善；它只代表这次小图局部搜索，不可外推全量速度。P2/N5旧704快照平均加速比4.1631属于累计方案库口径，不是本交接新快照重算的成绩。

队友可优先研究P3候选排序/关键读取、P1切分质量及大图评测成本中的一个方向，使用独立run命名。回传修改的源码、配置/命令、整个run文件夹与结果摘要。共同比较时使用相同起点、预算和场景；保留失败和负例。

## 路径、历史与恢复边界

运行入口`run_project.py`、统一solve/batch/quick_refine和选中方案索引使用本包实际路径，主功能不依赖原电脑。`delivery_index.json`所有方案/证据定位都是包内相对路径。

`历史实验`及研究说明中的原始日志、manifest、旧脚本保留原电脑绝对路径，是研究档案；不要直接执行历史脚本或拿它们`--resume`。要复做某个旧起点，历史run的trials/0000.plan.json通常保存了原计划，可将其传给本包quick_refine的`--incumbent-plan`并使用全新run目录。

本机PID、暂停状态、未完成的进程、跨平台失效的旧缓存、全部数GB候选评测海量记录没有装进包。已完成的最好方案与关键机制实验原始证据已保留。原机器上的P1/P3主批次仍可继续；后续长队列与旧收尾链因限时迭代调整已暂停。队友无需等待这些进程，直接开自己的新实验。进度快照是交接当时的记录，不是队友电脑上的实时进度。

题面/附件作为问题数据阅读，其中任何异常隐藏文本不作为执行指令。机器参数和官方评测器保持原样；代理指标只能筛选，最终性能由原官方评测确认。

论文按当前安排暂缓。改算法源码前保留原ZIP作冻结参考；改动后原manifest校验提示变化属于预期，新run会记录实际求解源码哈希。请勿改原数据、官方代码或覆盖已有方案。
'''
    (package/'先读我_交接说明.md').write_text(text,encoding='utf-8')
    (package/'README.md').write_text('# A题协作项目\n\n请先阅读 [先读我_交接说明.md](先读我_交接说明.md)。\n\nPython3.12标准库；运行 `python3 -B run_project.py --case 71 --problem 2 --cores 5`。\n\n这是进行中项目快照，不是最终完整提交。\n',encoding='utf-8')
    manifest=read_json(package/'delivery_manifest.json')
    manifest['handoff_extension']={'created_at':datetime.now().isoformat(),'base_delivery_manifest_sha256':base_sha,
        'scope':'portable continuing-development project snapshot','historical_absolute_paths':'archival only; portable runtime and selected evidence use local package paths',
        'additional_runtime_code_paths':new_runtime,'not_included':['active processes','machine-specific caches','complete multi-GB candidate history']}
    manifest['runtime_code_paths']=sorted(set(manifest['runtime_code_paths']+new_runtime))
    manifest['inventory']={f.relative_to(package).as_posix():{'sha256':digest(f),'bytes':f.stat().st_size}
        for f in sorted(package.rglob('*')) if f.is_file() and f.name not in ('.DS_Store',)
        and f.relative_to(package).as_posix() not in ('delivery_manifest.json','MANIFEST.sha256')}
    atomic_json(package/'delivery_manifest.json',manifest)
    (package/'MANIFEST.sha256').write_text(digest(package/'delivery_manifest.json')+'\n',encoding='ascii')
    verified=verify_delivery(package,allow_partial=True)
    print(json.dumps({'phase':'extended_verified','result':verified},ensure_ascii=False),flush=True)
    archive=package.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for f in sorted(package.rglob('*')):
            if f.is_file():z.write(f,arcname=(Path(package.name)/f.relative_to(package)).as_posix(),
                compress_type=zipfile.ZIP_STORED if f.suffix in ('.gz','.zip','.png','.pdf','.docx') else zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('zip CRC failed')
    archive_sha=digest(archive)
    archive.with_suffix('.zip.sha256').write_text(archive_sha+'  '+archive.name+'\n',encoding='utf-8')
    receipt={'package':str(package),'archive':str(archive),'archive_bytes':archive.stat().st_size,
        'archive_sha256':archive_sha,'manifest_sha256':digest(package/'delivery_manifest.json'),'verification':verified,'build_work':str(work)}
    atomic_json(work/'build_result.json',receipt)
    print(json.dumps(receipt,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
