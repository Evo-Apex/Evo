// 统计服务器"全体玩家"汇总的测试（2026-11-19 改成按天增量算）：node server/stats/test.mjs（不用 wrangler、不联网）。
// 用 Node 自带的 SQLite 当 D1（建表用 schema.sql），数据经 POST /v1/report 写进去，假装时间一小时一小时过去跑定时任务，
// 每一步都和"同一份数据从头建"的结果比（去掉 made_at 后 JSON 一字不差）：第一次分几个小时重建；重发改过的局、原样重发、新局、
// 新的日子、没有日期的局；/v1/delete 只重建那几天、中间结果里不留这个编号；核对发现某天不对会换掉。
// 和原来的 SQL 算法逐项一样，是 2026-11-19 用线上数据的副本另外核对的（见 docs/底座.md"匿名统计"），这里不再比

import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

// 假的时间：worker.js 里 new Date()、Date.now() 都用它
const RealDate = Date;
let fakeNow = RealDate.parse("2026-11-18T23:07:00Z");
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [fakeNow])); }
  static now() { return fakeNow; }
};
const worker = (await import("./worker.js")).default;
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf-8");

// D1 的样子：prepare().bind().all() / first() / run()、batch()（一个事务）
class FakeD1 {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    this.db.exec(schema);
  }
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
  async batch(list) {
    this.db.exec("BEGIN");
    try {
      const out = list.map((s) => s.exec());
      this.db.exec("COMMIT");
      return out;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  rows(sql, ...args) { return this.db.prepare(sql).all(...args).map((r) => ({ ...r })); }
}

let failed = 0;
function check(ok, what) {
  console.log(`${ok ? "✓" : "✗"} ${what}`);
  if (!ok) failed += 1;
}

// ---------- 造数据 ----------
let seed = 7;
const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const pick = (list) => list[Math.floor(rand() * list.length)];
const hex = (n) => Array.from({ length: n }, () => "0123456789abcdef"[Math.floor(rand() * 16)]).join("");
// 有一个名字是另一个的开头（组合按名字排先后时要逐个名字比）
const LEGENDS = ["寻血犬", "直布罗陀", "命脉", "恶灵", "班加罗尔", "动力小子", "探路者", "沃特森", "侵蚀", "幻象", "命脉二号", null];
const TIERS = ["菜鸟", "白银", "黄金", "白金", "钻石", "大师", "Apex 猎杀者", "认不出的段位", null];
const DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-08", "2026-11-12", "2026-11-13", "2026-11-17", "2026-11-18", null];
const WEAPONS = ["r301", "flatline", "car", "nemesis", "wingman"];
const ids = Array.from({ length: 14 }, () => hex(32));
const games = new Map();  // "编号 k" → [编号, 发给服务器的一局]

function randomGame(day = pick(DAYS)) {
  const num = (list) => pick(list);
  const w = rand();
  return {
    k: hex(20), day, version: "0.6.1", mode: pick(["ranked", "ranked", "pubs", null, "other", "wildcard"]), map: null,
    legend: pick(LEGENDS), mates: [pick(LEGENDS), pick(LEGENDS)],
    mate_stats: [0, 1].map(() => ({ kills: num([null, 0, 2, 5]), assists: num([null, 1]), knockdowns: null, damage: num([null, 300, 1500]) })),
    placement: num([null, 1, 1, 2, 4, 5, 6, 13, 20]), kills: num([null, 0, 1, 3, 9]), assists: num([null, 0, 2]),
    knockdowns: null, damage: num([null, 0, 420, 2100]), rp_delta: null, tier: pick(TIERS), rp_source: null,
    review: rand() < 0.15, edited: [],
    weapons: w < 0.2 ? null : w < 0.3 ? {} : Object.fromEntries([pick(WEAPONS), pick(WEAPONS)].map((k) => [k, 1 + Math.floor(rand() * 600)])),
  };
}

async function send(env, id, list) {
  for (let i = 0; i < list.length; i += 100) {
    const res = await worker.fetch(new Request("https://stats.test/v1/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, version: "0.6.1", matches: list.slice(i, i + 100) }),
    }), env, {});
    if (!res.ok) throw new Error(await res.text());
  }
  for (const g of list) games.set(`${id} ${g.k}`, [id, g]);
}

