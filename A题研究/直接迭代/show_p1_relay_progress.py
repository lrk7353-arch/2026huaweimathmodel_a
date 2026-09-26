"""Read the P1 relay experiment's saved progress without starting any work."""
import argparse
from datetime import datetime
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
FAMILIES = {"legacy_joint": "旧 joint/phase", "region_joint": "新区域重划"}


def read(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def updated(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M:%S")
    except OSError:
        return "尚无记录"


def makespan(record):
    if not record or record.get("status") != "success":
        return "—"
    return record.get("metrics", {}).get("makespan", "—")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=HERE / "P1接力实验_20260926" / "run_v1")
    args = parser.parse_args()
    run = args.run.resolve()
    protocol = read(run / "protocol.json", {})
    manifest = read(run.parent / "inputs" / "manifest.json", {})
    seeds = protocol.get("seed_definitions", manifest.get("seeds", []))
    families = protocol.get("families", list(FAMILIES))
    seed_count = len(seeds) or 6
    arm_count = seed_count * len(families)
    max_calls = arm_count * protocol.get("budget", 6)

    replays = {}
    for result in read(run / "seed_replays.json", []) or []:
        if isinstance(result, dict) and result.get("seed", {}).get("id"):
            replays[result["seed"]["id"]] = result
    for path in (run / "seeds").glob("*/seed.json"):
        result = read(path, {})
        if result.get("seed", {}).get("id"):
            replays[result["seed"]["id"]] = result
    replay_done = sum(bool(result.get("complete")) for result in replays.values())
    replay_verified = sum(bool(result.get("verified")) for result in replays.values())

    states = {}
    statuses = {}
    pending = 0
    for path in (run / "arms").glob("*/*/summary.json"):
        state = read(path)
        if not state:
            continue
        states[(path.parent.parent.name, path.parent.name)] = (state, path)
        pending += int(state.get("pending") is not None)
        for call in state.get("calls", []):
            status = call.get("record", {}).get("status", "未明确")
            statuses[status] = statuses.get(status, 0) + 1
    arms_done = sum(bool(state.get("complete")) for state, _ in states.values())
    calls_recorded = sum(statuses.values())

    print("P1 接力实验进度  " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("目录：" + str(run))
    print("只读磁盘记录；每次打开重新读取，文件暂未更新不能据此判断进程停止。\n")
    if not protocol:
        print("协议尚未开始：等待实验启动。")
    print(f"起点复放：{replay_done}/{seed_count} 已结束，其中 {replay_verified} 份核对通过")
    print(f"完成实验臂：{arms_done}/{arm_count}（结束原因在下方列出）")
    print(f"官方候选调用已落盘：{calls_recorded}/{max_calls}；pending 待回写：{pending}（未计为完成）")
    if statuses:
        print("调用状态：" + "，".join(f"{status}={count}" for status, count in sorted(statuses.items())))
    print("候选调用上限不含 6 次起点复放；时间预算可使实验在用尽上限前结束。\n")
    for seed in seeds:
        seed_id = seed["id"]
        kind = "区域起点" if seed.get("kind") == "region" else "精选起点"
        print(f"{seed.get('case', seed_id)} / {kind}")
        replay = replays.get(seed_id, {})
        if replay.get("complete") and not replay.get("verified"):
            print("  起点复放未通过：" + str(replay.get("error", "原因尚未写入")))
        for family in families:
            state, path = states.get((seed_id, family), ({}, run / "arms" / seed_id / family / "summary.json"))
            if state.get("complete"):
                status = "已结束：" + str(state.get("stop_reason", "未注明原因"))
            elif state.get("pending") is not None:
                status = "候选待回写"
            elif state:
                status = "未结束"
            else:
                status = "等待开始"
            best = makespan(state.get("best_record"))
            if not state and replay.get("verified"):
                best = makespan(replay.get("record"))
            print(f"  {FAMILIES.get(family, family)}：{status}；调用 {len(state.get('calls', []))}/{protocol.get('budget', 6)}")
            print(f"    最好 {best} / 起点 {seed.get('expected_makespan', '—')} / 精选阈值 {seed.get('selected_makespan', '—')}；更新 {updated(path)}")
    if not seeds:
        print("起点清单尚未写入；等待准备完成。")
    print("\n耗时越小越好。尚未开始的实验臂显示已验证起点，不能据此当作新增提升。")


if __name__ == "__main__":
    main()
