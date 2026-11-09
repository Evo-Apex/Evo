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

    def locate(self, matcher, crop, key):
        """局部优先、失败回退全图，和生产 RouteWorker 完全一致。"""
        previous = self.previous.get(key)
        near = (previous["x"], previous["y"]) if previous is not None else None
        return matcher.locate(crop, near)

    def process(self, matcher, crop, key, elapsed_ms):
        """定位 + 接受。返回 (原始匹配结果, 接受后的点或 None)。"""
        result = self.locate(matcher, crop, key)
        return result, self.accept(key, elapsed_ms, result)

    def accept(self, key, elapsed_ms, result):
        """首次位置要连续两次相近；已开始后拒绝不可能的瞬移，连续异常后重新找起点。

        重新找到的起点不是上一段的延续：它开一个新 segment。断点本身没有点，两段之间不连线。"""
        if result is None:
            return None
        current = {"elapsed_ms": elapsed_ms, **result}
        previous = self.previous.get(key)
        if previous is None:
            candidate = self.tentative.get(key)
            if candidate is not None:
                distance = math.hypot(current["x"] - candidate["x"], current["y"] - candidate["y"])
                if distance <= float(self.cfg["matcher"]["start_confirm_radius"]):
                    # 每确认一个新起点就换一段：第一次是 0，重定位后是 1、2……
                    self.segments[key] = self.segments.get(key, -1) + 1
                    current["segment_id"] = self.segments[key]
                    self.previous[key] = current
                    self.tentative.pop(key, None)
                    self.rejects[key] = 0
                    return current
            self.tentative[key] = current
            return None
        if route_step_ok(previous, current, self.cfg["matcher"]):
            current["segment_id"] = self.segments.get(key, 0)
            self.previous[key] = current
            self.rejects[key] = 0
            return current
        self.rejects[key] = self.rejects.get(key, 0) + 1
        if self.rejects[key] >= int(self.cfg["matcher"]["reset_after_rejects"]):
            self.previous.pop(key, None)
            self.tentative[key] = current
            self.rejects[key] = 0
        return None


def route_step_ok(previous, current, cfg):
    """相邻定位是否可能由正常移动产生；过滤数秒内横跨地图的误匹配。"""
    elapsed = max(0.0, (current["elapsed_ms"] - previous["elapsed_ms"]) / 1000)
    limit = max(float(cfg["min_step_radius"]), elapsed * float(cfg["max_speed_per_second"]))
    return math.hypot(current["x"] - previous["x"], current["y"] - previous["y"]) <= limit
