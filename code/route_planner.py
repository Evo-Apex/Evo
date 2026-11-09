"""路线 P1 原型：用一张共享的低分辨率硬障碍掩码，把穿过山体的直线换成绕行折线。

只在赛后/面板空闲时调用；不保存截图，不往数据库加字段。锚点仍是原始定位点，不改观测值：
- 相邻锚点直线没穿障碍：原样保留；
- 穿了：在掩码上做有限制的 A*（节点、耗时、绕行长度都有上限），再按可见性抽稀；
- 找不到可信路径：这一段标 uncertain，调用方画虚线或断开，绝不画直线。
"""

from __future__ import annotations

import heapq
import math
import time

import cv2
import numpy as np

FREE = 128
STEPS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)))


def load_mask(path):
    """读 8 位掩码：>=128 可走，<128 是硬障碍（海、山体、地图外）。"""
    raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        raise ValueError(f"掩码读不出来：{path}")
    return raw >= FREE


def may_plan(phase):
    """P1 只在赛后或空闲跑；对局中、开局、录像回放一律不跑，避免增加对局负担。"""
    return phase in ("post_match", "idle")


def _n(value, size):
    return max(0, min(size - 1, int(round(float(value) * (size - 1)))))


def _visible(mask, ax, ay, bx, by):
    """两点之间画一条像素线，碰到硬障碍就不可见（Bresenham 逐格检查，不漏细墙）。"""
    x, y = int(round(ax)), int(round(ay))
    tx, ty = int(round(bx)), int(round(by))
    if not mask[y, x] or not mask[ty, tx]:
        return False
    dx, dy = abs(tx - x), abs(ty - y)
    sx = 1 if tx > x else -1
    sy = 1 if ty > y else -1
    err = dx - dy
    while (x, y) != (tx, ty):
        ox, oy = x, y
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
        if not mask[y, x]:
            return False
        if x != ox and y != oy and not (mask[oy, x] and mask[y, ox]):
            return False
    return True


