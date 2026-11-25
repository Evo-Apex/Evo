// 定时从 Liquipedia 抓数据（2026-11-18 第 3 步）。Worker 的定时任务每分钟叫一次 tick()，每次最多发 1 个请求。
//
// 抓什么：网页上"收录的比赛"（Liquipedia 比赛页面名）的资料和参赛名单 → 名单里的人加上选手库里填了 Liquipedia 页面名的人，
// 抓他们的资料、队伍经历、历届成绩 → 他们现在的队和参赛队伍的战队资料。一轮排成一串步骤（queue），一步一个请求。
// 额度：两次请求至少隔 50 秒、一小时最多 50 次。2026-11-25 用户给协作网页换了一个专用的 Key（额度单独算，一小时 60 次；
// 原来和派圈的定时器共用一个 Key，那时是隔 2.5 分钟、一小时最多 20 次，一轮 361 人要抓五六个小时），留 10 次给本机调试、出错重试。
// 历届成绩（2026-11-25）：一个人第一次抓全部成绩，之后每轮只问最近 RECENT_DAYS 天的（一次 RECENT_BATCH 人），
// 全部成绩隔 FULL_EVERY 重抓一次（一轮最多 FULL_STALE_MAX 人，摊开来抓）。原来每轮把每个人的全部成绩（3 万多条，一次 5 人）
// 重抓一遍，占一轮请求的七成，条款也要求"不要反复要同样的数据"。
// 一批太大：Liquipedia 回 414（网址太长）、或者一次给满了 1000 行，就拆成两半重查（不算失败）。
// 一轮抓完歇 24 小时；页面上"现在更新"只是让下一轮马上开始，不会多发请求。
// 失败：一步连着 3 次不成就跳过（旧数据留着，宁可旧、不可编），记在错误里。发请求之前先把"试了几次"写下来：
// 万一这一次 Worker 被掐断（免费版每次运行的计算时间很短），下次也知道试过，不会一直重试同一步、白白用掉额度。
//
// 存在 evo-team 的 lp/ 下（和手动的 manual/ 分开，谁也不改谁的）：
//   lp/tracked.json   {"items": [{"page", "addedBy", "addedAt"}]}：收录的比赛（网页上加、删）
//   lp/state.json     进度：queue、round、recent（最近一小时发请求的时刻）、attempts、errors、lastRequestAt、lastRoundDone、refresh
//   lp/index.json     {"events": {页面名: 比赛资料 + teams/players 数}, "players": {页面名: 选手资料}, "teams": {页面名: 战队资料}}
//                     没找到的写 {"page", "missing": true}
//   lp/squads.json    {页面名: [队伍经历]}
//   lp/event/<页面名>.json    一场比赛的参赛名单
//   lp/results/<页面名>.json  一位选手的历届成绩（页面名用 encodeURIComponent）
//   manual/lp_rejects.json    待确认里点了"不是"的（选手 → Liquipedia 页面名）

import { Refuse, readDoc, updateDoc } from "./store.js";
import { players as manualPlayers, writeLog } from "./data.js";
import { squash } from "./esports.js";
import { conflicts, merged, reconcile } from "./overrides.js";
import {
  fetchPlayers, fetchResults, fetchRoster, fetchSquads, fetchTeams, fetchTournaments, pageFromInput, resultKey, teamPage,
} from "./liquipedia.js";

