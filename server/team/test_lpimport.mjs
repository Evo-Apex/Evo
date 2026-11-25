// 从 Liquipedia 一键加进选手库的测试（2026-11-18）。由 test.mjs 运行。
// 走一遍：选手资料里认出直播间（斗鱼、虎牙、B站直播间、抖音直播间、Twitch（2026-11-25，排最后）；个人主页、短链、
// Twitch 自己的页面不算）、收录的比赛上的按钮和权限、
// 加进来的名字（手动修正过的用修正的）、队名、页面名、直播间自动填（别人用着的房间不填）、没有的标"直播间待填"、已经有的跳过、
// 待填的不挡发布（按"不直播"发、提醒一句）、补上直播间、战队页加现役选手、名单还没抓到时提示；
// 从 Liquipedia 补直播间（2026-11-25）：候选怎么算、页面、权限、整批核对、打开以后被人改过的跳过、改动记录。

import { hash, newToken, update } from "./auth.js";
import { PLAYERS } from "./data.js";
import { normPlayer } from "./liquipedia.js";
import { addFromLiquipedia, fillCandidates } from "./lpimport.js";
import { INDEX, TRACKED, eventKey } from "./lpsync.js";
import { OVERRIDES } from "./overrides.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
const now = Date.UTC(2026, 10, 18, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const players = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const find = (name) => players().find((p) => p.name === name);

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: role === "admin", created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

// ---------- 认直播间 ----------
const p = normPlayer({ pagename: "3Mz", id: "3Mz", links: { vod: "https://v.douyu.com/author-replay/x", bilibili: "https://space.bilibili.com/38979291",
  douyin_page: "https://v.douyin.com/iM1VAd7L", douyu: "https://www.douyu.com/1667826", huyatv: "https://www.huya.com/18957845",
  bilibili2: "https://live.bilibili.com/21452505?broadcast_type=0", douyin: "https://live.douyin.com/XXFPS", twitch: "https://www.twitch.tv/x" } });
check("认出能用的直播间（抖音转小写），个人主页、短链、太短的 Twitch 名不算", JSON.stringify(p.streams) === JSON.stringify([
  { platform: "douyu", room: "1667826" }, { platform: "huya", room: "18957845" }, { platform: "bilibili", room: "21452505" },
  { platform: "douyin", room: "xxfps" }]));
const tw = normPlayer({ pagename: "Hal", links: { twitch: "https://www.twitch.tv/ImperialHal__", twitch2: "https://m.twitch.tv/abcd/",
  a: "https://twitch.tv/videos", b: "https://www.twitch.tv/x_y_z/videos", c: "https://www.twitch.tv/team/tsm", d: "https://go.twitch.tv/Genburten?ref=lp#x",
  e: "https://www.twitch.tv/Directory", f: "https://twitch.tv.example.com/abcdef", douyu: "https://www.douyu.com/12" } });
check("Twitch：www / m / go 和不带前缀都认、转小写、排在国内平台后面；Twitch 自己的页面、两段路径、别的站不认", JSON.stringify(tw.streams) === JSON.stringify([
  { platform: "douyu", room: "12" }, { platform: "twitch", room: "imperialhal__" }, { platform: "twitch", room: "abcd" },
  { platform: "twitch", room: "genburten" }]));
check("没有 links 的选手：没有直播间", normPlayer({ pagename: "x" }).streams.length === 0);

// ---------- 准备：抓到的资料、收录的比赛、选手库里已经有的 ----------
const EVENT = "ALGS/2026/LCQ";
await env.DATA.put(INDEX, JSON.stringify({
  events: { [EVENT]: { page: EVENT, name: "LCQ", rosterTeams: 2, rosterPlayers: 6, rosterAt: now } },
  players: {
    Ggod: { page: "Ggod", id: "Ggod", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [{ platform: "douyu", room: "111" }, { platform: "huya", room: "222" }] },
    YY: { page: "YY", id: "YY", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [{ platform: "bilibili", room: "333" }] },
    Chisu: { page: "Chisu", id: "Chisu", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [] },
    "Old_(player)": { page: "Old_(player)", id: "Old", teamPage: "Falcons", team: "Falcons", streams: [{ platform: "douyu", room: "444" }] },
    Fresh: { page: "Fresh", id: "Fresh", teamPage: "Falcons", team: "Falcons", streams: [{ platform: "douyin", room: "abc_1" }] },
    Third: { page: "Third", id: "Third", teamPage: "Falcons", team: "Falcons" },
  },
  teams: { Wolves_Esports: { page: "Wolves_Esports", name: "Wolves Esports" }, Falcons: { page: "Falcons", name: "Falcons" } },
}));
await env.DATA.put(eventKey(EVENT), JSON.stringify({ page: EVENT, teams: [
  { team: "Wolves", place: "1", players: [{ page: "Ggod", id: "Ggod" }, { page: "YY", id: "YY" }, { page: "Chisu", id: "Chisu" }] },
  { team: "Team Falcons", place: "2", players: [{ page: "Old_(player)", id: "Old" }, { page: "Fresh", id: "Fresh" }, { page: "Third", id: "Third" }] }] }));
await env.DATA.put(TRACKED, JSON.stringify({ items: [{ page: EVENT, addedBy: "黄指导", addedAt: now }, { page: "No/Roster", addedBy: "黄指导", addedAt: now }] }));
await env.DATA.put(OVERRIDES, JSON.stringify({ items: { YY: { id: { value: "YY_new", reason: "改名", by: "黄指导", at: now, source: "YY" } } } }));

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const viewer = await join("看看", "viewer");
await editor.post("/players/add", { name: "老人", team: "老队", platform: "douyu", room: "111" });               // 占着 Ggod 的斗鱼房间
await editor.post("/players/add", { name: "Old 本人", team: "老队", platform: "douyu", room: "9", liquipedia: "Old_(player)" });  // 已经连上了

// ---------- 收录的比赛：加进选手库 ----------
let r = await viewer.get("/liquipedia");
check("只读的看不到加进选手库", !r.text.includes("/liquipedia/to-roster"));
r = await editor.get("/liquipedia");
check("收录的比赛上有加进选手库（名单抓到了才有）", (r.text.match(/action="\/liquipedia\/to-roster"/g) ?? []).length === 1);
r = await viewer.post("/liquipedia/to-roster", { page: EVENT });
check("只读的不能加", r.status === 403);
r = await editor.post("/liquipedia/to-roster", { page: EVENT });
check("加进选手库 → 回选手库，说加了几位、几位待填、跳过几位", r.location === "/players?ok=fromlp&added=5&pending=2&skipped=1");
check("名字用手动修正过的 ID、队名用名单上的、页面名填好", find("YY_new")?.team === "Wolves" && find("YY_new").liquipedia === "YY"
  && find("Fresh").team === "Team Falcons");
check("直播间：别人占着的斗鱼不填、用下一个虎牙", find("Ggod").platform === "huya" && find("Ggod").room === "222");
check("直播间：B站直播间、抖音直播间自动填", find("YY_new").platform === "bilibili" && find("YY_new").room === "333"
  && find("Fresh").platform === "douyin" && find("Fresh").room === "abc_1");
check("没有直播间的标待填（平台、房间号空着）", find("Chisu").platform === "" && find("Chisu").room === "" && find("Third").platform === "");
check("选手库里已经连上的跳过", !players().some((x) => x.name === "Old"));
r = await editor.get(r.location);
check("选手库上提示加了几位、标着直播间待填", r.text.includes("从 Liquipedia 加进来 5 位（2 位直播间待填") && r.text.includes("直播间待填</span>"));
r = await editor.post("/liquipedia/to-roster", { page: EVENT });
check("再点一次：都跳过", r.location === "/players?ok=fromlp&added=0&pending=0&skipped=6");
r = await editor.post("/liquipedia/to-roster", { page: "No/Roster" });
check("名单还没抓到 → 提示", r.status === 400 && r.text.includes("名单还没抓到"));

// ---------- 待填的不挡发布（按"不直播"发、提醒一句），补上 ----------
await editor.post("/events/add", [["name", "LCQ"], ["type", "杯赛"], ["stage", ""], ["start", "2026-11-18T19:00"], ["end", "2026-11-18T22:00"],
  ["teams", "Wolves"], ["teams", "Team Falcons"]]);
r = await admin.get("/publish");
check("有直播间待填的 → 照样能发布，提醒这次按不直播发", !r.text.includes("还不能发布") && r.text.includes("2 位选手直播间还没填，这次按&quot;不直播&quot;发布")
  && r.text.includes("「Third」"));
r = await admin.post("/publish/test");
const sent = env.PUBLIC.json("esports_test.json");
check("发出去的文件里待填的人写成不直播", r.location === "/publish?ok=test"
  && sent.players.find((x) => x.name === "Third")?.platform === "none" && sent.players.find((x) => x.name === "Third").room === "");
r = await editor.get("/players");
check("待填的人编辑时平台先是请选平台", r.text.includes('<option value="" selected>请选平台</option>'));
r = await editor.post(`/players/${find("Chisu").id}/save`, { name: "Chisu", team: "Wolves", platform: "", room: "" });
check("平台还空着就保存 → 提示选一个", r.status === 400 && r.text.includes("选一个平台"));
await editor.post(`/players/${find("Chisu").id}/save`, { name: "Chisu", team: "Wolves", platform: "none", room: "", liquipedia: "Chisu" });
await editor.post(`/players/${find("Third").id}/save`, { name: "Third", team: "Team Falcons", platform: "douyu", room: "555", liquipedia: "Third" });
r = await admin.get("/publish");
check("补上以后不再提示直播间还没填", !r.text.includes("直播间还没填"));

// ---------- 战队页：把现役选手加进选手库 ----------
r = await editor.get("/teams/Falcons");
check("战队页：标出已在选手库的、按钮写着还有几人没加", r.text.includes("已在选手库") && r.text.includes("把现役选手加进选手库（0 人）") === false);
await env.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: players().filter((x) => x.team !== "Team Falcons") }));
r = await editor.get("/teams/Falcons");
check("战队页：没加的有按钮（写着几人）", r.text.includes("把现役选手加进选手库（2 人）"));
r = await viewer.post("/teams/Falcons/to-roster");
check("只读的不能加", r.status === 403);
r = await editor.post("/teams/Falcons/to-roster");
check("战队页加进来：队名用 Liquipedia 的队名", r.location === "/players?ok=fromlp&added=2&pending=1&skipped=1" && find("Fresh").team === "Falcons");
check("改动记录写明从哪加的", env.DATA.json("log.json").items[0].text.startsWith("从 Liquipedia 加进选手库（战队 Falcons）：新加 2 位"));