def _snap(mask, x, y, radius):
    """锚点落在障碍里时，在很小的半径内找最近的自由像素；找不到返回 None。"""
    h, w = mask.shape
    if mask[y, x]:
        return x, y
    best = None
    for r in range(1, radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and mask[ny, nx]:
                    distance = dx * dx + dy * dy
                    if best is None or distance < best[0]:
                        best = (distance, nx, ny)
        if best is not None:
            return best[1], best[2]
    return None


def snap_anchors(mask, anchors, radius):
    """把仅因粗掩码边缘误差落入障碍的显示锚点，有限地吸附到最近可走像素。

    原始观测仍由调用方单独保存；这里只有赛后显示坐标会移动，且最多 ``radius`` 个掩码像素。
    找不到邻近自由像素的锚点保持原样，后续会被标成不确定并断线。
    """
    h, w = mask.shape
    out = []
    for anchor in anchors:
        item = dict(anchor)
        x, y = _n(item["x"], w), _n(item["y"], h)
        snapped = _snap(mask, x, y, radius)
        if snapped is not None and snapped != (x, y):
            item["snapped_from"] = [item["x"], item["y"]]
            item["x"] = snapped[0] / (w - 1)
            item["y"] = snapped[1] / (h - 1)
        out.append(item)
    return out


def _simplify_visible(mask, path):
    """从起点开始每次跳到最远的可见点；这样抽稀后每一段都仍然看得见，不会重新穿山。"""
    if len(path) <= 2:
        return path
    out = [path[0]]
    i = 0
    while i < len(path) - 1:
        nxt = i + 1
        for j in range(len(path) - 1, i, -1):
            if _visible(mask, *path[i], *path[j]):
                nxt = j
                break
        out.append(path[nxt])
        i = nxt
    return out


def plan_segment(mask, start, goal, *, max_nodes=30000, max_seconds=0.35, max_detour=2.2,
                 snap_px=8):
    """在两个归一化坐标之间规划一段。返回 dict：status=ok/uncertain、points、nodes、seconds、length、reason。"""
    h, w = mask.shape
    sx, sy = _n(start[0], w), _n(start[1], h)
    gx, gy = _n(goal[0], w), _n(goal[1], h)
    blocked_start, blocked_goal = not mask[sy, sx], not mask[gy, gx]
    snapped_start = _snap(mask, sx, sy, snap_px)
    snapped_goal = _snap(mask, gx, gy, snap_px)
    if snapped_start is None or snapped_goal is None:
        return {"status": "uncertain", "points": [], "nodes": 0, "seconds": 0.0, "length": 0.0,
                "reason": "端点附近没有可通行像素"}
    sx, sy = snapped_start
    gx, gy = snapped_goal
    if blocked_start or blocked_goal:
        # 原始锚点在障碍里：观测点保留给调用方，绝不能用确定实线连接吸附点。
        return {"status": "uncertain", "points": [], "nodes": 0, "seconds": 0.0, "length": 0.0,
                "reason": "锚点在硬障碍内（已吸附，不画确定线）",
                "snap_start": snapped_start, "snap_goal": snapped_goal}
    straight = math.hypot(gx - sx, gy - sy)
    if straight <= 1:
        return {"status": "ok", "points": [(sx / (w - 1), sy / (h - 1))], "nodes": 0,
                "seconds": 0.0, "length": 0.0, "reason": "同一点"}
    if _visible(mask, sx, sy, gx, gy):
        return {"status": "ok", "points": [(sx / (w - 1), sy / (h - 1)), (gx / (w - 1), gy / (h - 1))],
                "nodes": 0, "seconds": 0.0, "length": straight, "reason": "直线可见"}

    started = time.perf_counter()
    queue = [(math.hypot(gx - sx, gy - sy), 0.0, sx, sy)]
    cost = {(sx, sy): 0.0}
    parent = {}
    nodes = 0
    found = False
    while queue:
        _, used, x, y = heapq.heappop(queue)
        if used > cost.get((x, y), float("inf")):
            continue
        nodes += 1
        if nodes > max_nodes:
            return {"status": "uncertain", "points": [], "nodes": nodes,
                    "seconds": time.perf_counter() - started, "length": 0.0, "reason": "节点超限"}
        if time.perf_counter() - started > max_seconds:
            return {"status": "uncertain", "points": [], "nodes": nodes,
                    "seconds": time.perf_counter() - started, "length": 0.0, "reason": "耗时超限"}
        if (x, y) == (gx, gy):
            found = True
            break
        for dx, dy, step in STEPS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h) or not mask[ny, nx]:
                continue
            if dx and dy and not (mask[y, nx] and mask[ny, x]):
                continue  # 两个相邻正交格都必须可走，禁止贴着单侧障碍切角
            new_cost = used + step
            if new_cost >= cost.get((nx, ny), float("inf")):
                continue
            cost[(nx, ny)] = new_cost
            parent[(nx, ny)] = (x, y)
            heuristic = math.hypot(gx - nx, gy - ny)
            heapq.heappush(queue, (new_cost + heuristic, new_cost, nx, ny))
    if not found:
        return {"status": "uncertain", "points": [], "nodes": nodes,
                "seconds": time.perf_counter() - started, "length": 0.0, "reason": "没有可行路径"}
    path = [(gx, gy)]
    while path[-1] != (sx, sy):
        path.append(parent[path[-1]])
    path.reverse()
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
    if length > max_detour * straight + max(8, w * 0.01):
        return {"status": "uncertain", "points": [], "nodes": nodes,
                "seconds": time.perf_counter() - started, "length": length, "reason": "绕行太长"}
    path = _simplify_visible(mask, path)
    points = [(x / (w - 1), y / (h - 1)) for x, y in path]
    return {"status": "ok", "points": points, "nodes": nodes,
            "seconds": time.perf_counter() - started, "length": length, "reason": "绕行"}


def _route_points(points, started, ended, confidence):
    """按累计路径长度分配时间，保证时间严格递增；两端用锚点自己的时间。

    可用毫秒数不够容纳所有点时，先按位置均匀抽稀（保留首尾），绝不靠 last+1 把终点往后推。"""
    started, ended = int(started), int(ended)
    if len(points) > 1 and ended <= started:
        ended = started + 1
    if len(points) > 1:
        slots = ended - started + 1
        if len(points) > slots:
            if slots <= 1:
                points = [points[-1]]
            else:
                picked = []
                for i in range(slots):
                    index = round(i * (len(points) - 1) / (slots - 1))
                    if not picked or index != picked[-1]:
                        picked.append(index)
                points = [points[i] for i in picked]
    if len(points) == 1:
        return [{"x": points[0][0], "y": points[0][1], "elapsed_ms": int(started),
                 "confidence": confidence}]
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = lengths[-1] or 1.0
    out = []
    last = started - 1
    for i, point in enumerate(points):
        if i == 0:
            at = started
        elif i == len(points) - 1:
            at = ended
        else:
            at = int(round(started + (ended - started) * lengths[i] / total))
            at = min(max(at, started + 1), ended - 1)
        at = max(at, last + 1)
        last = at
        out.append({"x": point[0], "y": point[1], "elapsed_ms": at, "confidence": confidence})
    return out


