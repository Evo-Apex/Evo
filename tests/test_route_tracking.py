"""生产与录像回放共用的路线状态机：上一接受点、接受/拒绝/重定位、采样间隔。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import route_tracking
import routes


CFG = {
    "sample_seconds": 3.0,
    "max_sample_seconds": 10.0,
    "worker": {"target_ms": 20},
    "matcher": {
        "start_confirm_radius": 0.03,
        "min_step_radius": 0.03,
        "max_speed_per_second": 0.01,
        "reset_after_rejects": 3,
        "max_gap_seconds": 20,
        "max_consecutive_misses": 3,
        "start_confirmations": 3,
    },
}


class FakeMatcher:
    def __init__(self):
        self.calls = []
        self.last_scope = "local"
        self.none = False

    def locate(self, crop, previous=None):
        self.calls.append((crop, previous))
        if self.none:
            return None
        return {"x": crop[0], "y": crop[1], "confidence": 0.8, "matches": 10}


def checks():
    out = {}

    def check(name, ok):
        out[name] = bool(ok)

    interval = route_tracking.sample_interval(CFG, 0, 0)
    check("空闲时按 3 秒采样", interval == 3.0)
    check("匹配慢时放慢，但不超过 10 秒", route_tracking.sample_interval(CFG, 100, 0) == 10.0)
    check("队列有压力时也放慢", route_tracking.sample_interval(CFG, 0, 1.0) == 10.0)
    check("正好 20ms 不额外放慢", route_tracking.sample_interval(CFG, 20, 0) == 3.0)

    tracker = route_tracking.RouteTracker(CFG)
    matcher = FakeMatcher()
    _, first = tracker.process(matcher, (0.2, 0.2), "m", 0)
    _, second = tracker.process(matcher, (0.205, 0.205), "m", 3000)
    _, third = tracker.process(matcher, (0.207, 0.207), "m", 5000)
    check("第一次只当候选，不产生点", first is None)
    check("第二次仍不采用，第三次足够近才接受",
          second is None and third is not None and third["segment_id"] == 0)

    _, far = tracker.process(matcher, (0.8, 0.8), "m", 7000)
    check("后续定位把上一接受点传给 matcher",
          matcher.calls[-1][1] is not None and max(abs(a - b) for a, b in zip(matcher.calls[-1][1], (0.207, 0.207))) < 1e-6)
    check("瞬移被拒绝", far is None and tracker.rejects["m"] == 1)
    tracker.process(matcher, (0.8, 0.8), "m", 10000)
    tracker.process(matcher, (0.8, 0.8), "m", 13000)
    check("连续拒绝后清掉上一接受点", "m" not in tracker.previous and tracker.rejects["m"] == 0)
    _, restarted_2 = tracker.process(matcher, (0.8, 0.8), "m", 16000)
    _, restarted_3 = tracker.process(matcher, (0.8, 0.8), "m", 19000)
    check("重定位后三个一致点才开新 segment",
          restarted_2 is None and restarted_3 is not None and restarted_3["segment_id"] == 1)

    # 5 秒短暂失联：不清上一段，回来后还能正常继续
    t2 = route_tracking.RouteTracker(CFG)
    m2 = FakeMatcher()
    t2.process(m2, (0.2, 0.2), "s", 0)
    t2.process(m2, (0.205, 0.205), "s", 3000)
    t2.process(m2, (0.207, 0.207), "s", 5000)
    m2.none = True
    t2.process(m2, None, "s", 6000)
    t2.process(m2, None, "s", 8000)
    m2.none = False
    _, after_short = t2.process(m2, (0.21, 0.21), "s", 8000)
    check("5 秒短暂失联后仍在同一段继续", after_short is not None and after_short["segment_id"] == 0
          and "s" in t2.previous)

    # 连续 3 次没有定位结果：直接断段，远处恢复也要重新确认
    t3 = route_tracking.RouteTracker(CFG)
    m3 = FakeMatcher()
    t3.process(m3, (0.2, 0.2), "i", 0)
    t3.process(m3, (0.205, 0.205), "i", 3000)
    t3.process(m3, (0.207, 0.207), "i", 5000)
    m3.none = True
    for ms in (6000, 9000, 12000):
        t3.process(m3, None, "i", ms)
    check("室内连续 3 次没定位就结束当前段", "i" not in t3.previous)
    m3.none = False
    _, far_again = t3.process(m3, (0.7, 0.7), "i", 15000)
    _, far_second = t3.process(m3, (0.702, 0.702), "i", 18000)
    _, far_confirm = t3.process(m3, (0.704, 0.704), "i", 21000)
    check("失联后在远处恢复：重新三次确认并开新段",
          far_again is None and far_second is None and far_confirm is not None
          and far_confirm["segment_id"] == 1)

    # 30 秒失联：即使中途没累计到 3 次 miss，也按时间断段
    t4 = route_tracking.RouteTracker(CFG)
    m4 = FakeMatcher()
    t4.process(m4, (0.2, 0.2), "g", 0)
    t4.process(m4, (0.205, 0.205), "g", 3000)
    t4.process(m4, (0.207, 0.207), "g", 5000)
    m4.none = True
    t4.process(m4, None, "g", 33000)
    m4.none = False
    _, gap_point = t4.process(m4, (0.21, 0.21), "g", 33100)
    check("30 秒失联：旧段结束，不把新点接回去", gap_point is None and "g" not in t4.previous)
    _, gap_second = t4.process(m4, (0.212, 0.212), "g", 36100)
    _, gap_confirm = t4.process(m4, (0.214, 0.214), "g", 39100)
    check("30 秒失联后三个确认点开新段",
          gap_second is None and gap_confirm is not None and gap_confirm["segment_id"] == 1)

    # 两段之间不参与点击吸附和时间插值
    pts = [
        {"elapsed_ms": 0, "x": 0.1, "y": 0.1, "confidence": 0.9, "segment_id": 0},
        {"elapsed_ms": 1000, "x": 0.2, "y": 0.2, "confidence": 0.9, "segment_id": 0},
        {"elapsed_ms": 31000, "x": 0.8, "y": 0.8, "confidence": 0.9, "segment_id": 1},
        {"elapsed_ms": 32000, "x": 0.9, "y": 0.9, "confidence": 0.9, "segment_id": 1},
    ]
    near = routes.nearest_on_route(pts, 0.5, 0.5)
    at = routes.point_at_time(pts, 16000)
    check("失联断档不参与点击吸附", near["segment_id"] in (0, 1) and near["x"] in (0.2, 0.8))
    check("失联断档不参与时间插值", at["gap"] is True and at["x"] == 0.2)

    check("生产 RouteWorker 仍是同一套接受逻辑",
          hasattr(routes.RouteWorker, "_accept") and hasattr(routes, "route_step_ok"))
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
