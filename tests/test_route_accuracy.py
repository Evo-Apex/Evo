"""人工真值误差统计：中位数/P90/P95、0.02 内比例、不可见不计 0。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import route_accuracy


def checks():
    out = {}

    def check(name, ok):
        out[name] = bool(ok)

    rows = [
        {"manual_x": 0.1, "manual_y": 0.1, "alg_x": 0.1, "alg_y": 0.1},
        {"manual_x": 0.2, "manual_y": 0.2, "alg_x": 0.21, "alg_y": 0.2},
        {"manual_x": 0.3, "manual_y": 0.3, "alg_x": 0.35, "alg_y": 0.3},
        {"manual_x": 0.4, "manual_y": 0.4, "alg_x": 0.5, "alg_y": 0.4},
        {"manual_x": 0.5, "manual_y": 0.5, "alg_x": None, "alg_y": None},
        {"manual_x": 0.6, "manual_y": 0.6, "alg_x": 0.6, "alg_y": 0.6, "visible": False},
        {"manual_x": 0.7, "manual_y": 0.7, "alg_x": 0.7, "alg_y": 0.7, "indoor": True},
    ]
    s = route_accuracy.summarize(rows)
    check("只统计可见、非室内、有算法坐标的点", s["matched"] == 4)
    check("覆盖率按可见点算", s["coverage"] == 0.6667)
    check("中位数", s["median"] == 0.03)
    check("P90", round(s["p90"], 4) == 0.085)
    check("0.02 内比例", s["within_002"] == 0.5)
    check("误差 >0.08 计数", s["over_008"] == 1)
    check("全部不可见时不除零", route_accuracy.summarize([])["median"] is None)
    check("单个点", route_accuracy.percentile([0.03], 0.9) == 0.03)
    return out


def main():
    out = checks()
    for name, ok in out.items():
        print(("✅ " if ok else "❌ ") + name)
    failed = [k for k, v in out.items() if not v]
    print(f"\n{len(out) - len(failed)} ✅ / {len(failed)} ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
