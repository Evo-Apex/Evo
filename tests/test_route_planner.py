"""风暴点路线 P1 原型：掩码可通行性、A* 绕行、抽稀不穿障碍、无路/超限、时间单调。

运行：python tests/test_route_planner.py
"""

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))
sys.stdout.reconfigure(encoding="utf-8")

import route_planner  # noqa: E402

MASK = ROOT / "assets" / "maps" / "storm-point-s31-mask.png"


def _px(point, side):
    return round(point[0] * (side - 1)), round(point[1] * (side - 1))


def _inside(mask, points, box):
    x0, y0, x1, y1 = box
    for point in points:
        x, y = _px(point, mask.shape[1])
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def _clear(mask, points):
    for a, b in zip(points, points[1:]):
        ax, ay = _px(a, mask.shape[1])
        bx, by = _px(b, mask.shape[1])
        if not route_planner._visible(mask, ax, ay, bx, by):
            return False
    return True


def rectangle_checks():
    out = {}
    mask = np.ones((120, 120), dtype=bool)
    mask[20:100, 52:60] = False
    box = (52, 20, 59, 99)
    result = route_planner.plan_segment(mask, (0.08, 0.5), (0.92, 0.5))
    points = result["points"]
    out["合成矩形障碍：能绕过去"] = result["status"] == "ok" and len(points) >= 3
    out[f"合成矩形障碍：输出路线不与障碍相交（{result['reason']}）"] = (
        result["status"] == "ok" and not _inside(mask, points, box) and _clear(mask, points))
    out["合成矩形障碍：抽稀后每一段仍看得见"] = _clear(mask, points)
    return out


def u_shape_checks():
    out = {}
    mask = np.ones((120, 120), dtype=bool)
    mask[20:92, 46:51] = False
    mask[20:92, 65:70] = False
    mask[86:92, 46:70] = False
    result = route_planner.plan_segment(mask, (0.48, 0.68), (0.85, 0.5), max_detour=4.0)
    points = result["points"]
    walls = [(46, 20, 51, 92), (65, 20, 70, 92), (46, 86, 70, 92)]
    out[f"U 型障碍：能从上面绕出去（{result['reason']}, {len(points)} 点）"] = result["status"] == "ok"
    out["U 型障碍：绕行和抽稀后都不穿三面墙"] = (
        result["status"] == "ok" and not any(_inside(mask, points, box) for box in walls) and _clear(mask, points))
    return out


def no_path_checks():
    out = {}
    mask = np.ones((100, 100), dtype=bool)
    mask[30:70, 30:34] = False
    mask[30:70, 64:68] = False
    mask[30:34, 30:68] = False
    mask[66:70, 30:68] = False
    result = route_planner.plan_segment(mask, (0.5, 0.5), (0.9, 0.5))
    out[f"被围死：没有路径就返回 uncertain，不输出直线（{result['reason']}）"] = (
        result["status"] == "uncertain" and not result["points"])
    return out


def limit_checks():
    out = {}
    mask = np.ones((200, 200), dtype=bool)
    mask[:, 95:105] = False
    started = time.perf_counter()
    result = route_planner.plan_segment(mask, (0.1, 0.5), (0.9, 0.5), max_nodes=10)
    elapsed = time.perf_counter() - started
    out[f"搜索超限：节点上限一到就安全中止（{result['reason']}, {elapsed * 1000:.1f} ms）"] = (
        result["status"] == "uncertain" and "节点" in result["reason"] and elapsed < 0.5)
    return out


def time_checks():
    out = {}
    mask = np.ones((120, 120), dtype=bool)
    mask[20:100, 52:60] = False
    anchors = [
        {"x": 0.08, "y": 0.5, "elapsed_ms": 0, "confidence": 0.9, "segment_id": 0},
        {"x": 0.92, "y": 0.5, "elapsed_ms": 10000, "confidence": 0.8, "segment_id": 0},
    ]
    legs = route_planner.route_with_mask(anchors, mask)
    points = legs[0]["points"]
    times = [p["elapsed_ms"] for p in points]
    out[f"时间插值：单调递增，两端就是锚点时间（{[times[0], times[-1]]}）"] = (
        times[0] == 0 and times[-1] == 10000 and all(b > a for a, b in zip(times, times[1:])))
    out["时间插值：绕行中间点落在两个锚点时间之间"] = all(0 <= t <= 10000 for t in times)
    return out


def segment_checks():
    out = {}
    mask = np.ones((120, 120), dtype=bool)
    anchors = [
        {"x": 0.1, "y": 0.1, "elapsed_ms": 0, "confidence": 1, "segment_id": 0},
        {"x": 0.2, "y": 0.2, "elapsed_ms": 1000, "confidence": 1, "segment_id": 0},
        {"x": 0.8, "y": 0.8, "elapsed_ms": 5000, "confidence": 1, "segment_id": 1},
        {"x": 0.9, "y": 0.85, "elapsed_ms": 6000, "confidence": 1, "segment_id": 1},
    ]
    legs = route_planner.route_with_mask(anchors, mask)
    out[f"重定位：分成两段，不把两段接成一条（{len(legs)} 段）"] = len(legs) == 2
    out["重定位：每段只含自己的 segment_id"] = (
        all({p["segment_id"] for p in leg["points"]} == {leg["segment_id"]} for leg in legs))
    out["重定位：两段之间没有共用的连接点"] = (
        legs[0]["points"][-1]["elapsed_ms"] < legs[1]["points"][0]["elapsed_ms"])
    return out


