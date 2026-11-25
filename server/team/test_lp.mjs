// Liquipedia 定时抓取的测试（第 3 步）。由 test.mjs 运行。用假的 Liquipedia 接口（不联网），把一整轮走一遍：
// 没配 Key 不发请求、每次最多一个请求、两次至少隔 50 秒、一小时最多 50 次（2026-11-25 换成专用的 Key 以前是 2.5 分钟、20 次）、
// 一轮的步骤和数据、给满了拆成两半重查、
// 失败重试和 3 次跳过（含"发请求以后被掐断"）、24 小时一轮和"现在更新"、收录比赛、待确认（是他 / 不是）、页面、权限；
// 2026-11-25：历届成绩第一次和每周抓全部、平时只问最近 30 天（合并：换掉最近那一段、之前的照留），网址太长（414）、经历给满了拆小重查。

import { hash, newToken, update } from "./auth.js";
import { PLAYERS } from "./data.js";
import { eventRoster, pageFromInput } from "./liquipedia.js";
import { FULL_STALE_MAX, HOURLY_CAP, INDEX, MIN_GAP, SQUADS, STATE, TRACKED, eventKey, resultPlan, resultsKey, tick } from "./lpsync.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
let now = Date.UTC(2026, 10, 18, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const doc = (key, fallback = "{}") => env.DATA.json(key, fallback);

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: false, created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

// ---------- 假的 Liquipedia ----------
const EVENT = "ALGS/2026/Split_2/Playoffs";
const calls = [];
let failType = null;
let failStatus = 500;
let tooLongType = null;  // 这种请求一次问不止一页就回 414（网址太长）
let squadFlood = false;  // 经历一次问不止一页就给满 1000 行
function api(url, options) {
  const u = new URL(url);
  const type = u.pathname.split("/").pop();
  const cond = u.searchParams.get("conditions") ?? "";
  calls.push({ type, cond, url, headers: options.headers });
  if (failType === type) return Promise.resolve(new Response("down", { status: failStatus }));
  if (tooLongType === type && (cond.match(/::/g) ?? []).length > 1) return Promise.resolve(new Response("too long", { status: 414 }));
  if (type === "squadplayer" && squadFlood && (cond.match(/::/g) ?? []).length > 1) {
    const rows = Array.from({ length: 1000 }, (_, i) => ({ pagename: `T${i}`, link: "Alpha", joindate: "2024-01-01", status: "former" }));
    return Promise.resolve(new Response(JSON.stringify({ result: rows }), { status: 200 }));
  }
  const has = (page) => cond.includes(`::${page}]]`);
  let result = [];
  if (type === "tournament") {
    if (has(EVENT)) result.push({ pagename: EVENT, name: "ALGS Split 2 Playoffs", startdate: "2026-11-20", enddate: "2026-11-23",
                                  liquipediatier: "1", participantsnumber: 40, prizepool: 1000000 });
  } else if (type === "placement" && cond.startsWith("[[parent::")) {
    if (has(EVENT)) {
      result = [
        { opponenttype: "team", opponentname: "Team Falcons", placement: "2", opponentplayers: { p1: "Alpha", p2: "Beta_(player)", p3: "Gamma", p2dn: "Beta" } },
        { opponenttype: "team", opponentname: "Wolves", placement: "1", opponentplayers: { p1: "Kite_(player)", p1dn: "Kite" } },
        { opponenttype: "team", opponentname: "TBD", placement: "", opponentplayers: { p1: "Nobody" } },
        { opponenttype: "solo", opponentname: "MVP", mode: "individual award", opponentplayers: { p1: "Alpha" } },
      ];
    }
  } else if (type === "player") {
    for (const [page, id, team] of [["Alpha", "Alpha", "Team_Falcons"], ["Beta_(player)", "Beta", "Team_Falcons"], ["Gamma", "Gamma", ""],
                                    ["Kite_(player)", "Kite", "Wolves_Esports"]]) {
      if (has(page)) result.push({ pagename: page, id, name: `${id} 真名`, nationality: "China", teampagename: team, birthdate: "0000-01-01",
                                   extradata: { input: "Controller", signatureLegend1: "Wraith" } });
    }
  } else if (type === "squadplayer") {
    if (cond.includes("::Beta (player)]]")) result.push({ pagename: "Team_Falcons", link: "Beta (player)", joindate: "2025-01-01", status: "active" });
    if (cond.includes("::Alpha]]")) {
      result.push({ pagename: "Team_Falcons", link: "Alpha", joindate: "2024-02-01", status: "active" });
      result.push({ pagename: "Team_Falcons", link: "Alpha", joindate: "2024-02-01", status: "active" });  // 重复的一行
    }
  } else if (type === "placement") {
    const pages = (cond.match(/opponentplayers_p1::/g) ?? []).length;
    if (pages > 2) {  // 一批人太多：给满 1000 行
      result = Array.from({ length: 1000 }, (_, i) => ({ parent: `E${i}`, placement: "1", opponentname: "X", opponentplayers: { p1: "Alpha" } }));
    } else {
      if (has("Alpha")) result.push({ parent: EVENT, tournament: "ALGS", date: "2026-11-23", placement: "2", prizemoney: 50000,
                                       liquipediatier: "1", opponentname: "Team Falcons", opponentplayers: { p1: "Alpha" } });
      if (has("Kite_(player)")) result.push({ parent: "Old_Cup", date: "2025-05-01", placement: "", opponentplayers: { p1: "Kite_(player)" } });
    }
  } else if (type === "team") {
    for (const [page, name] of [["Team_Falcons", "Team Falcons"], ["Wolves_Esports", "Wolves Esports"]]) {
      if (has(page)) result.push({ pagename: page, name, region: "Asia-Pacific", status: "active", earnings: 12345 });
    }
  }
  return Promise.resolve(new Response(JSON.stringify({ result }), { status: 200 }));
}
const run = () => tick(env, now, api);

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const viewer = await join("看看", "viewer");

// ---------- 小工具 ----------
check("链接 → 页面名（去掉 ? # 和结尾的 /，解开 %28）", pageFromInput("https://liquipedia.net/apexlegends/Panic_%28American_player%29/?x=1#a") === "Panic_(American_player)");
check("直接写页面名也认、空格换下划线", pageFromInput("ALGS 2026") === "ALGS_2026");
check("别的网站的链接不认", pageFromInput("https://example.com/apexlegends/x") === null);
const roster = eventRoster([{ opponenttype: "team", opponentname: "B", placement: "", opponentplayers: { p1: "b" } },
  { opponenttype: "team", opponentname: "A", placement: "3", opponentplayers: { p1: "a" } },
  { opponenttype: "team", opponentname: "TBD", placement: "1", opponentplayers: { p1: "t" } }]);
check("名单：按名次排、没名次的放后面、TBD 不算", roster.map((t) => t.team).join() === "A,B");

// ---------- 收录比赛、权限 ----------
let r = await viewer.post("/liquipedia/track", { page: EVENT });
check("只读的不能收录", r.status === 403);
r = await editor.post("/liquipedia/track", { page: `https://liquipedia.net/apexlegends/${EVENT}?useskin=x#Results` });
check("贴链接收录", r.location === "/liquipedia?ok=tracked" && doc("lp/tracked.json").items[0].page === EVENT);
r = await editor.post("/liquipedia/track", { page: EVENT });
check("收录过的再收录 → 提示", r.status === 400 && r.text.includes("已经收录了"));
r = await editor.post("/liquipedia/track", { page: "https://example.com/x" });
check("看不懂的链接 → 提示", r.status === 400 && r.text.includes("看不懂这个链接"));
await editor.post("/liquipedia/track", { page: "No_Such_Event" });

// 选手库：Alpha（没填页面名，名字对得上）、gamma（大小写不一样）、Kite（填了）、Ghost（填了个不存在的）
for (const [name, team, room, liquipedia] of [["Alpha", "猎鹰", "1", ""], ["gamma", "猎鹰", "2", ""], ["Kite", "狼队", "3", "Kite_(player)"],
                                                ["Ghost", "狼队", "4", "NoSuchPage"]]) {
  await editor.post("/players/add", { name, team, platform: "douyu", room, liquipedia });
}

// ---------- 没配 Key ----------
r = await run();
check("没配 Key：不发请求", r === "nokey" && calls.length === 0);
r = await viewer.get("/liquipedia");
check("页面上写着还没配 Key、写明数据来源", r.text.includes("还没配 Liquipedia 的 Key") && r.text.includes("CC BY-SA 3.0"));

// ---------- 一整轮 ----------
env.LIQUIPEDIA_API_KEY = "test-key";
r = await run();
check("第一步：问比赛资料", r === "done" && calls.length === 1 && calls[0].type === "tournament");
check("请求带 Key、写明是谁在用、问的是 Apex 维基", calls[0].headers.Authorization === "Apikey test-key"
  && calls[0].headers["User-Agent"].startsWith("EvoTeam/") && calls[0].url.includes("wiki=apexlegends"));
r = await run();
check("马上再叫一次 → 等着（两次至少隔 50 秒）", r === "wait" && calls.length === 1);
now += 40 * 1000;
check("隔 40 秒 → 还等着", (await run()) === "wait" && calls.length === 1);
now -= 40 * 1000;
const outcomes = [];
for (let i = 0; i < 40 && doc(STATE).queue?.length !== 0; i++) {
  now += 3 * MINUTE;
  outcomes.push(await run());
}
const types = calls.map((c) => c.type).join(",");
check("一轮：比赛、两份名单、选手、经历、成绩（给满了一层层拆成两半：5 → 3+2 → 2+1+2）、战队", types
  === "tournament,placement,placement,player,squadplayer,placement,placement,placement,placement,placement,team", types);
check("每次只发一个请求", outcomes.every((o) => o === "done") && outcomes.length === calls.length - 1);
let idx = doc(INDEX);
check("比赛资料、名单人数；没有的页面标出来", idx.events[EVENT].name === "ALGS Split 2 Playoffs" && idx.events[EVENT].rosterTeams === 2
  && idx.events[EVENT].rosterPlayers === 4 && idx.events.No_Such_Event.missing === true);
check("名单存下来（TBD、个人奖项不算）", doc(eventKey(EVENT)).teams.map((t) => t.team).join() === "Wolves,Team Falcons");
check("选手资料（没生日不编、常用传奇、输入设备）", idx.players.Alpha.id === "Alpha" && idx.players.Alpha.birth === ""
  && idx.players.Alpha.legends[0] === "Wraith" && idx.players.Alpha.input === "Controller");
check("Liquipedia 上没有的页面名标出来", idx.players.NoSuchPage.missing === true);
check("队伍经历去掉重复的", doc(SQUADS).Alpha.length === 1 && doc(SQUADS)["Beta_(player)"][0].team === "Team Falcons");
check("成绩：没名次的不算", doc(resultsKey("Alpha")).rows.length === 1 && doc(resultsKey("Kite_(player)")).rows.length === 0
  && idx.players.Alpha.results === 1);
check("战队资料（参赛的队 + 选手现在的队）", idx.teams.Team_Falcons.region === "Asia-Pacific" && idx.teams.Wolves_Esports.earnings === 12345);
let s = doc(STATE);
check("一轮抓完：记下时间", s.queue.length === 0 && s.lastRoundDone === now && s.round.finished === now);

// ---------- 24 小时一轮、现在更新 ----------
now += 3 * MINUTE;
check("抓完了 → 24 小时内不再抓", (await run()) === "idle" && calls.length === 11);
r = await viewer.post("/liquipedia/refresh");
check("只读的不能点现在更新", r.status === 403);
r = await editor.post("/liquipedia/refresh");
check("现在更新 → 排上", r.location === "/liquipedia?ok=refresh" && doc(STATE).refresh === true);
now += 3 * MINUTE;
check("下一次就开始新的一轮", (await run()) === "done" && calls.length === 12 && doc(STATE).refresh === false);

// ---------- 额度、失败、跳过 ----------
const st = doc(STATE);
st.recent = Array.from({ length: HOURLY_CAP }, (_, i) => now - i * MINUTE);
st.lastRequestAt = now - 10 * MINUTE;
await env.DATA.put(STATE, JSON.stringify(st));
now += 3 * MINUTE;
check(`这一小时用满 ${HOURLY_CAP} 次 → 等着`, (await run()) === "wait" && calls.length === 12);
st.recent = [];
await env.DATA.put(STATE, JSON.stringify(st));
failType = "placement";
failStatus = 429;
now += 3 * MINUTE;
const sent429 = calls.length;
check("Liquipedia 说太频繁（429）→ 歇着", (await run()) === "paused" && doc(STATE).pausedUntil === now + 15 * MINUTE);
check("429 那一次不算试过", Object.values(doc(STATE).attempts).every((n) => n === 0));
now += 3 * MINUTE;
check("歇着的时候不发请求", (await run()) === "wait" && calls.length === sent429 + 1);
r = await viewer.get("/liquipedia");
check("页面上写着在歇着", r.text.includes("Liquipedia 说请求太频繁，歇到"));
now += 13 * MINUTE;
failStatus = 500;
now += 3 * MINUTE;
check("Liquipedia 出错 → 这一步没成、记下错误", (await run()) === "failed" && doc(STATE).errors[0].text.includes("HTTP 500"));
now += 3 * MINUTE;
await run();
now += 3 * MINUTE;
await run();
const before = calls.length;
now += 3 * MINUTE;
check("同一步试了 3 次都没成 → 跳过、不再发请求", (await run()) === "skipped" && calls.length === before
  && doc(STATE).errors[0].text.includes("试了 3 次"));
failType = null;
// 发请求以后被掐断：试的次数已经先写下了
const crashed = doc(STATE);
crashed.attempts = { [`roster:${crashed.queue[0].page}`]: 3 };
await env.DATA.put(STATE, JSON.stringify(crashed));
now += 3 * MINUTE;
check("被掐断过 3 次的那一步也跳过", (await run()) === "skipped");
for (let i = 0; i < 40 && doc(STATE).queue.length; i++) {
  now += 3 * MINUTE;
  await run();
}
check("跳过的几步照旧留着上次抓的", doc(eventKey(EVENT)).teams.length === 2 && doc(STATE).queue.length === 0);

// ---------- 页面、待确认 ----------
r = await viewer.get("/liquipedia");
check("页面：收录的比赛、名单人数、没有的页面", r.text.includes("ALGS Split 2 Playoffs") && r.text.includes("名单 2 支队、4 人")
  && r.text.includes("Liquipedia 上没有这个页面"));
check("页面：这一小时用了几次", /这一小时用了 \d+ 次请求/.test(r.text));
check("待确认：名字一样的建议（不管大小写）", r.text.includes("选手库的「Alpha」（猎鹰）是不是 Liquipedia 上的 Alpha")
  && r.text.includes("选手库的「gamma」（猎鹰）是不是 Liquipedia 上的 Gamma"));
check("待确认：填了不存在的页面名", r.text.includes("「Ghost」填的 Liquipedia 页面名「NoSuchPage」"));
check("只读的看不到按钮", !r.text.includes('action="/liquipedia/match"'));
r = await admin.get("/");
check("首页：抓到几位选手、几条要确认", r.text.includes("抓到 4 位选手") && r.text.includes("3 条要确认"));
const alpha = JSON.parse(env.DATA.objects.get(PLAYERS).body).items.find((x) => x.name === "Alpha");
const gamma = JSON.parse(env.DATA.objects.get(PLAYERS).body).items.find((x) => x.name === "gamma");
r = await editor.post("/liquipedia/match", { player: alpha.id, page: "Alpha", answer: "yes" });
const manualAlpha = () => JSON.parse(env.DATA.objects.get(PLAYERS).body).items.find((x) => x.name === "Alpha");
check("是他 → 选手库里填上页面名、记下谁点的", r.location === "/liquipedia?ok=matched#confirm" && manualAlpha().liquipedia === "Alpha"
  && manualAlpha().updatedBy === "Momona");
r = await editor.post("/liquipedia/match", { player: gamma.id, page: "Gamma", answer: "no" });
r = await editor.get("/liquipedia");
check("不是 → 不再建议", r.text.includes("记下了") === false && !r.text.includes("「gamma」（猎鹰）是不是"));
check("待确认少了两条", r.text.includes("待确认（1）"));
r = await editor.post("/liquipedia/match", { player: alpha.id, page: "Alpha", answer: "yes" });
check("已经填了的再点 → 提示", r.status === 400 && r.text.includes("已经填了"));
r = await editor.post("/liquipedia/untrack", { page: "No_Such_Event" });
check("不再收录", r.location === "/liquipedia?ok=untracked" && doc("lp/tracked.json").items.length === 1);
r = await viewer.get("/log");
check("改动记录：收录、确认、不是", r.text.includes(`收录 Liquipedia 比赛 ${EVENT}`) && r.text.includes("确认选手 Alpha 就是 Liquipedia 上的 Alpha")
  && r.text.includes("选手 gamma 不是 Liquipedia 上的 Gamma"));

// ---------- 抓的时候点更新：这一轮抓完马上再来一轮（11-18 用户找不到按钮） ----------
const running = doc(STATE);
Object.assign(running, { queue: [{ kind: "teams", pages: ["Team_Falcons"] }], round: { started: now, done: 5, total: 6 }, refresh: false,
                         lastRequestAt: 0, recent: [], pausedUntil: 0 });
await env.DATA.put(STATE, JSON.stringify(running));
r = await editor.get("/liquipedia");
check("抓的时候也有更新按钮", r.text.includes(">这一轮抓完马上再抓一轮</button>"));
await editor.post("/liquipedia/refresh");
r = await editor.get("/liquipedia");
check("点了以后写着已经排上、按钮收起来", r.text.includes("已经排上：这一轮抓完马上再抓一轮") && !r.text.includes(">这一轮抓完马上再抓一轮</button>"));
now += 3 * MINUTE;
check("这一轮的最后一步照常抓完、排上的还在", (await run()) === "done" && doc(STATE).queue.length === 0 && doc(STATE).refresh === true);
now += 3 * MINUTE;
check("抓完马上开始新的一轮", (await run()) === "done" && doc(STATE).refresh === false && doc(STATE).round.started === now);

// ---------- 2026-11-25：专用 Key、历届成绩只抓新的、拆小重查 ----------
const DAY = 24 * 60 * MINUTE;
check("额度：一小时最多 50 次、两次至少隔 50 秒（定时任务每分钟一次）", HOURLY_CAP === 50 && MIN_GAP === 50 * 1000);
const plan = resultPlan(["New", "Fresh", "Old1", "Old2", "Gone"], { players: {
  Fresh: { results: 3, resultsFullAt: now - DAY },
  Old1: { results: 3, resultsFullAt: now - 9 * DAY },
  Old2: { results: 3 },  // 2026-11-25 以前抓的：没记哪次抓的全部 → 当作到期
  Gone: { missing: true, results: 0, resultsFullAt: now - DAY },
} }, now);
check("成绩怎么抓：没抓过的、全部成绩超过 7 天的（最久没抓的先）抓全部，别的只问最近的", JSON.stringify(plan)
  === JSON.stringify({ full: ["New", "Old2", "Old1"], recent: ["Fresh", "Gone"] }));
const many = Array.from({ length: FULL_STALE_MAX + 3 }, (_, i) => `S${i}`);
const capped = resultPlan(many, { players: Object.fromEntries(many.map((p, i) => [p, { results: 1, resultsFullAt: i }])) }, now);
check(`到期的一轮最多重抓 ${FULL_STALE_MAX} 人（最久的先），剩下的这一轮先只问最近的`, capped.full.length === FULL_STALE_MAX
  && capped.recent.length === 3 && capped.full[0] === "S0" && capped.recent[0] === `S${FULL_STALE_MAX}`);

// 新的一份存储桶：选手库里只有 Alpha（填了页面名），没有收录的比赛；上次抓过他的全部成绩（1 天前）
async function freshEnv(manual, index, results = {}) {
  const e = { DATA: new FakeBucket(), PUBLIC: new FakeBucket(), LIQUIPEDIA_API_KEY: "test-key" };
  const stamp = { created: now, createdBy: "黄指导", updated: now, updatedBy: "黄指导", rev: 1 };
  await e.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: manual.map(([name, page], i) => ({ id: `c000000${i}`, name, team: "猎鹰",
    platform: "douyu", room: String(9000 + i), liquipedia: page, ...stamp })) }));
  await e.DATA.put(TRACKED, JSON.stringify({ items: [] }));
  await e.DATA.put(INDEX, JSON.stringify({ events: {}, teams: {}, players: index }));
  for (const [page, body] of Object.entries(results)) await e.DATA.put(resultsKey(page), JSON.stringify(body));
  await e.DATA.put(STATE, JSON.stringify({ queue: [], round: null, recent: [], attempts: {}, errors: [], lastRequestAt: 0, lastRoundDone: now, refresh: true }));
  return e;
}
async function runRound(e) {
  const from = calls.length;
  for (let i = 0; i < 40; i++) {
    now += MINUTE;
    await tick(e, now, api);
    if (!e.DATA.json(STATE, "{}").queue.length) break;
  }
  return calls.slice(from);
}
const fullAt = now - DAY;
const env2 = await freshEnv([["Alpha", "Alpha"]], { Alpha: { page: "Alpha", id: "Alpha", results: 2, resultsFullAt: fullAt } }, {
  Alpha: { page: "Alpha", full: false, fetchedAt: fullAt, fullAt, rows: [
    { date: "2026-11-20", event: "Gone_Event", tournament: "已经删掉的", tier: "", tierType: "", place: "5", prize: 0, team: "X" },
    { date: "2025-01-01", event: "Old_Event", tournament: "老比赛", tier: "2", tierType: "", place: "1", prize: 100, team: "Team Falcons" }] } });
