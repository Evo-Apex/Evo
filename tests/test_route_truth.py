"""路线真值工具：每帧都有标签，按最近生产锚点合并，室内不污染 eligible 覆盖率。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "code"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import evaluate_route_truth
import route_accuracy


def checks():
    output = {}

    def check(name, value):
        output[name] = bool(value)

    truth = [
        {"map": "测试图", "video_time": "10", "manual_x": "0.10", "manual_y": "0.10",
         "visible": "true", "indoor": "false", "occluded": "false"},
        {"map": "测试图", "video_time": "20", "manual_x": "0.20", "manual_y": "0.20",
         "visible": "true", "indoor": "true", "occluded": "false"},
        {"map": "测试图", "video_time": "30", "manual_x": "", "manual_y": "",
         "visible": "false", "indoor": "false", "occluded": "true"},
        {"map": "测试图", "video_time": "50", "manual_x": "0.50", "manual_y": "0.50",
         "visible": "true", "indoor": "false", "occluded": "false"},
    ]
    replay = {"测试图": {"raw_points": [
        {"elapsed_ms": 10500, "x": 0.11, "y": 0.10, "confidence": 0.8, "segment_id": 0},
        {"elapsed_ms": 20500, "x": 0.20, "y": 0.20, "confidence": 0.8, "segment_id": 0},
    ]}}
    rows = evaluate_route_truth.merge(truth, replay, 3)
    check("最近锚点在容差内会合并", rows[0]["no_result"] is False and rows[0]["alg_time_delta_s"] == 0.5)
    check("超过时间容差标 no_result", rows[3]["no_result"] is True and rows[3]["alg_x"] is None)
    summary = route_accuracy.summarize(rows)
    check("室内和遮挡不进入 eligible 分母", summary["eligible"] == 2)
    check("漏检进入 eligible 覆盖率", summary["eligible_coverage"] == 0.5)
    check("字符串 false 不会被当成真", summary["indoor"] == 1 and summary["occluded"] == 1)
    return output


def main():
    output = checks()
    for name, ok in output.items():
        print(("✅ " if ok else "❌ ") + name)
    failed = [name for name, ok in output.items() if not ok]
    print(f"\n{len(output) - len(failed)} ✅ / {len(failed)} ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
