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
ROUTE_EVENT_FIELDS = ("kills", "assists")  # 面板在路线图上单独画形状的两项（击杀、助攻）
DEATH_STATE = "成盒"  # 与 in_match.STATE_DEAD 同一个词（那一局里自己成盒 = 死亡）


def thin_by_time(jobs, limit):
    """按时间均匀地减少状态块：从头到尾每隔「总时长 / (上限 - 1)」留一个，最新的那块一定留下。

    不能像以前那样隔一个丢一个（`jobs[::2]`）：那样每满一次就折半一次，越早的点被丢的次数越多，
    整局下来开局会空掉一大段。2026-11-07 实战就是这样：一条 20 分钟的路线，节点在快结束前全是 0，
    到结尾（最后被杀那一刻）才一次跳到总数。"""
    limit = max(2, int(limit))
    jobs = list(jobs)
    if len(jobs) <= limit:
        return jobs
    gap = max(1, (jobs[-1][0] - jobs[0][0]) // (limit - 1))
    kept = [jobs[-1]]
    for item in reversed(jobs[:-1]):
        if kept[-1][0] - item[0] >= gap:
            kept.append(item)
    kept.reverse()
    return kept


@lru_cache(maxsize=1)
def load_route_config():
    with open(CONFIG_DIR / CONFIG_NAME, encoding="utf-8") as file:
        return json.load(file)


@lru_cache(maxsize=1)
def load_evo_rules():
    with open(CONFIG_DIR / "evo_events.json", encoding="utf-8") as file:
        return json.load(file)


def infer_evo_event(previous, current, rules=None):
    """用获得的 EVO 扣掉伤害贡献后匹配世界事件；只返回“可能”，不把同分值当成确定事实。"""
    rules = rules or load_evo_rules()
    if previous.get("evo") is None or current.get("evo") is None:
        return None
    if rules.get("value_mode") == "remaining":
        evo_delta = previous["evo"] - current["evo"]
    else:
        evo_delta = current["evo"] - previous["evo"]
    # 剩余值在升级后会跳高；没有同时可靠读到等级时，不能拿这次跳变猜固定分值事件。
    if evo_delta <= 0:
        return None
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


def route_step_ok(previous, current, cfg):
    """相邻定位是否可能由正常移动产生；过滤数秒内横跨地图的误匹配。"""
    elapsed = max(0.0, (current["elapsed_ms"] - previous["elapsed_ms"]) / 1000)
    limit = max(float(cfg["min_step_radius"]), elapsed * float(cfg["max_speed_per_second"]))
    return math.hypot(current["x"] - previous["x"], current["y"] - previous["y"]) <= limit


class RouteWorker:
    """单线程持续配准。满队列不阻塞截屏线程；RouteRecorder 会自动降低采样频率。"""

    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self.input = queue.Queue(maxsize=int(cfg["worker"]["queue"]))
        self.output = queue.Queue()
        self.matchers, self.previous, self.tentative, self.rejects = {}, {}, {}, {}
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
                previous = self.previous.get(key)
                near = (previous["x"], previous["y"]) if previous is not None else None
                result = self._matcher(map_name).locate(image, near)
                result = self._accept(key, elapsed_ms, result)
                self.output.put((session, elapsed_ms, result))
            except Exception as exc:
                self.output.put((session, elapsed_ms, None))
                self.log(f"⚠ 行进路线配准出错：{exc}")
            finally:
                took = (perf_counter() - started) * 1000
                self.average_ms = took if not self.average_ms else self.average_ms * 0.9 + took * 0.1
                self.input.task_done()

    def _accept(self, key, elapsed_ms, result):
        """首次位置要连续两次相近；已开始后拒绝不可能的瞬移，连续异常后重新找起点。"""
        if result is None:
            return None
        current = {"elapsed_ms": elapsed_ms, **result}
        previous = self.previous.get(key)
        if previous is None:
            candidate = self.tentative.get(key)
            if candidate is not None:
                distance = math.hypot(current["x"] - candidate["x"], current["y"] - candidate["y"])
                if distance <= float(self.cfg["matcher"]["start_confirm_radius"]):
                    self.previous[key] = current
                    self.tentative.pop(key, None)
                    self.rejects[key] = 0
                    return current
            self.tentative[key] = current
            return None
        if route_step_ok(previous, current, self.cfg["matcher"]):
            self.previous[key] = current
            self.rejects[key] = 0
            return current
        self.rejects[key] = self.rejects.get(key, 0) + 1
        if self.rejects[key] >= int(self.cfg["matcher"]["reset_after_rejects"]):
            self.previous.pop(key, None)
            self.tentative[key] = current
            self.rejects[key] = 0
        return None

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
        elapsed = (now - session["started"]).total_seconds()
        if elapsed < session["next_sample"]:
            return
        elapsed_ms = round(elapsed * 1000)
        evo = self.cfg.get("evo")
        if evo:
            crop = cut(frame, evo["roi"], self.cfg["base_resolution"], evo.get("anchor")).copy()
            self.record_evo(crop, now)
        if self.worker.available(map_name):
            r = self.cfg["minimap"]
            crop = cut(frame, r["roi"], self.cfg["base_resolution"], r.get("anchor")).copy()
            if self.worker.submit(session["id"], map_name, elapsed_ms, crop):
                session["pending"] += 1
        elif map_name not in self.missing_warned:
            self.log(f"⚠ 行进路线：{map_name} 没有基准地图；仍记录状态，但这局不能生成路线（assets/maps/）")
            self.missing_warned.add(map_name)
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
                   "match_id": None, "saved": False, "top_jobs": deque(), "evo_jobs": deque(),
                   "state_jobs": [], "last_evo_thumb": None, "states": [], "last_state": {}}
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
        self.current["top_jobs"].append((elapsed_ms, snip))
        self._thin("top_jobs")

    def record_state(self, state, now):
        """存活 / 倒地 / 成盒 变了：只记时间和状态，位置赛后按时间从路线上取。

        对局中的状态判断不花 OCR，所以这一步几乎没有代价；成盒会变成路线图上的"死亡"标记。"""
        if self.current is None:
            return
        elapsed_ms = max(0, round((now - self.current["started"]).total_seconds() * 1000))
        jobs = self.current["state_jobs"]
        if jobs and jobs[-1][0] == elapsed_ms and jobs[-1][1] == state:
            return
        jobs.append((elapsed_ms, state))

    def record_evo(self, crop, now):
        """低频保存发生变化的 EVO 小块到内存；不落盘，赛后 OCR 完就释放。"""
        if self.current is None or crop.size == 0:
            return
        thumb = crop[::4, ::4].astype("int16")
        previous = self.current["last_evo_thumb"]
        self.current["last_evo_thumb"] = thumb
        if previous is not None and previous.shape == thumb.shape:
            if float(abs(thumb - previous).mean()) < float(self.cfg["evo"].get("change", 3)):
                return
        elapsed_ms = max(0, round((now - self.current["started"]).total_seconds() * 1000))
        self.current["evo_jobs"].append((elapsed_ms, crop))
        self._thin("evo_jobs")

    def _thin(self, key):
        limit = int(self.cfg.get("max_state_samples", 60))
        jobs = self.current[key]
        if len(jobs) > max(2, limit):
            self.current[key] = deque(thin_by_time(jobs, limit))

    def idle_step(self, hud):
        """非战斗画面每轮最多读一个状态块，避免和总结页争抢 CPU。"""
        self._drain()
        session = next((s for s in self.completed if not s["saved"] and (s["top_jobs"] or s["evo_jobs"])), None)
        if session is None:
            return False
        use_evo = bool(session["evo_jobs"] and
                       (not session["top_jobs"] or session["evo_jobs"][0][0] <= session["top_jobs"][0][0]))
        if use_evo:
            elapsed_ms, crop = session["evo_jobs"].popleft()
            value, score = hud.read_evo(crop, self.cfg["evo"])
            if value is not None and session["last_state"].get("evo") != value:
                state = {**session["last_state"], "evo": value}
                session["states"].append({"elapsed_ms": elapsed_ms, **state, "ocr_confidence": score,
                                          "changed": ("evo",)})
                session["last_state"] = state
        else:
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
        labels = {"damage": "伤害", "evo": "距下一级 EVO", "kills": "击杀", "assists": "助攻",
                  "participation": "参与", "squads_left": "剩余小队", "rp_delta": "RP"}
        parts = []
        for name in state.get("changed", ()):
            value = state.get(name)
            old = previous.get(name)
            if name == "evo" and isinstance(value, int) and isinstance(old, int) and value > old:
                parts.append(f"EVO 等级提升，距下一级 {value}")
            elif name == "evo":
                parts.append(f"距下一级 EVO {value}")
            elif isinstance(value, int) and isinstance(old, int) and value > old:
                parts.append(f"{labels[name]} +{value - old}")
            else:
                parts.append(f"{labels[name]} {value}")
        inferred = infer_evo_event(previous, state)
        if inferred is not None:
            parts.insert(0, inferred["label"])
        return "、".join(parts) or "状态变化"

    def _with_states(self, points, states, events=()):
        """把每个定位点补上当时最近一次状态，并生成系统时间点（含成盒 = 死亡）和时间点。"""
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
            changed = state.get("changed") or ()
            if changed and set(changed) <= {"damage"}:
                # 伤害一直在涨：每涨一点都记一个时间点，表格和地图上的圆点会被埋掉。
                # 伤害照样补在路线上（上面 enriched），只是不单独记时间点。
                previous.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
                continue
            mark = point_at_time(enriched, state["elapsed_ms"])
            if mark is None:
                continue
            mark.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
            mark.update({"elapsed_ms": state["elapsed_ms"], "kind": "auto", "label": self._state_label(previous, state),
                         "note": (f"OCR {state['ocr_confidence']:.2f}" if state.get("ocr_confidence") is not None else None),
                         "created_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
            marks.append(mark)
            previous.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
        for elapsed_ms, state_name in sorted(events, key=lambda item: item[0]):
            if state_name != DEATH_STATE:
                continue  # 存活、倒地不单独记：只是标题上的状态跳动
            mark = point_at_time(enriched, elapsed_ms)
            if mark is None:
                continue
            values = {}
            for state in states:
                if state["elapsed_ms"] <= elapsed_ms:
                    values.update({k: state[k] for k in storage.ROUTE_STATE_FIELDS if k in state})
            mark.update(values)
            mark.update({"elapsed_ms": int(elapsed_ms), "kind": "death", "label": "死亡", "note": None,
                         "created_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
            marks.append(mark)
        marks.sort(key=lambda mark: mark["elapsed_ms"])
        return enriched, marks

    def _save_if_ready(self, session):
        if (session["saved"] or session["ended"] is None or session["match_id"] is None
                or session["pending"] or session["top_jobs"] or session["evo_jobs"]):
            return
        points = sorted(session["points"], key=lambda p: p["elapsed_ms"])
        raw_count = len(points)
        points = simplify(points, self.cfg["simplify_tolerance"]) if points else []
        points, marks = self._with_states(points, session["states"], session["state_jobs"])
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


def mark_events(marks):
    """给每个时间点标上这一刻为什么记下它（kills、assists），以及这一刻 RP 涨了多少，面板据此在路线图上画不同的形状。

    只认系统时间点，和上一个系统时间点比大小：一局里第一次读到击杀不算"这一刻的击杀"
    （没有上一块可比）；手动的点带的是吸附位置那一刻的读数，没有先后可比，一律留空。

    RP 不在这里算：面板直接显示 mark["rp_delta"]，也就是游戏右下角那个数字（开局是负的入场费，
    打完整局是 +223 这种从开局算起的累计值）；用户要的就是"当时右上角写多少就写多少"。"""
    previous = {}
    for mark in marks:
        events = []
        if mark.get("kind") == "auto":
            events = [name for name in ROUTE_EVENT_FIELDS
                      if isinstance(mark.get(name), int) and isinstance(previous.get(name), int)
                      and mark[name] > previous[name]]
            previous.update({name: mark[name] for name in ROUTE_EVENT_FIELDS if isinstance(mark.get(name), int)})
        mark["events"] = events
    return marks


def route_view(conn, match_id):
    """面板需要的 JSON 友好结构。"""
    route = storage.route_of(conn, match_id)
    if route is None:
        return None
    points = [dict(r) for r in storage.route_points_of(conn, match_id)]
    marks = mark_events([dict(r) for r in storage.route_marks_of(conn, match_id)])
    meta = dict(route)
    meta["asset_available"] = bool(meta.get("map_asset") and (ASSET_DIR / meta["map_asset"]).is_file())
    return {"meta": meta, "points": points, "marks": marks}