// ---------- 一键加进选手库：Twitch（2026-11-25） ----------
await env.DATA.put(INDEX, JSON.stringify({ events: {}, teams: {}, players: {
  OnlyTw: { page: "OnlyTw", id: "OnlyTw", streams: [{ platform: "twitch", room: "onlytw" }] },
  Both: { page: "Both", id: "Both", streams: [{ platform: "douyu", room: "60601" }, { platform: "twitch", room: "both_tw" }] },
  TakenTw: { page: "TakenTw", id: "TakenTw", streams: [{ platform: "twitch", room: "onlytw" }] },
} }));
await env.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: [] }));
const added = await addFromLiquipedia(env.DATA, { name: "Momona" }, [{ page: "OnlyTw", name: "OnlyTw", team: "T" },
  { page: "Both", name: "Both", team: "T" }, { page: "TakenTw", name: "TakenTw", team: "T" }], "测试", now);
check("一键加：只有 Twitch 的填 Twitch；有斗鱼也有 Twitch 的用斗鱼；Twitch 房间被同一批的人占了算待填",
  find("OnlyTw").platform === "twitch" && find("OnlyTw").room === "onlytw" && find("Both").platform === "douyu"
  && find("TakenTw").platform === "" && added.added === 3 && added.pending === 1);

// ---------- 从 Liquipedia 补直播间（2026-11-25） ----------
const lpPlayers = {
  Hal: { page: "Hal", id: "Hal", streams: [{ platform: "twitch", room: "imperialhal__" }] },
  Gen: { page: "Gen", id: "Gen", streams: [{ platform: "douyu", room: "777" }, { platform: "twitch", room: "genburten" }] },
  Dup1: { page: "Dup1", id: "Dup1", streams: [{ platform: "twitch", room: "shared_one" }] },
  Dup2: { page: "Dup2", id: "Dup2", streams: [{ platform: "twitch", room: "shared_one" }] },
  Owned: { page: "Owned", id: "Owned", streams: [{ platform: "twitch", room: "ownedroom" }] },
  Empty: { page: "Empty", id: "Empty", streams: [] },
  Gone: { page: "Gone", missing: true },
  Has: { page: "Has", id: "Has", streams: [{ platform: "twitch", room: "has_tw" }] },
  UrlPage: { page: "UrlPage", id: "UrlPage", streams: [{ platform: "twitch", room: "urlroom" }] },
};
const person = (id, name, platform, room, liquipedia) => ({ id, name, team: "国际队", platform, room, liquipedia,
  created: now - MINUTE, createdBy: "黄指导", updated: now - MINUTE, updatedBy: "黄指导", rev: 2 });