export const TRACKED = "lp/tracked.json";
export const STATE = "lp/state.json";
export const INDEX = "lp/index.json";
export const SQUADS = "lp/squads.json";
export const REJECTS = "manual/lp_rejects.json";
export const eventKey = (page) => `lp/event/${encodeURIComponent(page)}.json`;
export const resultsKey = (page) => `lp/results/${encodeURIComponent(page)}.json`;

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
export const MIN_GAP = 50 * 1000;  // 定时任务每分钟一次，留几秒误差（隔 60 秒整的两次可能差一点点）
export const HOURLY_CAP = 50;
export const CYCLE = 24 * HOUR;
export const MAX_ATTEMPTS = 3;
export const PAUSE_429 = 15 * MINUTE;  // Liquipedia 说请求太频繁（429）：歇这么久再来，这一次不算"试过"
const BATCH = 100;          // 比赛、选手资料、队伍经历、战队：一次问这么多页（原来 40；经历 100 人约 530 行，最多给 1000 行）
const RESULT_BATCH = 5;     // 一个人的全部成绩：一次 5 人（一人最多 300 多条）
const RECENT_BATCH = 25;    // 只问最近的成绩：一次 25 人（每人 4 个条件，再多网址太长；30 天里一人不过十几条）
export const RECENT_DAYS = 30;
export const FULL_EVERY = 7 * 24 * HOUR;  // 一个人的全部成绩隔这么久重抓一次（补上老比赛后来改的名次、奖金）
export const FULL_STALE_MAX = 60;          // 一轮里最多给这么多"到期了"的人重抓全部成绩（第一次抓的人不算在内）
const MAX_TRACKED = 20;
const KEEP_ERRORS = 20;

const emptyTracked = () => ({ items: [] });
const emptyState = () => ({ queue: [], round: null, recent: [], attempts: {}, errors: [], lastRequestAt: 0, lastRoundDone: 0,
                            refresh: false });
const emptyIndex = () => ({ events: {}, players: {}, teams: {} });
const emptyMap = () => ({});
const emptyRejects = () => ({ items: [] });

export async function tracked(bucket) {
  return (await readDoc(bucket, TRACKED, emptyTracked)).data.items;
}

export async function state(bucket) {
  return (await readDoc(bucket, STATE, emptyState)).data;
}

export async function index(bucket) {
  return (await readDoc(bucket, INDEX, emptyIndex)).data;
}

export async function squadsOf(bucket) {
  return (await readDoc(bucket, SQUADS, emptyMap)).data;
}

export async function resultsOf(bucket, page) {
  return (await readDoc(bucket, resultsKey(page), emptyMap)).data.rows ?? [];
}

export async function rejects(bucket) {
  return (await readDoc(bucket, REJECTS, emptyRejects)).data.items;
}

// ---------- 收录的比赛（网页上加、删） ----------

export async function addTracked(bucket, by, input, now) {
  const page = pageFromInput(input);
  if (!page) throw new Refuse("看不懂这个链接：贴 Liquipedia 比赛页的地址（https://liquipedia.net/apexlegends/…），或者页面名");
  await updateDoc(bucket, TRACKED, emptyTracked, (data) => {
    if (data.items.some((x) => x.page === page)) throw new Refuse(`已经收录了 ${page}`);
    if (data.items.length >= MAX_TRACKED) throw new Refuse(`最多收录 ${MAX_TRACKED} 场比赛，先删掉几场不用的`);
    data.items.push({ page, addedBy: by.name, addedAt: now });
  });
  await requestRefresh(bucket);
  await writeLog(bucket, by, now, `收录 Liquipedia 比赛 ${page}`);
  return page;
}

export async function removeTracked(bucket, by, page, now) {
  await updateDoc(bucket, TRACKED, emptyTracked, (data) => {
    if (!data.items.some((x) => x.page === page)) throw new Refuse("没有收录这场比赛（可能刚被别人删了）");
    data.items = data.items.filter((x) => x.page !== page);
  });
  await writeLog(bucket, by, now, `不再收录 Liquipedia 比赛 ${page}（抓过的资料留着）`);
}

// "现在更新"：下一次定时任务就开始新的一轮（正在抓的那一轮照常抓完）
export async function requestRefresh(bucket) {
  await updateDoc(bucket, STATE, emptyState, (s) => {
    s.refresh = true;
  });
}

// ---------- 一轮的步骤 ----------

