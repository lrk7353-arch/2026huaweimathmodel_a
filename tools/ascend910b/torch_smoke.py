#!/usr/bin/env python3
"""Run one tiny NPU Add correctness check using the installed PyTorch stack.

This is not a custom Ascend C build or a performance benchmark.
Source the platform CANN set_env.sh before running; install nothing here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Reserve an output without overwriting an earlier observation.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as output:
        report = {
            "scope": "installed torch_npu Add correctness only; NOT a performance benchmark",
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "success": False,
        }
        try:
            import torch
            import torch_npu

            report["torch_version"] = torch.__version__
            report["torch_npu_version"] = torch_npu.__version__
            count = torch.npu.device_count()
            report["visible_device_count"] = count
            if count != 1:
                raise RuntimeError(f"Expected the single assigned logical NPU, observed {count}")
            # Management ID 3 is NOT used as a runtime index.
            torch.npu.set_device(0)
            report["logical_device"] = 0
            report["device_name"] = torch.npu.get_device_name(0)
            index = torch.arange(16384, dtype=torch.float32)
            left = (index - 8192) * 0.25
            right = (index.remainder(31) - 15) * 0.5
            expected = left + right
            left_npu = left.to("npu:0")
            right_npu = right.to("npu:0")
            actual_npu = left_npu + right_npu
            torch.npu.synchronize()
            actual = actual_npu.cpu()
            report["elements"] = actual.numel()
            report["dtype"] = str(actual.dtype)
            report["execution_device"] = str(actual_npu.device)
            report["max_absolute_error"] = float((actual - expected).abs().max())
            report["exact_match"] = bool(torch.equal(actual, expected))
            report["all_finite"] = bool(torch.isfinite(actual).all())
            report["success"] = (
                actual_npu.device.type == "npu"
                and report["exact_match"]
                and report["all_finite"]
            )
        except Exception:
            report["error"] = traceback.format_exc()
        json.dump(report, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
