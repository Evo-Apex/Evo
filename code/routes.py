"""对局行进路线：低频裁小地图，后台配准到预置全地图，最终只保存带时间戳的矢量折线。

对局中的采集线程只做三件很便宜的事：按配置裁小地图、复制小块、放进有上限的队列。
特征提取和配准在一个低优先级后台线程里持续做；队列变长或单张变慢时，采样间隔会从 3 秒逐步放慢，
但软件开着就会继续消化。成功以后原始小地图块立即释放，数据库只留下归一化坐标、时间和置信度。

地图底图放在 assets/maps/，每张图只存一份；每局不另存路线截图。面板用底图 + SVG 折线即时绘制，
所以路线可以悬停、点击并按时间取状态，同时比 PNG 小得多。
"""

from __future__ import annotations

import math
import json
import queue
import threading
import ctypes
from collections import deque
from datetime import datetime
from functools import lru_cache
from time import perf_counter

import storage
from layout import cut
from paths import APP_DIR, CONFIG_DIR

CONFIG_NAME = "routes.json"
ASSET_DIR = APP_DIR / "assets" / "maps"
END_SCREENS = {"eliminated", "champion", "summary", "xp", "squad_out", "lobby"}
ZERO_STATE_FIELDS = {"kills", "assists", "participation", "damage"}


@lru_cache(maxsize=1)
def load_route_config():
    with open(CONFIG_DIR / CONFIG_NAME, encoding="utf-8") as file:
        return json.load(file)


@lru_cache(maxsize=1)
def load_evo_rules():
    with open(CONFIG_DIR / "evo_events.json", encoding="utf-8") as file:
        return json.load(file)


def infer_evo_event(previous, current, rules=None):
    """用 EVO 增量扣掉伤害带来的 EVO 后匹配世界事件；只返回“可能”，不把同分值当成确定事实。"""
    rules = rules or load_evo_rules()
    if previous.get("evo") is None or current.get("evo") is None:
        return None
    evo_delta = current["evo"] - previous["evo"]
    damage_delta = max(0, (current.get("damage") or 0) - (previous.get("damage") or 0))
    residual = evo_delta - damage_delta * float(rules.get("damage_to_evo", 1))
    tolerance = float(rules.get("tolerance", 10))
    candidates = [(abs(residual - event["value"]), event) for event in rules["events"]]
    distance, event = min(candidates, default=(float("inf"), None), key=lambda item: item[0])
    if event is None or distance > tolerance:
        return None
    return {"key": event["key"], "label": event["label"], "value": event["value"], "residual": residual,
            "confidence": max(0.0, 1.0 - distance / max(1.0, tolerance))}


def _distance_to_line(point, start, end):
    px, py = point["x"], point["y"]
    ax, ay, bx, by = start["x"], start["y"], end["x"], end["y"]
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


def simplify(points, tolerance=0.003):
    """Ramer–Douglas–Peucker 简化折线。时间和状态跟着保留下来的点走；首尾一定保留。"""
    if len(points) <= 2:
        return list(points)
    start, end = points[0], points[-1]
    distances = [_distance_to_line(p, start, end) for p in points[1:-1]]
    farthest = max(range(len(distances)), key=distances.__getitem__)
    if distances[farthest] <= tolerance:
        return [start, end]
    split = farthest + 1
    return simplify(points[:split + 1], tolerance)[:-1] + simplify(points[split:], tolerance)


def nearest_on_route(points, x, y):
    """把地图上的点击位置吸附到最近线段，返回坐标、插值后的时间、置信度和前一点的状态。"""
    if not points:
        return None
    if len(points) == 1:
        return {**dict(points[0]), "x": float(points[0]["x"]), "y": float(points[0]["y"])}
    best = None
    for a, b in zip(points, points[1:]):
        ax, ay = float(a["x"]), float(a["y"])
        bx, by = float(b["x"]), float(b["y"])
        dx, dy = bx - ax, by - ay
        u = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        px, py = ax + u * dx, ay + u * dy
        distance = math.hypot(x - px, y - py)
        if best is None or distance < best[0]:
            state = {k: a[k] for k in storage.ROUTE_STATE_FIELDS if k in a.keys()}
            best = (distance, {
                "x": px,
                "y": py,
                "elapsed_ms": round(int(a["elapsed_ms"]) + u * (int(b["elapsed_ms"]) - int(a["elapsed_ms"]))),
                "confidence": min(float(a["confidence"]), float(b["confidence"])),
                **state,
            })
    return best[1]