async function cron(env) {
  const waits = [];
  await worker.scheduled({ cron: "7 * * * *" }, env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
}

const state = (db) => JSON.parse(db.rows("SELECT body FROM summary WHERE name = 'v1_days'")[0]?.body ?? "null");
const summaryOf = (db) => {
  const row = db.rows("SELECT body FROM summary WHERE name = 'v1'")[0];
  return row ? JSON.stringify({ ...JSON.parse(row.body), made_at: null }) : null;
};

// 同一份数据从头建：新的库，把所有局原样发一遍，GET /v1/summary（还没算过就现算，一次建完）
async function reference() {
  const env = { DB: new FakeD1() };
  const byId = {};
  for (const [id, g] of games.values()) (byId[id] ??= []).push(g);
  for (const [id, list] of Object.entries(byId)) await send(env, id, list);
  const res = await worker.fetch(new Request("https://stats.test/v1/summary"), env, {});
  return JSON.stringify({ ...JSON.parse(await res.text()), made_at: null });
}

// ---------- 第一次：分几个小时重建，建完的下一个小时出汇总 ----------
const env = { DB: new FakeD1() };
const db = env.DB;
for (const id of ids) await send(env, id, Array.from({ length: 25 + Math.floor(rand() * 30) }, () => randomGame()));
// 先放一份"昨天"出的汇总（线上部署时就是这样）：建完以前不换
db.db.prepare("INSERT INTO summary (name, body, made_at) VALUES ('v1', '{\"old\":true}', '2026-11-18T00:07:00.000Z')").run();
fakeNow = RealDate.parse("2026-11-19T00:07:00Z");
let runs = 0;
while ((state(db)?.todo.length ?? 1) > 0 && runs < 20) {
  await cron(env);
  runs += 1;
  if (state(db).todo.length) check(JSON.parse(db.rows("SELECT body FROM summary WHERE name = 'v1'")[0].body).old, `重建第 ${runs} 次：还有 ${state(db).todo.length} 天没建，汇总照旧是原来的`);
  fakeNow += 3600 * 1000;
}
check(runs === Math.ceil(DAYS.length / 4), `一共 ${DAYS.length} 天（含没有日期的），每小时建 4 天，${runs} 次建完`);
await cron(env);
check(summaryOf(db) === (await reference()), "建完的下一个小时出汇总：和从头建的一字不差");
const madeAt = db.rows("SELECT made_at FROM summary WHERE name = 'v1'")[0].made_at;
await cron(env);
check(db.rows("SELECT made_at FROM summary WHERE name = 'v1'")[0].made_at === madeAt, "同一天再运行不再出汇总");

// ---------- 重发：改过的局、原样重发的局、新局（新的日子、没有日期） ----------
const all = [...games.values()];
for (let i = 0; i < 60; i++) {
  const [id, g] = pick(all);
  const changed = { ...g, ...[{ placement: pick([1, 3, 18, null]) }, { legend: pick(LEGENDS) }, { tier: pick(TIERS) },
    { mode: pick(["ranked", "pubs", null]) }, { review: !g.review }, { weapons: null }, { mates: [g.legend, pick(LEGENDS)] }][i % 7] };
  await send(env, id, [changed]);
}
for (let i = 0; i < 20; i++) {  // 原样重发
  const [id, g] = pick(all);
  await send(env, id, [g]);
}
for (let i = 0; i < 30; i++) await send(env, pick(ids), [randomGame(pick([...DAYS, "2026-11-19"]))]);
await send(env, hex(32), [randomGame(null), randomGame("2026-11-19")]);  // 新玩家
fakeNow = RealDate.parse("2026-11-20T00:07:00Z");
await cron(env);
check(summaryOf(db) === (await reference()), "第二天 0 点：增量更新以后出的汇总和从头建的一字不差");
check(!db.rows("SELECT 1 FROM summary_days WHERE day NOT IN (SELECT DISTINCT coalesce(day, '') FROM matches)").length,
      "没有对不上 matches 的日子");

const wildBody = JSON.parse(await (await worker.fetch(new Request("https://stats.test/v1/summary"), env, {})).text());
const wild = wildBody.slices;
const wildGames = [...games.values()].filter(([, g]) => g.mode === "wildcard");
const unknownGames = [...games.values()].filter(([, g]) => g.mode === null || g.mode === undefined);
check(wildGames.length > 0, `造的局里有外卡（${wildGames.length} 局）`);
check(wild["all.wildcard.all"] && typeof wild["all.wildcard.all"].matches === "number", "汇总里有一份 all.wildcard.all");
check(!("all.wildcard.diamond" in wild) && !("all.wildcard.low" in wild), "外卡不分段位（只有 all.wildcard.all）");
check(unknownGames.length > 0, `造的局里有没认出模式的局（${unknownGames.length} 局）`);
check(wild["all.unknown.all"] && typeof wild["all.unknown.all"].matches === "number", "汇总里有一份 all.unknown.all");
check(wild["all.unknown.all"].matches === unknownGames.length,
      `未知那一份的局数就是没认出模式的局数（${wild["all.unknown.all"].matches} / ${unknownGames.length}）`);
const pubsMatches = wild["all.pubs.all"].matches;
const rankedMatches = wild["all.ranked.all"].matches;
const wildcardMatches = wild["all.wildcard.all"].matches;
const unknownMatches = wild["all.unknown.all"].matches;
const totalMatches = [...games.values()].length;
check(pubsMatches + rankedMatches + wildcardMatches + unknownMatches === totalMatches,
      `四份加起来等于总局数（${rankedMatches} + ${pubsMatches} + ${wildcardMatches} + ${unknownMatches} = ${totalMatches}）`);
check(wildcardMatches === wildGames.length, `外卡那一份的局数就是外卡的局数（${wildcardMatches}）`);
const pubsOnly = [...games.values()].filter(([, g]) => g.mode !== null && g.mode !== undefined
  && g.mode !== "ranked" && g.mode !== "wildcard");
check(pubsMatches === pubsOnly.length, `匹配那一份只剩认出是匹配 / 其他模式的局（${pubsMatches} / ${pubsOnly.length}）`);
const allSlice = wild["all.all.all"];
const brSlice = wild["all.br.all"];
// 按原始模式核对大逃杀，公开匹配分组仍包含其他模式。
const isBr = (g) => ["ranked", "pubs", "wildcard"].includes(g.mode);
const brGames = [...games.values()].filter(([, g]) => isBr(g));
const otherGames = pubsOnly.filter(([, g]) => g.mode !== "pubs");
check(allSlice && typeof allSlice.matches === "number", "汇总里有一份 all.all.all（全部模式）");
check(brSlice && typeof brSlice.matches === "number", "汇总里有一份 all.br.all（大逃杀）");
check(allSlice.matches === totalMatches, `全部模式 = 所有局（${allSlice.matches} / ${totalMatches}）`);
check(brSlice.matches === brGames.length, `大逃杀 = 排位 + 匹配 + 外卡（${brSlice.matches} / ${brGames.length}）`);
check(otherGames.length > 0 && allSlice.matches === brSlice.matches + unknownMatches + otherGames.length,
      "全部模式 = 大逃杀 + 未知 + 其他");
check(!("all.all.diamond" in wild) && !("all.br.low" in wild), "全部模式、大逃杀不分段位");
// 近 7 天、本赛季这两档也要有 all / br（页面上的范围按钮会用到），局数按各自的起始日期算
const inFrom = ([, g], from) => Boolean(g.day) && g.day >= from;
const recent = [...games.values()].filter((x) => inFrom(x, wildBody.recent_from));
const season = [...games.values()].filter((x) => inFrom(x, wildBody.season_from));
check(wild["7d.all.all"]?.matches === recent.length,
      `近 7 天的全部模式 = ${wildBody.recent_from} 起的局（${wild["7d.all.all"]?.matches} / ${recent.length}）`);
check(wild["7d.br.all"]?.matches === recent.filter((x) => isBr(x[1])).length,
      `近 7 天的大逃杀 = ${wildBody.recent_from} 起的大逃杀局（${wild["7d.br.all"]?.matches} / ${recent.filter((x) => isBr(x[1])).length}）`);
check(wild["season.all.all"]?.matches === season.length,
      `本赛季的全部模式 = ${wildBody.season_from} 起的局（${wild["season.all.all"]?.matches} / ${season.length}）`);
check(wild["season.br.all"]?.matches === season.filter((x) => isBr(x[1])).length,
      `本赛季的大逃杀 = ${wildBody.season_from} 起的大逃杀局（${wild["season.br.all"]?.matches} / ${season.filter((x) => isBr(x[1])).length}）`);
check(!("7d.all.diamond" in wild) && !("season.br.low" in wild), "近 7 天、本赛季的 all / br 也不分段位");

// ---------- /v1/delete：只重建这个编号打过的那几天，中间结果里不留它 ----------
const gone = ids[3];
const goneDays = db.rows("SELECT DISTINCT day FROM summary_pairs WHERE id = ?", gone).length;
await worker.fetch(new Request("https://stats.test/v1/delete", { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ id: gone }) }), env, {});
for (const key of [...games.keys()]) if (key.startsWith(gone)) games.delete(key);
check(!db.rows("SELECT 1 FROM summary_pairs WHERE id = ?", gone).length, "删除时马上从 summary_pairs 删掉这个编号");
fakeNow = RealDate.parse("2026-11-20T01:07:00Z");
runs = 0;
do {
  await cron(env);
  runs += 1;
  fakeNow += 3600 * 1000;
} while (state(db).todo.length && runs < 10);
check(runs === Math.ceil(goneDays / 4), `只重建这个编号打过的 ${goneDays} 天（${runs} 次）`);
check(!db.rows("SELECT 1 FROM summary_days WHERE instr(body, ?) > 0", gone).length && !db.rows("SELECT 1 FROM summary WHERE name = 'v1_dirty'").length,
      "各天的部分和里也没有这个编号了，删除标记清掉了");