def storm_mask_checks():
    out = {}
    mask = route_planner.load_mask(MASK)
    blocked = 1 - mask.mean()
    out[f"风暴点掩码：512×512、硬障碍占 {blocked * 100:.1f}%（不是全封也不是全通）"] = (
        mask.shape == (512, 512) and 0.3 < blocked < 0.75)
    return out


def visibility_checks():
    out = {}
    mask = np.ones((100, 100), dtype=bool)
    mask[:, 60:61] = False
    out["可见性：1 像素细墙也不会被漏掉"] = not route_planner._visible(mask, 10, 50, 90, 50)
    return out


def phase_checks():
    out = {}
    out["对局阶段 P1 一律不跑（赛后/空闲才跑）"] = (
        not route_planner.may_plan("in_match")
        and not route_planner.may_plan("starting")
        and route_planner.may_plan("post_match")
        and route_planner.may_plan("idle"))
    return out


def boundary_checks():
    out = {}
    mask = np.ones((100, 100), dtype=bool)
    # 斜穿一个只有对角相邻的缝：两个正交格都堵住，不能切角。
    mask[49, 50] = False
    mask[50, 49] = False
    out["切角：两个正交格都堵时斜线不可见"] = not route_planner._visible(mask, 49, 49, 51, 51)

    # L 型墙：沿墙绕行，抽稀后也不能从墙角的单侧缝隙斜穿。
    mask = np.ones((100, 100), dtype=bool)
    mask[20:80, 60:62] = False
    mask[78:80, 30:60] = False
    result = route_planner.plan_segment(mask, (0.3, 0.5), (0.8, 0.6), max_detour=4.0)
    out[f"L 型墙：能绕过、抽稀后不切角（{result['reason']}，{len(result['points'])} 点）"] = (
        result["status"] == "ok" and _clear(mask, result["points"]))
    return out


def snap_checks():
    out = {}
    mask = np.ones((60, 60), dtype=bool)
    mask[25:36, 25:36] = False
    inside = {"x": 29 / 59, "y": 29 / 59, "elapsed_ms": 0, "confidence": 0.9, "segment_id": 0}
    after = {"x": 0.9, "y": 0.9, "elapsed_ms": 3000, "confidence": 0.8, "segment_id": 0}
    result = route_planner.plan_segment(mask, (inside["x"], inside["y"]), (after["x"], after["y"]))
    out[f"锚点在障碍里：整对返回 uncertain，不丢原点（{result['reason']}）"] = (
        result["status"] == "uncertain" and result["reason"].startswith("锚点在硬障碍内"))
    legs = route_planner.route_with_mask([inside, after], mask)
    points = legs[0]["points"]
    out["锚点在障碍里：原始锚点保留、只标这一条边不确定"] = (
        len(points) == 2 and points[0]["x"] == inside["x"] and points[0]["y"] == inside["y"]
        and points[1].get("uncertain_before") and legs[0]["uncertain_edges"] == [1])
    return out


def uncertain_edge_checks():
    out = {}
    mask = np.ones((80, 80), dtype=bool)
    mask[30:50, 40:42] = False
    anchors = [
        {"x": 0.1, "y": 0.1, "elapsed_ms": 0, "confidence": 1, "segment_id": 0},
        {"x": 0.2, "y": 0.2, "elapsed_ms": 1000, "confidence": 1, "segment_id": 0},
        {"x": 41 / 79, "y": 0.5, "elapsed_ms": 2000, "confidence": 1, "segment_id": 0},  # 在墙里
        {"x": 0.8, "y": 0.8, "elapsed_ms": 3000, "confidence": 1, "segment_id": 0},
    ]
    leg = route_planner.route_with_mask(anchors, mask)[0]
    out["不确定按相邻锚点对记：前面成功的边不跟着标 uncertain"] = (
        not leg["points"][1].get("uncertain_before") and leg["uncertain_edges"] == [2, 3])
    out["不确定边数等于失败的相邻对，不是整段一个布尔值"] = len(leg["uncertain_edges"]) == 2
    return out


def time_thinning_checks():
    out = {}
    points = [(i / 10, i / 10) for i in range(10)]
    timed = route_planner._route_points(points, 0, 3, 1.0)
    times = [p["elapsed_ms"] for p in timed]
    out[f"时间不够时先抽稀：10 个点放进 3 毫秒（{len(timed)} 点，{times}）"] = (
        len(timed) <= 4 and times[0] == 0 and times[-1] == 3
        and all(a < b for a, b in zip(times, times[1:])))
    out["时间抽稀：中间点不超过终点时间"] = all(0 <= t <= 3 for t in times)
    return out


def mask_semantics_checks():
    out = {}
    import tempfile
    import cv2
    path = Path(tempfile.mkdtemp(prefix="evo-r04-mask-")) / "m.png"
    cv2.imwrite(str(path), np.array([[0, 255]], dtype=np.uint8))
    mask = route_planner.load_mask(path)
    out["掩码语义：255=可走，0=硬障碍（和 JSON、证据图一致）"] = bool(mask[0, 1]) and not mask[0, 0]
    return out


def main():
    checks = {**rectangle_checks(), **u_shape_checks(), **no_path_checks(), **limit_checks(),
              **time_checks(), **segment_checks(), **storm_mask_checks(),
              **visibility_checks(), **phase_checks(), **boundary_checks(), **snap_checks(),
              **uncertain_edge_checks(), **time_thinning_checks(), **mask_semantics_checks()}
    for name, ok in checks.items():
        print(("  ✅ " if ok else "  ❌ ") + name)
    bad = sum(not ok for ok in checks.values())
    print(f"\n{len(checks) - bad} 通过、{bad} 失败、0 跳过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