def point_at_time(points, elapsed_ms):
    """按时间在线段之间插值坐标；状态取这个时间之前最近一个点。"""
    if not points:
        return None
    if elapsed_ms <= points[0]["elapsed_ms"]:
        return dict(points[0])
    for a, b in zip(points, points[1:]):
        if elapsed_ms <= b["elapsed_ms"]:
            span = max(1, int(b["elapsed_ms"]) - int(a["elapsed_ms"]))
            u = max(0.0, min(1.0, (elapsed_ms - int(a["elapsed_ms"])) / span))
            return {**dict(a), "x": float(a["x"]) + u * (float(b["x"]) - float(a["x"])),
                    "y": float(a["y"]) + u * (float(b["y"]) - float(a["y"])), "elapsed_ms": elapsed_ms,
                    "confidence": min(float(a["confidence"]), float(b["confidence"]))}
    return {**dict(points[-1]), "elapsed_ms": elapsed_ms}


class RouteWorker:
    """单线程持续配准。满队列不阻塞截屏线程；RouteRecorder 会自动降低采样频率。"""

    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self.input = queue.Queue(maxsize=int(cfg["worker"]["queue"]))
        self.output = queue.Queue()
        self.matchers, self.previous = {}, {}
        self.average_ms = 0.0
        self.running = True
        self.thread = threading.Thread(target=self._run, name="route-matcher", daemon=True)
        self.thread.start()

    def available(self, map_name):
        item = self.cfg["maps"].get(map_name)
        return bool(item and (ASSET_DIR / item["file"]).is_file())

    def submit(self, session, map_name, elapsed_ms, image):
        try:
            self.input.put_nowait((session, map_name, elapsed_ms, image))
            return True
        except queue.Full:
            return False

    def interval(self):
        base, maximum = self.cfg["sample_seconds"], self.cfg["max_sample_seconds"]
        pressure = self.input.qsize() / max(1, self.input.maxsize)
        slow = max(1.0, self.average_ms / self.cfg["worker"]["target_ms"])
        return min(maximum, base * max(1.0, 1 + pressure * 3, slow))

    def _matcher(self, map_name):
        if map_name not in self.matchers:
            from route_matcher import MapMatcher
            item = self.cfg["maps"][map_name]
            self.matchers[map_name] = MapMatcher(ASSET_DIR / item["file"], self.cfg["matcher"])
        return self.matchers[map_name]

    def _run(self):
        from route_matcher import configure_worker
        configure_worker()
        try:
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except (AttributeError, OSError):
            pass
        while self.running or not self.input.empty():
            try:
                session, map_name, elapsed_ms, image = self.input.get(timeout=0.2)
            except queue.Empty:
                continue
            started = perf_counter()
            try:
                key = (session, map_name)
                result = self._matcher(map_name).locate(image, self.previous.get(key))
                if result:
                    self.previous[key] = (result["x"], result["y"])
                self.output.put((session, elapsed_ms, result))
            except Exception as exc:
                self.output.put((session, elapsed_ms, None))
                self.log(f"⚠ 行进路线配准出错：{exc}")
            finally:
                took = (perf_counter() - started) * 1000
                self.average_ms = took if not self.average_ms else self.average_ms * 0.9 + took * 0.1
                self.input.task_done()

    def close(self):
        self.running = False
        # 退出时不必把尚未开始的旧截图全算完；已经得到的点照样能形成部分路线。
        while True:
            try:
                session, _map_name, elapsed_ms, _image = self.input.get_nowait()
            except queue.Empty:
                break
            self.output.put((session, elapsed_ms, None))
            self.input.task_done()
        self.thread.join(timeout=3)
        return self.thread.is_alive()