const roster = [
  person("a0000001", "Hal", "none", "", "Hal"),
  person("a0000002", "Gen", "", "", "Gen"),
  person("a0000003", "Dup1", "none", "", "Dup1"),
  person("a0000004", "Dup2", "none", "", "Dup2"),
  person("a0000005", "OwnedFan", "none", "", "Owned"),
  person("a0000006", "Owner", "twitch", "ownedroom", ""),
  person("a0000007", "Empty", "none", "", "Empty"),
  person("a0000008", "Gone", "", "", "Gone"),
  person("a0000009", "NoPage", "none", "", ""),
  person("a0000010", "Has", "douyu", "888", "Has"),
  person("a0000011", "Url", "none", "", "https://liquipedia.net/apexlegends/UrlPage"),
];
await env.DATA.put(INDEX, JSON.stringify({ events: {}, teams: {}, players: lpPlayers }));
await env.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: roster }));

const found = fillCandidates(roster, { players: lpPlayers });
check("候选：待填、不直播的都算，已经有平台的不算；挑第一个没被占的（有斗鱼用斗鱼）；页面名写成网址的也认",
  JSON.stringify(found.matches.map((m) => [m.name, m.was, m.platform, m.room])) === JSON.stringify([
    ["Hal", "none", "twitch", "imperialhal__"], ["Gen", "", "douyu", "777"], ["Dup1", "none", "twitch", "shared_one"],
    ["Url", "none", "twitch", "urlroom"]]));