function chunks(list, size) {
  const out = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

const stepId = (step) => `${step.kind}:${(step.pages ?? [step.page]).join("|")}`.slice(0, 300);

// 开始的几步：比赛资料（一批一问）、每场比赛的名单，然后"排选手"（不发请求，看名单里有谁再排后面的）
function firstSteps(pages) {
  return [...chunks(pages, BATCH).map((p) => ({ kind: "tournaments", pages: p })), ...pages.map((page) => ({ kind: "roster", page })),
          { kind: "plan-players" }];
}

// 历届成绩这一轮怎么抓（2026-11-25）：还没抓过全部成绩的 → 全部；全部成绩抓了超过 FULL_EVERY 的 → 全部（最久没抓的先，
// 一轮最多 FULL_STALE_MAX 人）；别的 → 只问最近 RECENT_DAYS 天的。idx：lp/index.json（results 是抓过的条数，resultsFullAt 是哪次抓的全部）
export function resultPlan(pages, idx, now) {
  const full = [];
  const stale = [];
  const recent = [];
  for (const page of pages) {
    const p = idx.players?.[page];
    if (typeof p?.results !== "number") full.push(page);
    else if (now - (p.resultsFullAt ?? 0) >= FULL_EVERY) stale.push(page);
    else recent.push(page);
  }
  stale.sort((a, b) => (idx.players[a].resultsFullAt ?? 0) - (idx.players[b].resultsFullAt ?? 0));
  return { full: [...full, ...stale.slice(0, FULL_STALE_MAX)], recent: [...recent, ...stale.slice(FULL_STALE_MAX)] };
}

const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);

// 名单里的人 + 选手库里填了页面名的人 → 资料、经历、成绩；最后"排战队"
async function planPlayers(bucket, trackedPages, now) {
  const pages = new Set((await manualPlayers(bucket)).map((p) => p.liquipedia).filter(Boolean));
  for (const page of trackedPages) {
    const event = (await readDoc(bucket, eventKey(page), emptyMap)).data;
    for (const team of event.teams ?? []) for (const p of team.players) pages.add(p.page);
  }
  const list = [...pages];
  const plan = resultPlan(list, await index(bucket), now);
  const since = isoDay(now - RECENT_DAYS * 24 * HOUR);  // 只问这一天以后的成绩（不含这一天）
  return [...chunks(list, BATCH).map((p) => ({ kind: "players", pages: p })), ...chunks(list, BATCH).map((p) => ({ kind: "squads", pages: p })),
          ...chunks(plan.full, RESULT_BATCH).map((p) => ({ kind: "results", pages: p })),
          ...chunks(plan.recent, RECENT_BATCH).map((p) => ({ kind: "recent", pages: p, since })), { kind: "plan-teams" }];
}

// 参赛的队 + 选手现在的队 → 战队资料
async function planTeams(bucket, idx, trackedPages) {
  const pages = new Set();
  for (const page of trackedPages) {
    const event = (await readDoc(bucket, eventKey(page), emptyMap)).data;
    for (const team of event.teams ?? []) pages.add(teamPage(team.team));
  }
  for (const p of Object.values(idx.players)) if (p.teamPage) pages.add(p.teamPage);
  return chunks([...pages], BATCH).map((p) => ({ kind: "teams", pages: p }));
}

// 一批拆成两半（给满了、网址太长）
function halves(step) {
  const half = Math.ceil(step.pages.length / 2);
  return [{ ...step, pages: step.pages.slice(0, half) }, { ...step, pages: step.pages.slice(half) }];
}

