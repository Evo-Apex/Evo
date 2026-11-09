"""地图配准质量门槛：冲突位置拒绝、几何尺度和内点分布检查。"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import route_matcher


def fake_matcher():
    matcher = route_matcher.MapMatcher.__new__(route_matcher.MapMatcher)
    matcher.cfg = {
        "sample_size": 224, "min_inliers": 4, "min_inlier_ratio": 0.4, "ransac_px": 2,
        "min_scale": 0.75, "max_scale": 1.65, "min_query_spread": 0.012,
        "min_query_quadrants": 2, "local_full_agree_radius": 0.035,
        "local_full_confidence_margin": 0.18, "local_full_ambiguity_ratio": 0.65,
    }
    matcher.image = np.zeros((1536, 1536), np.uint8)
    matcher.last_metrics = {}
    return matcher


def result(x, y, confidence):
    return {"x": x, "y": y, "confidence": confidence}


def checks():
    output = {}

    def check(name, value):
        output[name] = bool(value)

    matcher = fake_matcher()
    chosen, scope = matcher._choose_local_full(result(0.2, 0.2, 0.60), result(0.21, 0.21, 0.55))
    check("相近的 local/full 可以采用", chosen is not None and scope == "local")
    chosen, scope = matcher._choose_local_full(result(0.2, 0.2, 0.60), result(0.8, 0.8, 0.55))
    check("相距很远且强度接近时拒绝", chosen is None and scope == "ambiguous"
          and matcher.last_metrics["reject_reason"] == "local_full_conflict")
    chosen, scope = matcher._choose_local_full(result(0.2, 0.2, 0.90), result(0.8, 0.8, 0.30))
    check("明显更强的假设可以采用", chosen is not None and scope == "local")

    spread = np.float32([[30, 30], [190, 30], [30, 190], [190, 190], [100, 40], [40, 100]])
    transformed = spread * 1.1 + np.float32([300, 400])
    fitted, _ = matcher._fit(spread, transformed, len(spread))
    check("尺度正常且跨象限的解通过", fitted is not None and "reject_reason" not in fitted)
    clustered = np.float32([[100, 100], [101, 100], [100, 101], [101, 101], [102, 101], [101, 102]])
    fitted, _ = matcher._fit(clustered, clustered * 1.1 + np.float32([300, 400]), len(clustered))
    check("内点挤在一小团时拒绝", fitted is not None and fitted.get("reject_reason") == "clustered_inliers")
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
