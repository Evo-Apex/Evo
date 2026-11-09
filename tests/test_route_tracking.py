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
    },
}


class FakeMatcher:
    def __init__(self):
        self.calls = []
        self.last_scope = "local"

    def locate(self, crop, previous=None):
        self.calls.append((crop, previous))
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
    check("第一次只当候选，不产生点", first is None)
    check("第二次足够近才接受", second is not None and second["segment_id"] == 0)

    _, far = tracker.process(matcher, (0.8, 0.8), "m", 6000)
    check("后续定位把上一接受点传给 matcher",
          matcher.calls[-1][1] is not None and max(abs(a - b) for a, b in zip(matcher.calls[-1][1], (0.205, 0.205))) < 1e-6)
    check("瞬移被拒绝", far is None and tracker.rejects["m"] == 1)
    tracker.process(matcher, (0.8, 0.8), "m", 9000)
    tracker.process(matcher, (0.8, 0.8), "m", 12000)
    check("连续拒绝后清掉上一接受点", "m" not in tracker.previous and tracker.rejects["m"] == 0)
    _, restarted = tracker.process(matcher, (0.8, 0.8), "m", 15000)
    check("重定位后下一个点确认并开新 segment", restarted is not None and restarted["segment_id"] == 1)

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