// 发一个请求做一步 → {ok, error, replace（换掉这一步的新步骤）}；数据写进存储桶
async function runStep(bucket, step, deps, now) {
  if (step.kind === "tournaments") {
    const r = await fetchTournaments(step.pages, deps);
    if (!r.ok) return r;
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) {
        idx.events[page] = r.found[page] ? { ...idx.events[page], ...r.found[page], missing: false, fetchedAt: now }
          : { ...idx.events[page], page, missing: true, fetchedAt: now };
      }
    });
    return { ok: true };
  }
  if (step.kind === "roster") {
    const r = await fetchRoster(step.page, deps);
    if (!r.ok) return r;
    await bucket.put(eventKey(step.page), JSON.stringify({ page: step.page, teams: r.teams, fetchedAt: now }));
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      idx.events[step.page] = { page: step.page, ...idx.events[step.page], rosterTeams: r.teams.length,
                                rosterPlayers: r.teams.reduce((n, t) => n + t.players.length, 0), rosterAt: now };
    });
    return { ok: true };
  }
  if (step.kind === "players" || step.kind === "teams") {
    const r = await (step.kind === "players" ? fetchPlayers : fetchTeams)(step.pages, deps);
    if (!r.ok) return r;
    const field = step.kind === "players" ? "players" : "teams";
    const fresh = await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) {
        // 成绩是另外几步抓的：条数和哪次抓的全部成绩照留着
        const old = idx[field][page];
        idx[field][page] = r.found[page] ? { ...r.found[page], results: old?.results, resultsFullAt: old?.resultsFullAt, fetchedAt: now }
          : { page, missing: true, results: old?.results, resultsFullAt: old?.resultsFullAt, fetchedAt: now };
      }
      return idx;
    });
    if (step.kind === "players") await reconcile(bucket, fresh, step.pages, now);  // 手动修正对账（overrides.js）
    return { ok: true };
  }
  if (step.kind === "squads") {
    const r = await fetchSquads(step.pages, deps);
    if (!r.ok) return r;
    if (r.full && step.pages.length > 1) return { ok: true, replace: halves(step) };  // 给满了：可能截掉了，拆成两半重查
    await updateDoc(bucket, SQUADS, emptyMap, (all) => Object.assign(all, r.squads));
    return { ok: true };
  }
  if (step.kind === "results") {
    const r = await fetchResults(step.pages, deps);
    if (!r.ok) return r;
    if (r.full && step.pages.length > 1) return { ok: true, replace: halves(step) };  // 给满了：可能截掉了老比赛，拆成两半重查
    for (const page of step.pages) {
      await bucket.put(resultsKey(page), JSON.stringify({ page, rows: r.results[page], full: r.full, fetchedAt: now, fullAt: now }));
    }
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) {
        if (!idx.players[page]) continue;
        idx.players[page].results = r.results[page].length;
        idx.players[page].resultsFullAt = now;
      }
    });
    return { ok: true };
  }
  if (step.kind === "recent") {  // 只问 since 以后的成绩：换掉存着的 since 以后那一段，之前的照留（2026-11-25）
    const r = await fetchResults(step.pages, deps, step.since);
    if (!r.ok) return r;
    if (r.full) {  // 给满了：拆成两半；一个人 30 天里给满了 1000 条不可能，真遇到就重抓他的全部成绩
      return { ok: true, replace: step.pages.length > 1 ? halves(step) : [{ kind: "results", pages: step.pages }] };
    }
    const counts = {};
    for (const page of step.pages) {
      const old = (await readDoc(bucket, resultsKey(page), emptyMap)).data;
      const fresh = r.results[page];
      const keys = new Set(fresh.map(resultKey));
      const kept = (old.rows ?? []).filter((x) => !(String(x.date ?? "") > step.since) && !keys.has(resultKey(x)));
      const rows = [...fresh, ...kept].sort((a, b) => String(b.date ?? "").localeCompare(String(a.date ?? "")));
      counts[page] = rows.length;
      await bucket.put(resultsKey(page), JSON.stringify({ page, rows, full: Boolean(old.full), fetchedAt: now, fullAt: old.fullAt ?? old.fetchedAt ?? null }));
    }
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) if (idx.players[page]) idx.players[page].results = counts[page];
    });
    return { ok: true };
  }
  return { ok: false, error: `不认识的步骤 ${step.kind}` };
}

function addError(s, now, step, text) {
  s.errors = [{ at: now, step: stepId(step), text }, ...s.errors].slice(0, KEEP_ERRORS);
}