const sent = await runRound(env2);
const since = new Date(env2.DATA.json(STATE, "{}").round.started - 30 * DAY).toISOString().slice(0, 10);
check("上次抓过全部成绩的人：这一轮只问最近 30 天的（选手、经历、最近的成绩、战队，各一个请求）",
  sent.map((c) => c.type).join() === "player,squadplayer,placement,team" && sent[2].cond.startsWith(`[[date::>${since}]] AND (`)
  && sent[2].cond.includes("[[opponentplayers_p4::Alpha]]"));
const alphaRows = env2.DATA.json(resultsKey("Alpha"), "{}").rows;
check("合并：换掉存着的最近那一段（Liquipedia 上删掉的那条也没了）、之前的照留、按日期新的在前",
  JSON.stringify(alphaRows.map((x) => x.event)) === JSON.stringify([EVENT, "Old_Event"]));
const alphaIdx = env2.DATA.json(INDEX, "{}").players.Alpha;
check("条数跟着变；哪次抓的全部成绩照留（抓选手资料那一步也没冲掉它）", alphaIdx.results === 2 && alphaIdx.resultsFullAt === fullAt
  && alphaIdx.id === "Alpha" && env2.DATA.json(resultsKey("Alpha"), "{}").fullAt === fullAt);

// 网址太长（414）：拆成两半重查，不算失败；经历给满了 1000 行：也拆成两半
tooLongType = "player";
squadFlood = true;
const env3 = await freshEnv([["Alpha", "Alpha"], ["Gamma", "Gamma"], ["Kite", "Kite_(player)"]], {});
const sent3 = await runRound(env3);
tooLongType = null;
squadFlood = false;
const pagesAsked = (type) => sent3.filter((c) => c.type === type).map((c) => (c.cond.match(/::/g) ?? []).length);
check("选手资料一次问 3 页回 414 → 拆成 2 + 1、再拆成 1 + 1，都抓到了", JSON.stringify(pagesAsked("player")) === "[3,2,1,1,1]"
  && ["Alpha", "Gamma", "Kite_(player)"].every((p) => env3.DATA.json(INDEX, "{}").players[p]?.id));
check("经历一次给满了 1000 行 → 拆成两半重查到一页一问", JSON.stringify(pagesAsked("squadplayer")) === "[3,2,1,1,1]");
check("414 和给满了都不算失败、不记错误", !env3.DATA.json(STATE, "{}").errors.length);
check("第一次抓的人：抓全部成绩（不带日期条件），记下哪次抓的", sent3.filter((c) => c.type === "placement").every((c) => !c.cond.includes("[[date::"))
  && typeof env3.DATA.json(INDEX, "{}").players.Alpha.resultsFullAt === "number");