db.db.prepare("DELETE FROM summary WHERE name = 'v1'").run();  // 让下一次 GET 现出一次
const after = JSON.stringify({ ...JSON.parse(await (await worker.fetch(new Request("https://stats.test/v1/summary"), env, {})).text()), made_at: null });
check(after === (await reference()), "删除以后的汇总和从头建的一字不差");

// ---------- 核对：某天的部分和被改错，UTC 12 点以后那一次发现并换掉 ----------
const day = "2026-11-12";
const body = JSON.parse(db.rows("SELECT body FROM summary_days WHERE day = ?", day)[0].body);
const someone = Object.keys(body.p)[0];
Object.values(body.p[someone])[0].s[1] += 2;
db.db.prepare("UPDATE summary_days SET body = ? WHERE day = ?").run(JSON.stringify(body), day);
const s = state(db);
db.db.prepare("UPDATE summary SET body = ? WHERE name = 'v1_days'").run(JSON.stringify({ ...s, checked: "2026-11-08", verified: "" }));
fakeNow = RealDate.parse("2026-11-20T11:07:00Z");
await cron(env);
check(state(db).fixed === 0, "UTC 12 点以前不核对");
fakeNow = RealDate.parse("2026-11-20T12:07:00Z");
await cron(env);
check(state(db).fixed === 1 && state(db).checked === day, `12 点以后核对了 ${day}，发现不对、换掉了（fixed = 1）`);
await cron(env);
check(state(db).checked === day, "同一天只核对一次");
db.db.prepare("DELETE FROM summary WHERE name = 'v1'").run();
const fixed = JSON.stringify({ ...JSON.parse(await (await worker.fetch(new Request("https://stats.test/v1/summary"), env, {})).text()), made_at: null });
check(fixed === (await reference()), "换掉以后的汇总和从头建的一字不差");

console.log(failed ? `\n${failed} 项不通过` : "\n全部通过");
process.exit(failed ? 1 : 0);