// 定时任务每次叫一次。返回做了什么（测试、日志用）：idle 没到时候 / nothing 没有要抓的 / nokey 没配 Key /
// wait 离上次请求太近、这一小时用完了或者在歇着 / done 做了一步 / failed 这一步没成 / paused Liquipedia 说太频繁、歇 15 分钟 /
// skipped 试了 3 次跳过
export async function tick(env, now, fetchImpl = fetch) {
  const bucket = env.DATA;
  const s = await state(bucket);
  s.recent = s.recent.filter((t) => now - t < HOUR);
  // 写回进度。"现在更新"是页面上另外写进来的：这一次开始了新的一轮就清掉它，别的时候照留（抓的时候又有人点了）
  let tookRefresh = false;
  const save = () => updateDoc(bucket, STATE, emptyState, (fresh) => {
    Object.assign(fresh, s, { refresh: tookRefresh ? false : Boolean(fresh.refresh || s.refresh) });
  });

  if (!s.queue.length) {
    const due = s.refresh || !s.lastRoundDone || now - s.lastRoundDone >= CYCLE;
    if (!due) return "idle";
    const trackedPages = (await tracked(bucket)).map((x) => x.page);
    const manual = (await manualPlayers(bucket)).some((p) => p.liquipedia);
    s.refresh = false;
    tookRefresh = true;
    if (!trackedPages.length && !manual) {
      s.lastRoundDone = now;
      await save();
      return "nothing";
    }
    s.queue = firstSteps(trackedPages);
    s.round = { started: now, done: 0, total: s.queue.length };
    s.attempts = {};
  }
  if (!String(env.LIQUIPEDIA_API_KEY ?? "").trim()) {
    await save();
    return "nokey";
  }

  // 不发请求的"排一排"步骤先做掉
  while (s.queue.length && s.queue[0].kind.startsWith("plan-")) {
    const step = s.queue.shift();
    const trackedPages = (await tracked(bucket)).map((x) => x.page);
    const more = step.kind === "plan-players" ? await planPlayers(bucket, trackedPages, now) : await planTeams(bucket, await index(bucket), trackedPages);
    s.queue.unshift(...more);
    s.round.total += more.length - 1;
    s.round.done += 1;
  }
  if (!s.queue.length) return finishRound(s, now, save);
  if (now - s.lastRequestAt < MIN_GAP || s.recent.length >= HOURLY_CAP || now < (s.pausedUntil ?? 0)) {
    await save();
    return "wait";
  }

  const step = s.queue[0];
  const id = stepId(step);
  s.attempts[id] = (s.attempts[id] ?? 0) + 1;
  if (s.attempts[id] > MAX_ATTEMPTS) {  // 前几次都没成（或者运行到一半被掐断了）：跳过
    s.queue.shift();
    s.round.done += 1;
    addError(s, now, step, `试了 ${MAX_ATTEMPTS} 次都没成，跳过（上次抓到的照旧留着）`);
    delete s.attempts[id];
    if (!s.queue.length) return finishRound(s, now, save, "skipped");
    await save();
    return "skipped";
  }
  s.lastRequestAt = now;
  s.recent.push(now);
  await save();  // 先记下"试过了"再发请求

  let result = await runStep(bucket, step, { apiKey: env.LIQUIPEDIA_API_KEY, fetchImpl }, now);
  if (!result.ok && result.status === 414 && step.pages?.length > 1) result = { ok: true, replace: halves(step) };  // 网址太长：拆小重查
  if (!result.ok) {
    addError(s, now, step, result.error);
    if (result.status === 429) {
      s.pausedUntil = now + PAUSE_429;
      s.attempts[id] -= 1;
    }
    await save();
    return result.status === 429 ? "paused" : "failed";
  }
  s.queue.shift();
  delete s.attempts[id];
  if (result.replace) {
    s.queue.unshift(...result.replace);
    s.round.total += result.replace.length - 1;
  } else {
    s.round.done += 1;
  }
  if (!s.queue.length) return finishRound(s, now, save);
  await save();
  return "done";
}

async function finishRound(s, now, save, outcome = "done") {
  s.lastRoundDone = now;
  if (s.round) s.round.finished = now;
  await save();
  return outcome;
}

// ---------- 待确认：选手库的人和 Liquipedia 对上 ----------

