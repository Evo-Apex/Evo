"""统计对账：排位、匹配、外卡、未知和其他各一局，本机和「全体玩家」用同一套口径。

用其他模式验证非大逃杀对局的归类：
- 全部模式 5 局
- 大逃杀 3 局（排位 + 匹配 + 外卡）
- 匹配分组 2 局（匹配和其他）、外卡 1 局、未知 1 局、排位 1 局
- 本机算出来的数和服务器的汇总（server/stats/worker.js，用 node 跑同一份数据）一致

服务器的部分要有 Node.js 才跑（没有就跳过那几条，不算通过）。本机那部分不用 Node。

运行：python tests/test_mode_stats.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from fake_game import ROOT, report  # 要在项目代码之前导入

import modes
import panel_app
import storage

SERVER = ROOT / "server" / "stats"
NODE = shutil.which("node")


def local_db(path):
    """排位、匹配、外卡、未知、其他各一局（同一份数据也给服务器用）。"""
    path.unlink(missing_ok=True)
    conn = storage.connect(path)
    plan = [("ranked", 3), ("pubs", 5), ("wildcard", 2), (None, 11), ("other", 1)]
    ids = []
    for i, (mode, place) in enumerate(plan):
        ids.append(storage.insert_match(conn, {
            "ended_at": f"2026-11-24 1{i}:00:00", "mode": mode, "map": "风暴点", "legend": "恶灵",
            "placement": place, "kills": i, "assists": 0, "knockdowns": 0, "damage": 500 + i, "squad_kills": 10,
            "needs_review": 0, "player_name": "ImperialHzd"}))
    conn.close()
    return ids


def local_counts(path):
    panel_app.app.config.update(DB=path, TESTING=True)
    conn = storage.connect(path)
    rows = storage.all_matches(conn)
    conn.close()
    return {key: len(panel_app.mode_rows(rows, key))
            for key in ("all", "br", "ranked", "pubs", "wildcard", "unknown")}


def server_counts(path):
    """把同一份数据发给统计服务器（node 跑 server/stats/worker.js），读它汇总里各模式的局数。
    返回 {模式键: 局数} 或 None（没有 Node.js）。"""
    if not NODE:
        return None
    script = r"""
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
const worker = (await import("./worker.js")).default;
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf-8");
class FakeD1 {
  constructor() { this.db = new DatabaseSync(":memory:"); this.db.exec(schema); }
  prepare(sql) {
    const db = this.db;
    const make = (args) => ({
      bind: (...more) => make(more),
      exec() {
        const stmt = db.prepare(sql);
        if (/^\s*(INSERT|UPDATE|DELETE)/i.test(sql)) return { results: [], meta: { changes: Number(stmt.run(...args).changes) } };
        return { results: stmt.all(...args).map((r) => ({ ...r })), meta: {} };
      },
      async all() { return this.exec(); },
      async run() { return this.exec(); },
      async first() { return this.exec().results[0] ?? null; },
    });
    return make([]);
  }
  async batch(list) { return list.map((s) => s.exec()); }
}
const env = { DB: new FakeD1() };
const games = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const res = await worker.fetch(new Request("https://stats.test/v1/report", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ id: "0".repeat(32), version: "0.7.2", matches: games }),
}), env, {});
if (!res.ok) { console.error(await res.text()); process.exit(1); }
const summary = JSON.parse(await (await worker.fetch(new Request("https://stats.test/v1/summary"), env, {})).text());
const out = {};
for (const key of ["all", "br", "ranked", "pubs", "wildcard", "unknown"]) out[key] = summary.slices[`all.${key}.all`].matches;
console.log(JSON.stringify(out));
"""
    payload = []
    conn = storage.connect(path)
    for row in storage.all_matches(conn):
        payload.append({
            "k": f"{row['match_id']:020x}", "day": "2026-11-24", "version": "0.7.2", "mode": row["mode"],
            "map": row["map"], "legend": row["legend"], "mates": [], "placement": row["placement"],
            "kills": row["kills"], "assists": row["assists"], "damage": row["damage"], "review": bool(row["needs_review"]),
        })
    conn.close()
    temp = path.with_name("stats_payload.json")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    # 脚本要和 worker.js 放在一起（import "./worker.js" 是相对脚本自己的路径）；跑完删掉
    script_path = SERVER / "_stats_check.mjs"
    script_path.write_text(script, encoding="utf-8")
    try:
        done = subprocess.run([NODE, str(script_path), str(temp)], cwd=SERVER, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
    finally:
        temp.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
    if done.returncode != 0:
        raise RuntimeError(done.stdout + done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


def main():
    print("统计对账：排位、匹配、外卡、未知、其他各一局（计划第 4 节）")
    folder = Path(__import__("tempfile").mkdtemp(prefix="evo-modes-"))
    try:
        path = folder / "modes.db"
        local_db(path)
        counts = local_counts(path)
        expected = {"all": 5, "br": 3, "ranked": 1, "pubs": 2, "wildcard": 1, "unknown": 1}
        checks = {f"本机统计每档局数符合预期（{counts}）": counts == expected}
        server = server_counts(path)
        if server is None:
            print("  ⏭ 服务器那几条跳过：没找到 Node.js（统计服务器是 JavaScript）")
            print("  ⚠ 缺这一半不算通过：本机那几条过了，本机和服务器一致这件事还没核对")
            return None if report(checks) else False
        checks.update({
            f"服务器统计每档局数符合预期（{server}）": server == expected,
            "本机和服务器各档对账一致": server == counts,
        })
        return report(checks)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
