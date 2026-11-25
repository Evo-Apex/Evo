// Liquipedia 数据库 API（LPDB v3）：拼请求、取数、把原始字段整理成我们的格式（2026-11-18 第 3 步）。
// 整理的规则照搬派圈的 cloudfunctions/apex/liquipedia.js（同一个作者写的，2026-11 实测过 Apex 维基的字段）。
//
// 条款里决定做法的几条（https://liquipedia.net/api-terms-of-use）：
//   - 每小时的请求有上限：2026-11-25 起协作网页用自己专用的 Key（一小时 60 次，和派圈定时器的 Key 额度分开；
//     原来两边共用一个 Key，这里每小时最多 20 次）→ 这里每小时最多 50 次（lpsync.js），打开页面时不现查，只读抓好存下来的
//   - 尽量用存下来的、不反复要同样的数据：历届成绩只在第一次和每周抓全部，平时只问最近 30 天的（lpsync.js）
//   - 数据是 CC BY-SA 3.0，页面上写明"数据来自 Liquipedia"
//   - Key 不给第三方：只放在 Worker 的机密变量 LIQUIPEDIA_API_KEY 里（wrangler secret put，用户自己粘贴），不进 git、不下发到网页
//   - 只走 API，不抓网页
// 请求：GET https://api.liquipedia.net/api/v3/<类型>?wiki=apexlegends&conditions=…&query=…&limit=…，
// 请求头 Authorization: Apikey <key>；回 {result: [...], error?: [...]}。取不到一律回 {ok: false, error: 原因}，不抛异常。

const BASE = "https://api.liquipedia.net/api/v3/";
const WIKI = "apexlegends";
// 条款要求写明是谁在用（通用的 node-fetch 之类会被封）；不写开发者的邮箱
const UA = "EvoTeam/1.0 (team.evotracker.cn; Apex esports roster tool for Evo)";
const TIMEOUT = 15000;
const NO_DATE = "0000-01-01";
export const SITE = "https://liquipedia.net/apexlegends/";
export const RESULT_LIMIT = 1000;  // API 一次最多给这么多行

export function buildUrl(type, params) {
  const url = new URL(BASE + type);
  url.searchParams.set("wiki", WIKI);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  return url.toString();
}

// 发一次请求 → {ok: true, rows} 或 {ok: false, error}
export async function query(type, params, { apiKey, fetchImpl = fetch }) {
  const key = String(apiKey ?? "").trim();
  if (!key) return { ok: false, error: "还没配 Liquipedia 的 Key" };
  let response;
  try {
    response = await fetchImpl(buildUrl(type, params), {
      headers: { Authorization: `Apikey ${key}`, "User-Agent": UA, "Accept-Encoding": "gzip" },
      signal: AbortSignal.timeout(TIMEOUT),
    });
  } catch {
    return { ok: false, error: "连不上 Liquipedia（超时或网络出错）" };
  }
  if (!response.ok) {
    const hint = response.status === 429 ? "（请求太频繁，先歇一会儿）" : "";
    return { ok: false, status: response.status, error: `Liquipedia 回了 HTTP ${response.status}${hint}` };
  }
  let body;
  try {
    body = await response.json();
  } catch {
    return { ok: false, error: "Liquipedia 回的不是 JSON" };
  }
  if (Array.isArray(body?.error) && body.error.length) return { ok: false, error: `Liquipedia 报错：${String(body.error[0]).slice(0, 200)}` };
  if (!Array.isArray(body?.result)) return { ok: false, error: "Liquipedia 回的格式不对" };
  return { ok: true, rows: body.result };
}

// ---------- 小工具 ----------

export function anyOf(column, values) {
  return values.map((v) => `[[${column}::${v}]]`).join(" OR ");
}

// 页面名用下划线（Panic_(American_player)），队员表里的 link 用空格
export const pageToLink = (page) => String(page ?? "").replace(/_/g, " ");
export const linkToPage = (link) => String(link ?? "").replace(/ /g, "_");
export const teamName = (page) => String(page ?? "").replace(/_/g, " ").trim();
export const teamPage = (name) => String(name ?? "").trim().replace(/ /g, "_");