// 比名字时不管大小写、空格、下划线和符号
export function nameKey(text) {
  return squash(text).toLowerCase().replace(/[\s_\-.·'"()（）]/g, "");
}

// 选手库里没填 Liquipedia 页面名的人：名字和抓到的某位选手的 ID 一样（点过"不是"的不算；ID 有手动修正的按修正的比）→ 建议
// 填了页面名、Liquipedia 上却没有这个页面的 → 也列出来；手动修正过、Liquipedia 后来又改成别的 → conflicts
// manualPros：手动建的选手资料（manualpros.js）：Liquipedia 上没有页面、但手动建了资料的，不再列出来
export function pending(manual, idx, rejected, ov = {}, manualPros = {}) {
  const byKey = new Map();
  for (const raw of Object.values(idx.players)) {
    if (raw.missing) continue;
    const p = merged(raw, ov[raw.page]);
    const key = nameKey(p.id);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(p);
  }
  const no = new Set(rejected.map((x) => `${x.player}|${x.page}`));
  const suggestions = [];
  const missing = [];
  for (const m of manual) {
    if (m.liquipedia) {
      if (idx.players[m.liquipedia]?.missing && !manualPros[m.liquipedia]) missing.push(m);
      continue;
    }
    const candidates = (byKey.get(nameKey(m.name)) ?? []).filter((p) => !no.has(`${m.id}|${p.page}`));
    if (candidates.length) suggestions.push({ player: m, candidates });
  }
  const clashes = conflicts(ov).map((c) => ({ ...c, player: idx.players[c.page] }));
  return { suggestions, missing, conflicts: clashes };
}

export function pendingCount(pend) {
  return pend.suggestions.length + pend.missing.length + (pend.conflicts?.length ?? 0);
}

// 点"是他"：把页面名填进选手库（手动的那一份，记谁点的）
export async function confirmMatch(bucket, by, playerId, page, now) {
  const idx = await index(bucket);
  if (!idx.players[page] || idx.players[page].missing) throw new Refuse("Liquipedia 上没有这个选手（可能刚更新过），请刷新页面再看");
  let name = "";
  await updateDoc(bucket, "manual/players.json", () => ({ version: 1, items: [] }), (list) => {
    const p = list.items.find((x) => x.id === playerId);
    if (!p) throw new Refuse("选手库里没有这个人了（可能刚被删了）");
    if (p.liquipedia) throw new Refuse(`「${p.name}」已经填了 Liquipedia 页面名 ${p.liquipedia}`);
    p.liquipedia = page;
    p.updated = now;
    p.updatedBy = by.name;
    p.rev = (p.rev ?? 0) + 1;
    name = p.name;
  });
  await writeLog(bucket, by, now, `确认选手 ${name} 就是 Liquipedia 上的 ${page}`);
}

// 点"不是"：记下来，以后不再建议这一对
export async function rejectMatch(bucket, by, playerId, page, now) {
  const manual = (await manualPlayers(bucket)).find((x) => x.id === playerId);
  if (!manual) throw new Refuse("选手库里没有这个人了（可能刚被删了）");
  await updateDoc(bucket, REJECTS, emptyRejects, (data) => {
    if (!data.items.some((x) => x.player === playerId && x.page === page)) data.items.push({ player: playerId, page, by: by.name, at: now });
  });
  await writeLog(bucket, by, now, `选手 ${manual.name} 不是 Liquipedia 上的 ${page}`);
}

// 页面上"这一轮"的进度、这一小时用了几次
export function progress(s, now) {
  const recent = (s.recent ?? []).filter((t) => now - t < HOUR).length;
  const running = s.queue?.length > 0;
  const left = running ? s.queue.filter((x) => !x.kind.startsWith("plan-")).length : 0;
  const next = !running && s.lastRoundDone ? (s.refresh ? now : s.lastRoundDone + CYCLE) : null;
  return { recent, running, done: s.round?.done ?? 0, total: s.round?.total ?? 0, left, hoursLeft: Math.ceil((left * 3) / 60 * 10) / 10,
           next, lastRoundDone: s.lastRoundDone, errors: s.errors ?? [], refresh: Boolean(s.refresh), pausedUntil: s.pausedUntil ?? 0 };
}

