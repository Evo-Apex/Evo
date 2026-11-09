"""生产和离线回放共用的路线定位/接受/重定位/分段逻辑。

只放纯逻辑，不依赖 OpenCV 或线程：生产由 RouteWorker 调用，录像回放由
tools/replay_route_video.py 调用，避免两份近似流程漂移。
"""
from __future__ import annotations

import math


def sample_interval(cfg, average_ms, pressure=0.0):
    """按当前平均耗时和队列压力算下一次采样间隔（秒）。生产与回放共用。"""
    base, maximum = float(cfg["sample_seconds"]), float(cfg["max_sample_seconds"])
    slow = max(1.0, float(average_ms) / float(cfg["worker"]["target_ms"]))
    return min(maximum, base * max(1.0, 1 + float(pressure) * 3, slow))


class RouteTracker:
    """一条录像/一局、一张地图的接受状态。

    previous/tentative/rejects/segments 的键由调用方决定；生产用 (session, map)，
    回放用 "video"。这样同一个类既能服务多局后台线程，也能服务离线回放。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.previous = {}
        self.tentative = {}
        self.rejects = {}
        self.segments = {}
        self.last_success_ms = {}
        self.misses = {}

    def _limits(self):
        matcher = self.cfg["matcher"]
        return (float(matcher.get("max_gap_seconds", 20.0)),
                int(matcher.get("max_consecutive_misses", 3)))

    def _start_confirmations(self):
        return max(2, int(self.cfg["matcher"].get("start_confirmations", 3)))

    def _drop_previous(self, key):
        """结束当前段：下一个确认点会开新 segment，两段之间没有连接点。"""
        self.previous.pop(key, None)
        self.tentative.pop(key, None)
        self.rejects[key] = 0

    def _remember_tentative(self, key, current):
        """累计一组相近的起点候选；远离或变换异常时从当前帧重新开始。

        起点比普通移动更容易误锁到重复地形，所以默认要求三帧连续一致。候选只留
        ``start_confirmations`` 个，不增加长期内存。
        """
        candidates = self.tentative.get(key)
        if not isinstance(candidates, list):
            candidates = [candidates] if candidates else []
        radius = float(self.cfg["matcher"]["start_confirm_radius"])
        if candidates:
            previous = candidates[-1]
            distance = math.hypot(current["x"] - previous["x"], current["y"] - previous["y"])
            scale_a, scale_b = previous.get("scale"), current.get("scale")
            angle_a, angle_b = previous.get("angle"), current.get("angle")
            scale_ok = scale_a is None or scale_b is None or abs(float(scale_a) - float(scale_b)) <= 0.18
            angle_ok = angle_a is None or angle_b is None or _angle_distance(angle_a, angle_b) <= 35
            if distance > radius or not scale_ok or not angle_ok:
                candidates = []
        candidates.append(current)
        candidates = candidates[-self._start_confirmations():]
        self.tentative[key] = candidates
        return candidates

    def _miss(self, key, elapsed_ms):
        """记一次没定位到；超过时间或连续次数就断段，绝不让旧点等很久再接新点。"""
        max_gap, max_misses = self._limits()
        misses = self.misses.get(key, 0) + 1
        self.misses[key] = misses
        last = self.last_success_ms.get(key)
        too_long = last is not None and (elapsed_ms - last) / 1000 > max_gap
        if self.previous.get(key) is not None and (too_long or misses >= max_misses):
            self._drop_previous(key)
        return None

    def locate(self, matcher, crop, key):
        """局部优先、失败回退全图，和生产 RouteWorker 完全一致。"""
        previous = self.previous.get(key)
        near = (previous["x"], previous["y"]) if previous is not None else None
        return matcher.locate(crop, near)

    def process(self, matcher, crop, key, elapsed_ms):
        """定位 + 接受。返回 (原始匹配结果, 接受后的点或 None)。"""
        result = self.locate(matcher, crop, key)
        if result is None:
            return None, self._miss(key, elapsed_ms)
        return result, self.accept(key, elapsed_ms, result)

    def accept(self, key, elapsed_ms, result):
        """首次位置要连续两次相近；已开始后拒绝不可能的瞬移，连续异常后重新找起点。

        重新找到的起点不是上一段的延续：它开一个新 segment。断点本身没有点，两段之间不连线。"""
        if result is None:
            return self._miss(key, elapsed_ms)
        current = {"elapsed_ms": elapsed_ms, **result}
        previous = self.previous.get(key)
        if previous is None:
            candidates = self._remember_tentative(key, current)
            if len(candidates) >= self._start_confirmations():
                # 每确认一个新起点就换一段：第一次是 0，重定位后是 1、2……
                self.segments[key] = self.segments.get(key, -1) + 1
                current["segment_id"] = self.segments[key]
                self.previous[key] = current
                self.tentative.pop(key, None)
                self.rejects[key] = 0
                self.misses[key] = 0
                self.last_success_ms[key] = elapsed_ms
                return current
            return None
        max_gap, _ = self._limits()
        last = self.last_success_ms.get(key, previous["elapsed_ms"])
        if (elapsed_ms - last) / 1000 > max_gap:
            # 失联太久：先断段，把这个点当新起点候选；下一个相近点才正式开新段。
            self._drop_previous(key)
            self._remember_tentative(key, current)
            return None
        if route_step_ok(previous, current, self.cfg["matcher"]):
            current["segment_id"] = self.segments.get(key, 0)
            self.previous[key] = current
            self.rejects[key] = 0
            self.misses[key] = 0
            self.last_success_ms[key] = elapsed_ms
            return current
        self.rejects[key] = self.rejects.get(key, 0) + 1
        if self.rejects[key] >= int(self.cfg["matcher"]["reset_after_rejects"]):
            self._drop_previous(key)
            self._remember_tentative(key, current)
            self.rejects[key] = 0
        else:
            self.misses[key] = self.misses.get(key, 0) + 1
        return None


def route_step_ok(previous, current, cfg):
    """相邻定位是否可能由正常移动产生；失联超过 gap 直接判不可能。"""
    elapsed = max(0.0, (current["elapsed_ms"] - previous["elapsed_ms"]) / 1000)
    max_gap = float(cfg.get("max_gap_seconds", 20.0))
    if elapsed > max_gap:
        return False
    limit = max(float(cfg["min_step_radius"]), elapsed * float(cfg["max_speed_per_second"]))
    return math.hypot(current["x"] - previous["x"], current["y"] - previous["y"]) <= limit


def _angle_distance(a, b):
    """两个角度的最小差值（度）。"""
    return abs((float(a) - float(b) + 180) % 360 - 180)