def route_with_mask(anchors, mask, *, max_nodes=30000, max_seconds=0.35, max_detour=2.2,
                    snap_px=8, anchor_snap_px=0):
    """把原始锚点变成若干段（legs）。每段 dict：segment_id、points、uncertain、reason。

    只有同一 segment 的相邻锚点才连；重定位后的断点不连。找不到路时 uncertain=True、points 只有两端，
    调用方画虚线；绝不能把它当确定路线。
    """
    anchors = snap_anchors(mask, anchors, anchor_snap_px) if anchor_snap_px else anchors
    legs = []
    if not anchors:
        return legs
    current = {"segment_id": anchors[0].get("segment_id", 0), "points": [dict(anchors[0])],
               "uncertain": False, "uncertain_edges": [], "snaps": [], "reason": ""}
    current["points"][0].setdefault("confidence", 0.0)
    for before, after in zip(anchors, anchors[1:]):
        segment = after.get("segment_id", 0)
        if segment != before.get("segment_id", 0):
            legs.append(current)
            current = {"segment_id": segment, "points": [dict(after)], "uncertain": False,
                       "uncertain_edges": [], "snaps": [], "reason": ""}
            continue
        planned = plan_segment(mask, (before["x"], before["y"]), (after["x"], after["y"]),
                               max_nodes=max_nodes, max_seconds=max_seconds, max_detour=max_detour,
                               snap_px=snap_px)
        if planned["status"] != "ok":
            # 只标记这一对相邻锚点；后面的区间仍可单独规划，不把整段都当成不确定。
            point = {**dict(after), "uncertain_before": True, "uncertain_reason": planned["reason"]}
            current["points"].append(point)
            current["uncertain_edges"].append(len(current["points"]) - 1)
            current["uncertain"] = True
            current["reason"] = planned["reason"]
            if planned.get("snap_start") or planned.get("snap_goal"):
                current["snaps"].append({"to": after["elapsed_ms"],
                                         "start": planned.get("snap_start"),
                                         "goal": planned.get("snap_goal")})
            continue
        confidence = min(float(before.get("confidence", 0)), float(after.get("confidence", 0)))
        timed = _route_points(planned["points"], before["elapsed_ms"], after["elapsed_ms"], confidence)
        current["points"].extend(timed[1:-1])
        current["points"].append({**dict(after), "confidence": after.get("confidence", confidence)})
    legs.append(current)
    for leg in legs:
        leg["points"].sort(key=lambda p: p["elapsed_ms"])
    return legs


def flatten_legs(legs):
    """把规划结果变成数据库/面板能直接消费的点，并把不确定边变成真正断段。

    ``uncertain_before`` 不能只停留在诊断字段里：现有数据库没有这个列，若直接保存会丢失，
    面板随后又会把两个端点画成实线。这里给不确定边后的点换一个新的 ``segment_id``，无需迁移
    数据库也能保证前后永不相连。返回 ``(points, uncertain_edges)``。
    """
    out = []
    display_segment = -1
    uncertain_edges = 0
    for leg in legs:
        display_segment += 1
        for point in leg.get("points", []):
            if point.get("uncertain_before") and out:
                display_segment += 1
                uncertain_edges += 1
            clean = {key: value for key, value in point.items()
                     if key not in ("uncertain_before", "uncertain_reason")}
            clean["segment_id"] = display_segment
            out.append(clean)
    return out, uncertain_edges


def isolate_anchors(anchors):
    """规划器异常时只保留定位点，不用直线把相邻点硬连起来。

    每个点分配独立线段，前端仍能展示时间点和状态，但不会在缺少可靠路径时
    重新制造一条穿墙线。输入点不做原地修改。
    """
    return [{**point, "segment_id": index} for index, point in enumerate(anchors)]