check("候选：直播间被选手库里别人占着、被同一页上排在前面的人用了 → 进\"占着\"那组，写是谁的",
  JSON.stringify(found.taken.map((t) => [t.name, t.room, t.owner])) === JSON.stringify([["Dup2", "shared_one", "Dup1"], ["OwnedFan", "ownedroom", "Owner"]]));
check("候选：资料里没有直播间、资料没抓到、没填页面名的各自计数", found.noLink === 1 && found.noData === 1 && found.noPage === 1);

r = await viewer.get("/players");
check("选手库：只读的看不到\"从 Liquipedia 补直播间\"", !r.text.includes("/players/fill-streams"));
r = await editor.get("/players");
check("选手库：编辑能看到\"从 Liquipedia 补直播间 ›\"", r.text.includes('<a href="/players/fill-streams">从 Liquipedia 补直播间 ›</a>'));
r = await viewer.get("/players/fill-streams");
check("只读的能看页面，但勾不了、没有提交按钮", r.status === 200 && r.text.includes("能补的（4 位）") && r.text.includes("checked disabled")
  && !r.text.includes("用勾上的这些</button>"));
r = await editor.get("/players/fill-streams");
check("页面：每行默认勾上、标着原来待填 / 原来不直播、房间号能点开 Twitch 核对",
  (r.text.match(/name="use_\d+" value="1" checked>/g) ?? []).length === 4 && r.text.includes("原来不直播") && r.text.includes("原来待填")
  && r.text.includes('href="https://www.twitch.tv/imperialhal__" target="_blank" rel="noopener noreferrer">Twitch imperialhal__</a>')
  && r.text.includes("用勾上的这些</button>"));