class RouteRecorder:
    """主循环里的薄层：管理一局的采样、后台结果和最终数据库归属。"""

    def __init__(self, conn, log=print, cfg=None):
        self.conn, self.log = conn, log
        self.cfg = cfg or load_route_config()
        self.worker = RouteWorker(self.cfg, log)
        self.current = None
        self.completed = deque()
        self.unbound = deque()
        self.sessions = {}
        self.next_session = 1
        self.missing_warned = set()

    def update(self, screen, frame, now, active, map_name):
        self._drain()
        new_start = (self.current is not None and screen == "match_start"
                     and (now - self.current["started"]).total_seconds() > 60)
        if self.current is not None and (screen in END_SCREENS or new_start):
            self._finish(now)
        if not active or not map_name:
            return
        if self.current is None:
            self._start(map_name, now)
        elif self.current["map_name"] != map_name:
            self._finish(now)
            self._start(map_name, now)
        session = self.current
        if not self.worker.available(map_name):
            if map_name not in self.missing_warned:
                self.log(f"⚠ 行进路线：{map_name} 没有基准地图，跳过这局（assets/maps/）")
                self.missing_warned.add(map_name)
            return
        elapsed = (now - session["started"]).total_seconds()
        if elapsed < session["next_sample"]:
            return
        r = self.cfg["minimap"]
        crop = cut(frame, r["roi"], self.cfg["base_resolution"], r.get("anchor"))
        crop = crop.copy()
        if self.worker.submit(session["id"], map_name, round(elapsed * 1000), crop):
            session["pending"] += 1
        session["next_sample"] = elapsed + self.worker.interval()

    def bind_match(self, match_id):
        """PostMatch 每记下一局调用；路线可能先结束，也可能这一帧稍后才看到结束画面。"""
        self._drain()
        target = next((s for s in self.completed if s["match_id"] is None), None)
        if target is None:
            self.unbound.append(match_id)
        else:
            target["match_id"] = match_id
            self._save_if_ready(target)

    def _start(self, map_name, now):
        item = self.cfg["maps"].get(map_name) or {}
        session = {"id": self.next_session, "map_name": map_name, "map_asset": item.get("file", ""),
                   "started": now, "ended": None, "next_sample": 0.0, "pending": 0, "points": [],
                   "match_id": None, "saved": False, "top_jobs": deque(), "states": [], "last_state": {}}
        self.next_session += 1
        self.current = session
        self.sessions[session["id"]] = session

    def _finish(self, now):
        if self.current is None:
            return
        if self.current["match_id"] is None and self.unbound:
            self.current["match_id"] = self.unbound.popleft()
        self.current["ended"] = now
        self.completed.append(self.current)
        self._save_if_ready(self.current)
        self.current = None

    def _drain(self):
        while True:
            try:
                session_id, elapsed_ms, result = self.worker.output.get_nowait()
            except queue.Empty:
                break
            session = self.sessions.get(session_id)
            if session is not None:
                session["pending"] = max(0, session["pending"] - 1)
                if result is not None:
                    session["points"].append({"elapsed_ms": elapsed_ms, **result})
                self._save_if_ready(session)

    def record_top(self, snip, now):
        """右上角发生变化时只保留小块和时间；OCR 留到非战斗阶段做。"""
        if self.current is None:
            return
        elapsed_ms = max(0, round((now - self.current["started"]).total_seconds() * 1000))
        jobs = self.current["top_jobs"]
        limit = int(self.cfg.get("max_state_samples", 60))
        if len(jobs) >= limit:
            # 伤害连续上涨时变化很多。内存到上限就把旧点隔一个留一个，首尾仍在，之后还能继续采。
            self.current["top_jobs"] = jobs = deque(list(jobs)[::2])
        jobs.append((elapsed_ms, snip))

    def idle_step(self, hud):
        """非战斗画面每轮最多读一个状态块，避免和总结页争抢 CPU。"""
        self._drain()
        session = next((s for s in self.completed if not s["saved"] and s["top_jobs"]), None)
        if session is None:
            return False
        elapsed_ms, snip = session["top_jobs"].popleft()
        values, scores, shown = hud.read_top(snip)
        if values.get("squads_left") is not None or values.get("rp_delta") is not None:
            state = dict(session["last_state"])
            for name, value in values.items():
                if value is not None:
                    state[name] = value
                elif name in ZERO_STATE_FIELDS and name not in shown and name not in state:
                    state[name] = 0
            changed = {k: v for k, v in state.items() if session["last_state"].get(k) != v}
            if changed:
                confidence = [scores.get(k) for k in changed if scores.get(k) is not None]
                session["states"].append({"elapsed_ms": elapsed_ms, **state,
                                          "ocr_confidence": sum(confidence) / len(confidence) if confidence else None,
                                          "changed": tuple(changed)})
                session["last_state"] = state
        self._save_if_ready(session)
        return True

    @staticmethod
    def _state_label(previous, state):
        labels = {"damage": "伤害", "evo": "进化点", "kills": "击杀", "assists": "助攻",
                  "participation": "参与", "squads_left": "剩余小队", "rp_delta": "RP"}
        parts = []
        for name in state.get("changed", ()):
            value = state.get(name)
            old = previous.get(name)
            if isinstance(value, int) and isinstance(old, int) and value > old:
                parts.append(f"{labels[name]} +{value - old}")
            else:
                parts.append(f"{labels[name]} {value}")
        inferred = infer_evo_event(previous, state)
        if inferred is not None:
            parts.insert(0, inferred["label"])
        return "、".join(parts) or "状态变化"

    def _with_states(self, points, states):
        """把每个定位点补上当时最近一次状态，并生成系统时间点。"""
        if not points:
            return points, []
        states = sorted(states, key=lambda s: s["elapsed_ms"])
        current, index = {}, 0
        enriched = []
        for point in points:
            while index < len(states) and states[index]["elapsed_ms"] <= point["elapsed_ms"]:
                current.update({k: states[index][k] for k in storage.ROUTE_STATE_FIELDS if k in states[index]})
                index += 1
            enriched.append({**point, **current})
        marks, previous = [], {}
        for state in states:
            mark = point_at_time(enriched, state["elapsed_ms"])
            if mark is None:
                continue
            mark.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
            mark.update({"elapsed_ms": state["elapsed_ms"], "kind": "auto", "label": self._state_label(previous, state),
                         "note": (f"OCR {state['ocr_confidence']:.2f}" if state.get("ocr_confidence") is not None else None),
                         "created_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
            marks.append(mark)
            previous.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
        return enriched, marks

    def _save_if_ready(self, session):
        if (session["saved"] or session["ended"] is None or session["match_id"] is None
                or session["pending"] or session["top_jobs"]):
            return
        points = sorted(session["points"], key=lambda p: p["elapsed_ms"])
        raw_count = len(points)
        points = simplify(points, self.cfg["simplify_tolerance"]) if points else []
        points, marks = self._with_states(points, session["states"])
        coverage = raw_count / max(1, round((session["ended"] - session["started"]).total_seconds()
                                            / self.cfg["sample_seconds"]))
        status = "ready" if len(points) >= self.cfg["min_route_points"] else ("partial" if points else "failed")
        route = {
            "map_name": session["map_name"], "map_asset": session["map_asset"], "status": status,
            "started_at": f"{session['started']:%Y-%m-%d %H:%M:%S}",
            "ended_at": f"{session['ended']:%Y-%m-%d %H:%M:%S}",
            "coverage": min(1.0, coverage), "processed_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        }
        storage.save_route(self.conn, session["match_id"], route, points, marks)
        session["saved"] = True
        self.log(f"           行进路线：{session['map_name']}，定位 {raw_count} 个点，简化后 {len(points)} 个"
                 f"（编号 {session['match_id']}，{status}）")

    def stop(self, now, hud=None):
        self._finish(now)
        worker_left = self.worker.close()
        self._drain()
        if worker_left:
            for session in self.completed:
                session["pending"] = 0
            self.log("⚠ 行进路线后台仍在结束最后一张定位；已用现有点保存，不继续等待")
        if hud is not None:
            while self.idle_step(hud):
                pass
        for session in list(self.completed):
            self._save_if_ready(session)


def route_view(conn, match_id):
    """面板需要的 JSON 友好结构。"""
    route = storage.route_of(conn, match_id)
    if route is None:
        return None
    points = [dict(r) for r in storage.route_points_of(conn, match_id)]
    marks = [dict(r) for r in storage.route_marks_of(conn, match_id)]
    meta = dict(route)
    meta["asset_available"] = bool(meta.get("map_asset") and (ASSET_DIR / meta["map_asset"]).is_file())
    return {"meta": meta, "points": points, "marks": marks}