function date10(value) {
  const d = String(value ?? "").slice(0, 10);
  return !d || d === NO_DATE ? "" : d;
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

// 贴进来的链接或页面名 → 页面名（https://liquipedia.net/apexlegends/ALGS/2026/Split_2?x=1 → ALGS/2026/Split_2）；不像 → null
export function pageFromInput(input) {
  let text = String(input ?? "").trim();
  if (!text) return null;
  const found = text.match(/^https?:\/\/(?:www\.)?liquipedia\.net\/apexlegends\/([^?#]+)/i);
  if (/^https?:\/\//i.test(text) && !found) return null;
  if (found) text = found[1];
  try {
    text = decodeURIComponent(text);
  } catch {
    return null;
  }
  text = text.replace(/\s+/g, "_").replace(/^_+|_+$/g, "").replace(/\/+$/, "");
  if (!text || text.length > 200 || /[<>[\]{}|#?]/.test(text)) return null;
  return text;
}

// ---------- 整理 ----------

// 选手资料 links 里能直接当直播间用的（2026-11-18 实测 40 人：斗鱼 4、虎牙 1 给的是直播间地址；B站多是个人主页
// space.bilibili.com、抖音多是 v.douyin.com 短链，那些不是直播间，不用；YouTube Evo 不支持）。
// Twitch（2026-11-25，Evo 能播 Twitch 以后加的）：twitch.tv/登录名，**排在最后**：一个人同时写了国内平台和 Twitch 时
// 用国内的（Evo 的玩家多在国内，Twitch 在国内上不去）。登录名 4–25 位（Twitch 的登录名最短 4 位）；
// twitch.tv/videos 这类 Twitch 自己的页面、两段以上的路径（twitch.tv/x/videos、twitch.tv/team/x）不算
const STREAM_LINKS = [
  ["douyu", /^https?:\/\/(?:www\.)?douyu\.com\/(\d{1,24})\/?(?:[?#].*)?$/i],
  ["huya", /^https?:\/\/(?:www\.)?huya\.com\/(\d{1,24})\/?(?:[?#].*)?$/i],
  ["bilibili", /^https?:\/\/live\.bilibili\.com\/(\d{1,24})\/?(?:[?#].*)?$/i],
  ["douyin", /^https?:\/\/live\.douyin\.com\/([A-Za-z0-9_]{1,24})\/?(?:[?#].*)?$/i],
  ["twitch", /^https?:\/\/(?:(?:www|m|go)\.)?twitch\.tv\/([A-Za-z0-9_]{4,25})\/?(?:[?#].*)?$/i],
];
// 不分大小写的平台：房间号存小写（同 esports.js 的 normalRoom）
const LOWER_ROOMS = new Set(["douyin", "twitch"]);
// twitch.tv/ 后面一段是 Twitch 自己的页面、不是谁的直播间
const TWITCH_PAGES = new Set(["directory", "videos", "downloads", "jobs", "settings", "subscriptions", "inventory", "drops",
  "wallet", "search", "store", "turbo", "prime", "friends", "messages", "following", "login", "signup", "about"]);

export function streamsFromLinks(links) {
  const urls = links && typeof links === "object" ? Object.values(links).filter((v) => typeof v === "string") : [];
  const out = [];
  for (const [platform, re] of STREAM_LINKS) {
    for (const url of urls) {
      const found = re.exec(url.trim());
      const room = found ? (LOWER_ROOMS.has(platform) ? found[1].toLowerCase() : found[1]) : null;
      if (!room || (platform === "twitch" && TWITCH_PAGES.has(room))) continue;
      if (!out.some((s) => s.platform === platform && s.room === room)) out.push({ platform, room });
    }
  }
  return out;
}

// 选手资料。extradata 里 Apex 维基用到的键（2026-11 实测）：signatureLegend1..N、input（Controller / Mouse & Keyboard）
export function normPlayer(row) {
  if (!row?.pagename) return null;
  const extra = row.extradata && typeof row.extradata === "object" ? row.extradata : {};
  const legends = [];
  for (let i = 1; i <= 6; i++) {
    const legend = extra[`signatureLegend${i}`];
    if (legend && !legends.includes(String(legend))) legends.push(String(legend));
  }
  return {
    page: String(row.pagename),
    id: String(row.id || row.pagename),
    name: String(row.name ?? ""),
    localName: String(row.localizedname ?? ""),
    nats: [row.nationality, row.nationality2, row.nationality3].filter(Boolean).map(String),
    birth: date10(row.birthdate),
    status: String(row.status ?? ""),
    teamPage: String(row.teampagename ?? ""),
    team: teamName(row.teampagename),
    earnings: num(row.earnings),
    input: String(extra.input ?? ""),
    legends,
    streams: streamsFromLinks(row.links),  // 能直接用的直播间（加进选手库时自动填上）
  };
}

// 队伍经历：一段 = 一支队 × 一次加入
export function normSquad(row) {
  if (!row?.pagename) return null;
  return { teamPage: String(row.pagename), team: teamName(row.pagename), join: date10(row.joindate), leave: date10(row.leavedate),
           status: String(row.status ?? ""), type: String(row.type ?? ""), role: String(row.role ?? "") };
}

function playersOf(row) {
  const o = row?.opponentplayers && typeof row.opponentplayers === "object" ? row.opponentplayers : {};
  const out = [];
  for (let i = 1; i <= 4; i++) {
    if (o[`p${i}`]) out.push({ page: String(o[`p${i}`]), id: String(o[`p${i}dn`] || o[`p${i}`]).replace(/_/g, " ") });
  }
  return out;
}

// 一条成绩。名次空的（还没打）、个人奖项不算
export function normPlacement(row) {
  if (!row?.placement || /award/i.test(String(row.mode ?? ""))) return null;
  return { date: date10(row.date), event: String(row.parent || row.pagename || ""), tournament: String(row.tournament ?? ""),
           tier: String(row.liquipediatier ?? ""), tierType: String(row.liquipediatiertype ?? ""), place: String(row.placement),
           prize: num(row.prizemoney), team: String(row.opponentname ?? "") };
}

// 一场比赛的参赛队伍和队员（按名次排；名次空的放最后、照样留着：名单本身是真的）。TBD（还没定的名额）不算一支队
export function eventRoster(rows) {
  const teams = [];
  for (const r of rows ?? []) {
    if (r?.opponenttype !== "team" || /award/i.test(String(r.mode ?? ""))) continue;
    const team = String(r.opponentname ?? "").trim();
    const players = playersOf(r);
    if (!team || /^tbd$/i.test(team) || !players.length || teams.some((t) => t.team === team)) continue;
    teams.push({ place: String(r.placement ?? ""), team, players });
  }
  const placeNum = (t) => (Number.isFinite(Number.parseInt(t.place, 10)) ? Number.parseInt(t.place, 10) : 9999);
  return teams.sort((a, b) => placeNum(a) - placeNum(b));
}

export function normTeam(row) {
  if (!row?.pagename) return null;
  return { page: String(row.pagename), name: String(row.name || teamName(row.pagename)), region: String(row.region ?? ""),
           status: String(row.status ?? ""), created: date10(row.createdate), disbanded: date10(row.disbanddate), earnings: num(row.earnings) };
}

export function normTournament(row) {
  if (!row?.pagename) return null;
  const n = Number(row.participantsnumber);
  return { page: String(row.pagename), name: String(row.name || teamName(row.pagename)), start: date10(row.startdate),
           end: date10(row.enddate), tier: String(row.liquipediatier ?? ""), tierType: String(row.liquipediatiertype ?? ""),
           teams: n > 0 ? n : 0, prize: num(row.prizepool) };
}

// ---------- 几种请求（每种一次请求，一批页面一起问） ----------

const PLAYER_COLS = "pagename,id,name,localizedname,nationality,nationality2,nationality3,birthdate,teampagename,status,earnings,extradata,links";
const SQUAD_COLS = "pagename,link,role,type,status,joindate,leavedate";
const RESULT_COLS = "pagename,parent,tournament,date,placement,prizemoney,liquipediatier,liquipediatiertype,opponentname,opponentplayers,mode";
const ROSTER_COLS = "pagename,parent,tournament,date,placement,opponentname,opponenttype,opponentplayers,mode";
const TEAM_COLS = "pagename,name,region,status,createdate,disbanddate,earnings";
const TOURNAMENT_COLS = "pagename,name,startdate,enddate,liquipediatier,liquipediatiertype,participantsnumber,prizepool";

// 每个函数回 {ok, error} 或 {ok: true, ...整理好的}
export async function fetchTournaments(pages, deps) {
  const r = await query("tournament", { conditions: anyOf("pagename", pages), query: TOURNAMENT_COLS, limit: 500 }, deps);
  if (!r.ok) return r;
  const found = {};
  for (const row of r.rows) {
    const t = normTournament(row);
    if (t && pages.includes(t.page)) found[t.page] = t;
  }
  return { ok: true, found };
}

export async function fetchRoster(page, deps) {
  const r = await query("placement", { conditions: `[[parent::${page}]]`, query: ROSTER_COLS, limit: 300 }, deps);
  return r.ok ? { ok: true, teams: eventRoster(r.rows) } : r;
}

export async function fetchPlayers(pages, deps) {
  const r = await query("player", { conditions: anyOf("pagename", pages), query: PLAYER_COLS, limit: 500 }, deps);
  if (!r.ok) return r;
  const found = {};
  for (const row of r.rows) {
    const p = normPlayer(row);
    if (p && pages.includes(p.page)) found[p.page] = p;
  }
  return { ok: true, found };
}

export async function fetchSquads(pages, deps) {
  const r = await query("squadplayer", { conditions: anyOf("link", pages.map(pageToLink)), query: SQUAD_COLS, limit: 1000 }, deps);
  if (!r.ok) return r;
  const out = Object.fromEntries(pages.map((p) => [p, []]));
  const seen = new Set();
  for (const row of r.rows) {
    const page = linkToPage(row.link);
    const s = normSquad(row);
    if (!out[page] || !s) continue;
    const key = `${page}|${s.teamPage}|${s.join}|${s.role}`;
    if (seen.has(key)) continue;  // 文档说 API 里可能有重复
    seen.add(key);
    out[page].push(s);
  }
  for (const list of Object.values(out)) list.sort((a, b) => (b.join || "").localeCompare(a.join || ""));
  return { ok: true, squads: out, full: r.rows.length >= RESULT_LIMIT };  // full：给满了 1000 行，可能截掉了（调用方拆小重查）
}

// 一个人的一条成绩算不算同一条：同一场比赛、同一支队、同一个名次
export const resultKey = (res) => `${res.event}|${res.team}|${res.place}`;

// full：一次给满了上限，可能截掉了老比赛（调用方把这一批拆小重查）。
// since（2026-11-25）：只要这一天以后的成绩（"YYYY-MM-DD"，不含这一天），平时只问最近的、不重抓全部
export async function fetchResults(pages, deps, since = "") {
  const players = pages.flatMap((p) => [1, 2, 3, 4].map((i) => `[[opponentplayers_p${i}::${p}]]`)).join(" OR ");
  const conditions = since ? `[[date::>${since}]] AND (${players})` : players;
  const r = await query("placement", { conditions, query: RESULT_COLS, limit: RESULT_LIMIT, order: "date DESC" }, deps);
  if (!r.ok) return r;
  const out = Object.fromEntries(pages.map((p) => [p, []]));
  const seen = new Set();
  for (const row of r.rows) {
    const res = normPlacement(row);
    if (!res) continue;
    for (const pl of playersOf(row)) {
      if (!out[pl.page]) continue;
      const key = `${pl.page}|${resultKey(res)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out[pl.page].push(res);
    }
  }
  return { ok: true, results: out, full: r.rows.length >= RESULT_LIMIT };
}

export async function fetchTeams(pages, deps) {
  const r = await query("team", { conditions: anyOf("pagename", pages), query: TEAM_COLS, limit: 500 }, deps);
  if (!r.ok) return r;
  const found = {};
  for (const row of r.rows) {
    const t = normTeam(row);
    if (t && pages.includes(t.page)) found[t.page] = t;
  }
  return { ok: true, found };
}