check("页面：占着的那组写清是谁的；最后一行写没直播间、没抓到、没填页面名的人数",
  r.text.includes("直播间被别人占着的（2 位）") && r.text.includes("是「Owner」的")
  && r.text.includes("1 位 Liquipedia 资料里没有能用的直播间，1 位的 Liquipedia 资料还没抓到，1 位没填 Liquipedia 页面名"));

const pick = (i, id, platform, room) => [[`use_${i}`, "1"], [`id_${i}`, id], [`platform_${i}`, platform], [`room_${i}`, room]];
r = await viewer.post("/players/fill-streams", pick(0, "a0000001", "twitch", "imperialhal__"));
check("只读的不能补", r.status === 403 && find("Hal").platform === "none");
r = await editor.post("/players/fill-streams", [["id_0", "a0000001"], ["platform_0", "twitch"], ["room_0", "imperialhal__"]]);
check("一个都没勾 → 提示", r.status === 400 && r.text.includes("一个都没勾"));
r = await editor.post("/players/fill-streams", [...pick(0, "a0000001", "twitch", "imperialhal__"), ...pick(1, "a0000007", "twitch", "madeup_room")]);
check("有一位 Liquipedia 上没写这个直播间 → 整批不补，说是谁", r.status === 400 && r.text.includes("「Empty」：Liquipedia 资料里没有 Twitch madeup_room")
  && find("Hal").platform === "none");

// 打开页面以后，Dup1 被人手动改成了 B站
await env.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: players().map((x) => (x.name === "Dup1" ? { ...x, platform: "bilibili", room: "424242", rev: 3 } : x)) }));
r = await editor.post("/players/fill-streams", [...pick(0, "a0000001", "twitch", "ImperialHal__"), ...pick(1, "a0000002", "douyu", "777"),
  ...pick(2, "a0000003", "twitch", "shared_one"), ...pick(3, "a0000011", "twitch", "urlroom")]);
check("补好 → 回这一页，说补了几位、跳过几位", r.location === "/players/fill-streams?ok=filled&n=3&skipped=1");
const hal = find("Hal");
check("补上的：平台、房间号（小写）、谁什么时候改的、rev 加 1", hal.platform === "twitch" && hal.room === "imperialhal__" && hal.updatedBy === "Momona"
  && hal.updated === now && hal.rev === 3 && find("Gen").platform === "douyu" && find("Gen").room === "777" && find("Url").room === "urlroom");
check("打开页面以后被人改过的跳过，不盖掉", find("Dup1").platform === "bilibili" && find("Dup1").room === "424242");
check("改动记录写明补了谁", env.DATA.json("log.json").items[0].text
  === "从 Liquipedia 补直播间（3 位）：Hal（Twitch imperialhal__）、Gen（斗鱼 777）、Url（Twitch urlroom）");
r = await editor.get(r.location);
check("页面上显示补了几位、跳过的原因", r.text.includes("补上了 3 位的直播间：下次发布 Evo 就能看到（1 位在你打开页面以后被人改过，没盖掉）"));
check("补完以后：Dup2 能补了（原来占着的 Dup1 换了平台），补过的不再列出来", r.text.includes("能补的（1 位）") && r.text.includes("Dup2"));
