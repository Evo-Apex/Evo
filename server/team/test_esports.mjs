// 选手库、赛程、发布的测试（第 2 步）。由 test.mjs 运行。
// 走一遍：权限（只读、编辑、能发布的编辑、管理员）、添加 / 修改 / 删除、出错时留着填的内容、两个人同时改、战队改名、
// 从 Excel 粘贴导入、赛程的分组、发布前的检查（人数、示例、结束超过 7 天、没有赛事）、发布（写进去读回来核对、记录）、
// 平台开关（照线上原样带着）、首页和改动记录。和 Python 规则逐项比的在 tests/test_team_publish.py。

import { hash, newToken, update } from "./auth.js";
import { parseTable } from "./esports.js";
import { EVENTS, LOG, PLAYERS, PUBLISHED } from "./data.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

let now = Date.UTC(2026, 10, 17, 4, 0);  // 北京时间 11-17 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const browser = (ua) => makeBrowser(env, () => now, ua);
const playerList = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const eventList = () => env.DATA.json(EVENTS, '{"items":[]}').items;
const logTexts = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);

// 照 tools/team.py 的做法放一个邀请，再用一个新"浏览器"进来
async function join(name, role, publish = false) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish, created: now,
                        createdBy: "命令行", expires: now + 7 * DAY });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const publisher = await join("能发布的编辑", "editor", true);
const viewer = await join("看看", "viewer");

// ---------- 权限 ----------
let r = await viewer.get("/players");
check("只读的能看选手库", r.status === 200 && r.text.includes("选手库"));
check("只读的看不到添加", !r.text.includes("/players/add") && !r.text.includes("从 Excel 粘贴导入"));
r = await viewer.post("/players/add", { name: "x", team: "y", platform: "douyu", room: "1" });
check("只读的不能加选手", r.status === 403 && playerList().length === 0);
r = await viewer.get("/");
check("导航里有选手库、赛程、发布", r.text.includes('href="/players"') && r.text.includes('href="/events"') && r.text.includes('href="/publish"'));

// ---------- 添加选手 ----------
const add = (b, name, team, platform, room, liquipedia = "") => b.post("/players/add", { name, team, platform, room, liquipedia });
r = await add(editor, "  Kite ", "星火电竞", "douyu", "520013");
check("编辑加选手 → 回列表、加好了", r.status === 303 && r.location === "/players?ok=added");
let p = playerList()[0];
check("存下来的选手（去掉空格、编号、rev、谁加的）", p.name === "Kite" && p.team === "星火电竞" && p.platform === "douyu"
  && p.room === "520013" && /^[0-9a-f]{8}$/.test(p.id) && p.rev === 1 && p.createdBy === "Momona");
r = await editor.get("/players?ok=added");
check("列表上提示加好了、有直播间链接", r.text.includes("加好了") && r.text.includes("https://www.douyu.com/520013"));
for (const [name, room, platform] of [["Ace", "100", "bilibili"], ["Bee", "200", "huya"]]) await add(editor, name, "星火电竞", platform, room);
for (const [name, room] of [["北一", "301"], ["北二", "302"], ["北三", "303"]]) await add(editor, name, "北辰", "douyin", room);
await add(editor, "残一", "残队", "douyu", "401");
await add(editor, "残二", "残队", "douyu", "402");
check("加了 8 个人", playerList().length === 8);

r = await add(editor, "Kite", "北辰", "douyu", "999");
check("同名的选手 → 提示在哪个队、不保存", r.status === 400 && r.text.includes("已经有一个叫「Kite」的选手了（在「星火电竞」）") && playerList().length === 8);
r = await add(editor, "新人", "北辰", "douyu", "520013");
check("同一个房间 → 提示是谁的", r.status === 400 && r.text.includes("斗鱼 房间 520013 已经是「Kite」的了"));
r = await add(editor, "新人", "北辰", "douyu", "abc");
check("房间号不是数字 → 提示", r.status === 400 && r.text.includes("只能是数字"));
check("出错时留着填的内容、添加框展开", r.text.includes('value="新人"') && /<details class="card fold" open>/.test(r.text));
r = await add(editor, "抖音号", "临时队", "douyin", "XXFPS");
const handle = playerList().find((x) => x.name === "抖音号");
check("抖音可以写抖音号，存成小写", r.location === "/players?ok=added" && handle.room === "xxfps");
r = await add(editor, "抖音号二", "临时队", "douyin", "xxFps");
check("大小写不同的同一个抖音号 → 算同一个房间", r.status === 400 && r.text.includes("抖音 房间 xxfps 已经是「抖音号」的了"));
r = await add(editor, "抖音号二", "临时队", "douyin", "a.b");
check("抖音号带点 → 提示只能是字母、数字、下划线", r.status === 400 && r.text.includes("只能是字母、数字、下划线"));
r = await add(editor, "斗鱼字母", "临时队", "douyu", "xxfps");
check("斗鱼写字母 → 提示只能是数字", r.status === 400 && r.text.includes("只能是数字"));
r = await editor.get("/players");
check("房间号输入框不限只填数字（抖音号要能填）、链接是小写", !r.text.includes('pattern="[0-9]{1,24}"') && r.text.includes("https://live.douyin.com/xxfps"));
await editor.post(`/players/${handle.id}/delete`);
r = await add(editor, "贴网址", "临时队", "douyu", "7001", "https://liquipedia.net/apexlegends/Panic_%28American_player%29");
const pasted = playerList().find((x) => x.name === "贴网址");
check("Liquipedia 贴网址 → 存成页面名", r.location === "/players?ok=added" && pasted.liquipedia === "Panic_(American_player)");
r = await add(editor, "乱填", "临时队", "douyu", "7002", "https://example.com/x");
check("别的网站的网址 → 提示看不懂", r.status === 400 && r.text.includes("看不懂 Liquipedia 页面名"));
// 11-18 以前存进去的整个网址：读的时候转成页面名
const raw = JSON.parse(env.DATA.objects.get(PLAYERS).body);
raw.items.find((x) => x.name === "贴网址").liquipedia = "https://liquipedia.net/apexlegends/3Mz";
await env.DATA.put(PLAYERS, JSON.stringify(raw));
r = await editor.get("/players");
check("以前存的网址读出来是页面名", r.text.includes("Liquipedia：3Mz") && !r.text.includes("Liquipedia：https://"));
await editor.post(`/players/${pasted.id}/delete`);
r = await add(editor, "x".repeat(41), "北辰", "douyu", "5");
check("名字超过 40 个字 → 提示", r.status === 400 && r.text.includes("最多 40 个字"));
r = await add(editor, "新人", "北辰", "youtube", "5");
check("不认识的平台 → 提示", r.status === 400 && r.text.includes("不认识"));
r = await add(editor, "<b>坏</b>", "北辰", "douyu", "6");
const escaped = await editor.get("/players");
check("名字里的 HTML 照字面显示", escaped.text.includes("&lt;b&gt;坏&lt;/b&gt;") && !escaped.text.includes("<b>坏</b>"));
await editor.post(`/players/${playerList().find((x) => x.name === "<b>坏</b>").id}/delete`);

r = await editor.get("/players");
check("一支队几个人都行：2 人的队不提示", r.text.includes("2 人</span>") && !r.text.includes("要正好"));

// ---------- 修改、两个人同时改 ----------
const kite = () => playerList().find((x) => x.name === "Kite");
r = await editor.post(`/players/${kite().id}/save`, { rev: "1", name: "Kite", team: "星火电竞", platform: "douyu", room: "520014", liquipedia: "Kite_(player)" });
check("改房间号 → 保存好了、rev 2、回到那个人（#p-…）", r.location === `/players?ok=saved#p-${kite().id}` && kite().room === "520014" && kite().rev === 2 && kite().updatedBy === "Momona");
check("改动记录写明改了什么", logTexts()[0] === "修改选手 Kite：房间号 520014 → 520014".replace("520014 →", "520013 →") + "，Liquipedia （空） → Kite_(player)");
r = await admin.post(`/players/${kite().id}/save`, { rev: "1", name: "Kite", team: "星火电竞", platform: "douyu", room: "1" });
check("拿着旧的 rev 保存 → 提示别人刚改过、不保存", r.status === 400 && r.text.includes("Momona 刚改过") && kite().room === "520014");
r = await editor.post(`/players/${kite().id}/save`, { rev: "2", name: "Kite", team: "星火电竞", platform: "douyu", room: "520014", liquipedia: "Kite_(player)" });
check("什么都没改 → 不加 rev、不记", kite().rev === 2 && !logTexts()[0].includes("修改选手 Kite：，"));
r = await editor.post("/players/zzzzzzzz/save", { name: "x" });
check("乱写的编号 → 没有这个页面", r.status === 404);

// ---------- 赛程 ----------
const eventForm = (name, type, start, end, teams, stage = "") =>
  [["name", name], ["type", type], ["stage", stage], ["start", start], ["end", end], ...teams.map((t) => ["teams", t])];
r = await editor.post("/events/add", eventForm("联赛 第 3 周", "联赛", "2026-11-17T19:00", "2026-11-17T23:00", ["星火电竞", "北辰"], "B 组对 C 组"));
check("加赛事", r.location === "/events?ok=added" && eventList().length === 1);
let e = eventList()[0];
check("时间存成北京时间的写法", e.start === "2026-11-17T19:00:00+08:00" && e.end === "2026-11-17T23:00:00+08:00" && e.teams.join() === "星火电竞,北辰");
r = await editor.post("/events/add", eventForm("坏的", "联赛", "2026-11-17T19:00", "2026-11-17T18:00", ["北辰"]));
check("结束早于开始 → 提示、留着填的", r.status === 400 && r.text.includes("结束要晚于开始") && r.text.includes('value="坏的"'));
r = await editor.post("/events/add", eventForm("坏的", "联赛", "2026-11-17T19:00", "2026-11-17T20:00", []));
check("没选队伍 → 提示", r.status === 400 && r.text.includes("至少要选一支队伍"));
r = await editor.post("/events/add", eventForm("坏的", "联赛", "2026-11-17T19:00", "2026-11-17T20:00", ["星火电竟"]));
check("队伍不在选手库里 → 提示像哪个", r.status === 400 && r.text.includes("不在选手库里（是不是「星火电竞」？）"));
r = await editor.post("/events/add", eventForm("坏的", "表演赛", "2026-11-17T19:00", "2026-11-17T20:00", ["北辰"]));
check("不认识的类型 → 提示", r.status === 400 && r.text.includes("只能是 联赛 / 训练赛 / 杯赛"));
await editor.post("/events/add", eventForm("训练赛 A", "训练赛", "2026-11-17T10:00", "2026-11-17T13:00", ["北辰"]));
await editor.post("/events/add", eventForm("很早的杯赛", "杯赛", "2026-11-01T19:00", "2026-11-01T22:00", ["北辰"]));
await editor.post("/events/add", eventForm("昨天的", "杯赛", "2026-11-16T19:00", "2026-11-16T22:00", ["北辰"]));
r = await viewer.get("/events");
check("赛程分成进行中、即将开始、已结束", r.text.includes("进行中（1）") && r.text.includes("即将开始（1）") && r.text.includes("已结束（2）"));
check("结束超过 7 天的标出来", r.text.includes("结束超过 7 天，不会发布"));
check("只读的看不到改", !r.text.includes("/events/add"));
e = eventList().find((x) => x.name === "联赛 第 3 周");
r = await editor.post(`/events/${e.id}/save`, eventForm("联赛 第 3 周", "联赛", "2026-11-17T19:30", "2026-11-17T23:00", ["星火电竞", "北辰"], "B 组对 C 组").concat([["rev", "1"]]));
check("改开始时间 → 记下几点到几点", logTexts()[0] === "修改赛事「联赛 第 3 周 · B 组对 C 组」：开始 2026-11-17 19:00 → 2026-11-17 19:30");

// ---------- 战队改名 ----------
r = await editor.post("/teams/rename", { from: "北辰", to: "星火电竞" });
check("改成已有的队名 → 提示", r.status === 400 && r.text.includes("已经有一支叫「星火电竞」的队了"));
r = await editor.post("/teams/rename", { from: "北辰", to: "北辰战队" });
check("战队改名：选手和赛程一起改", r.location === "/players?ok=renamed" && playerList().filter((x) => x.team === "北辰战队").length === 3
  && eventList().every((x) => !x.teams.includes("北辰")) && eventList().find((x) => x.name === "联赛 第 3 周").teams.includes("北辰战队"));

// ---------- 从 Excel 粘贴导入 ----------
r = await editor.post("/players/import", { table: "选手\t战队\t平台\t房间号\tLiquipedia页面名\n残三\t残队\t斗鱼\t403\t\nBad\t残队\t快手\t1\t" });
check("导入有一行不对 → 整批不存、说第几行", r.status === 400 && r.text.includes("第 2 行：平台「快手」不认识")
  && !playerList().some((x) => x.name === "残三") && r.text.includes("残三"));
r = await editor.post("/players/import", { table: "选手\t战队\t平台\t房间号\tLiquipedia页面名\r\n残三\t残队\t斗鱼\t403\t\r\nKite\t星火电竞\tdouyu\t520015\t\r\n" });
check("导入：新加 1、更新 1（按名字）", r.location === "/players?ok=imported&added=1&changed=1"
  && playerList().find((x) => x.name === "残三") && kite().room === "520015" && kite().liquipedia === "");
r = await editor.get(r.location);
check("提示导入了几条", r.text.includes("导入好了：新加 1 条，更新 1 条"));
r = await editor.post("/players/import", { table: "甲\t残队\t斗鱼\t401" });
check("导入的房间号和别人撞了 → 不存", r.status === 400 && r.text.includes("同时是「残一」和「甲」的"));
r = await editor.post("/players/import", { table: "" });
check("什么都没粘贴 → 提示", r.status === 400);
r = await editor.post("/events/import", { table: "名称\t类型\t阶段\t开始\t结束\t队伍\n杯赛 B\t杯赛\t\t2026/11/20 19:00\t2026/11/20 22:00\t星火电竞、残队\n逗号\t杯赛\t\t2026-11-21 19:00\t2026-11-21 22:00\t星火电竞,残队" });
check("导入赛程：逗号隔开的队伍 → 说第几行", r.status === 400 && r.text.includes("第 2 行：队伍要用顿号「、」隔开"));
r = await editor.post("/events/import", { table: "杯赛 B\t杯赛\t\t2026/11/20 19:00\t2026/11/20 22:00\t星火电竞、残队" });
check("导入赛程：Excel 的日期写法也认", r.location === "/events?ok=imported&added=1&changed=0"
  && eventList().find((x) => x.name === "杯赛 B").start === "2026-11-20T19:00:00+08:00");
r = await editor.post("/events/import", { table: "杯赛 B\t杯赛\t决赛\t2026/11/20 19:00\t2026/11/20 23:00\t星火电竞、残队" });
check("名称和开始时间一样 → 更新那一场", r.location === "/events?ok=imported&added=0&changed=1"
  && eventList().filter((x) => x.name === "杯赛 B").length === 1 && eventList().find((x) => x.name === "杯赛 B").stage === "决赛");
const table = parseTable('名称,"带,逗号",x\n"两行\n的格子",b,c');
check("CSV：引号里的逗号、换行", table[0][1] === "带,逗号" && table[1][0] === "两行 的格子");

// ---------- 发布 ----------
r = await viewer.get("/publish");
check("只读的能看发布页、没有发布按钮", r.status === 200 && !r.text.includes('action="/publish/test"') && r.text.includes("发布要找管理员"));
r = await editor.post("/publish/test");
check("没开发布的编辑不能发布", r.status === 403 && !env.PUBLIC.objects.has("esports_test.json"));
await add(editor, "残四", "残队", "douyu", "404");
r = await admin.get("/publish");
check("一支队几个人都行：4 个人的队也能发布", r.text.includes("检查过了，可以发布") && !r.text.includes("要正好"));
await editor.post(`/players/${playerList().find((x) => x.name === "残四").id}/delete`);
const third = () => playerList().find((x) => x.team === "残队" && x.room === "403");
await editor.post(`/players/${third().id}/save`, { name: "示例选手", team: "残队", platform: "douyu", room: "403" });
r = await admin.get("/publish");
check("还有示例数据 → 不能发布、按钮灰着", r.text.includes("还有示例数据（示例选手）") && r.text.includes("disabled"));
r = await admin.post("/publish/test");
check("硬发也不行", r.status === 400 && r.text.includes("没有发布") && !env.PUBLIC.objects.has("esports_test.json"));
await editor.post(`/players/${third().id}/save`, { name: "残三", team: "残队", platform: "douyu", room: "403" });
r = await admin.get("/publish");
check("改好了 → 可以发布、提醒队伍数和太早的赛事", r.text.includes("检查过了，可以发布") && r.text.includes("一般是 20 支")
  && r.text.includes("结束超过 7 天，不放进去"));
check("测试文件还没有", r.text.includes("线上还没有这个文件"));

r = await publisher.post("/publish/test");
check("能发布的编辑发测试文件", r.location === "/publish?ok=test" && env.PUBLIC.objects.has("esports_test.json"));
const testFile = env.PUBLIC.objects.get("esports_test.json");
const data = JSON.parse(testFile.body);
check("写的是 JSON、不缓存", testFile.options.httpMetadata.contentType === "application/json; charset=utf-8"
  && testFile.options.httpMetadata.cacheControl === "no-cache");
check("只写测试文件，正式文件没动", !env.PUBLIC.objects.has("esports.json") && env.PUBLIC.objects.size === 1);
check("版本、更新时间、五个平台都开", data.version === 1 && data.updated === "2026-11-17T12:00:00+08:00"
  && JSON.stringify(data.platforms) === '{"douyu":true,"huya":true,"bilibili":true,"douyin":true,"twitch":true}');
check("赛事按开始时间排、id 是时间加类型、太早的不放", data.events.map((x) => x.id).join() === "20261116-1900-cup,20261117-1000-scrim,20261117-1930-league,20261120-1900-cup");
check("选手的 id 是平台-房间号、只放用到的队", data.players.length === 9 && data.players.find((x) => x.name === "Kite").id === "douyu-520015");
check("记下谁发的", env.DATA.json(PUBLISHED).test.by === "能发布的编辑" && logTexts()[0].startsWith("发布测试文件：4 场赛事、3 支队、9 位选手"));
r = await admin.get("/publish?ok=test");
check("发布页显示线上现在的样子", r.text.includes("测试文件发好了") && r.text.includes("4 场赛事、3 支队、9 位选手") && r.text.includes("上次是 能发布的编辑"));

// 平台开关
r = await publisher.post("/publish/platform", { target: "test", platform: "douyu", on: "0" });
check("编辑不能改平台开关", r.status === 403);
r = await admin.post("/publish/platform", { target: "official", platform: "douyu", on: "0" });
check("正式文件还没有时不能改开关", r.status === 400 && r.text.includes("线上还没有正式文件"));
now += HOUR;
r = await admin.post("/publish/platform", { target: "test", platform: "douyu", on: "0" });
const switched = JSON.parse(env.PUBLIC.objects.get("esports_test.json").body);
check("关掉斗鱼：只改开关和更新时间", r.location === "/publish?ok=platform" && switched.platforms.douyu === false
  && switched.updated === "2026-11-17T13:00:00+08:00" && JSON.stringify(switched.events) === JSON.stringify(data.events));
check("改动记录写明", logTexts()[0] === "关掉平台 斗鱼（测试文件）：Evo 不在窗口里播它，改成“去这个平台看”");
await publisher.post("/publish/test");
check("再发布：关掉的开关照线上原样带着", JSON.parse(env.PUBLIC.objects.get("esports_test.json").body).platforms.douyu === false);
await admin.post("/publish/platform", { target: "test", platform: "douyu", on: "1" });
check("再打开", JSON.parse(env.PUBLIC.objects.get("esports_test.json").body).platforms.douyu === true);

r = await admin.post("/publish/official");
check("发正式文件", r.location === "/publish?ok=official" && JSON.parse(env.PUBLIC.objects.get("esports.json").body).events.length === 4);

// 同一时刻开始的同类赛事 → -2
await editor.post("/events/add", eventForm("同时的杯赛", "杯赛", "2026-11-20T19:00", "2026-11-20T21:00", ["北辰战队"]));
await admin.post("/publish/test");
check("同一时刻的同类赛事加 -2", JSON.parse(env.PUBLIC.objects.get("esports_test.json").body).events.some((x) => x.id === "20261120-1900-cup-2"));

// 写进去读回来不一样
const realGet = env.PUBLIC.get.bind(env.PUBLIC);
env.PUBLIC.get = async (key) => (key === "esports_test.json" ? { text: async () => "坏了" } : realGet(key));
r = await admin.post("/publish/test");
check("读回来不一样 → 提示再发一次", r.status === 400 && r.text.includes("读回来不一样"));
env.PUBLIC.get = realGet;

// 都太早了 → 只发选手（11-19 起：Evo 电竞页的"主播"一块列开直播的选手，没有赛事也有用）
now += 30 * DAY;
r = await admin.get("/publish");
check("赛事都结束超过 7 天 → 照样能发，提醒这次只发选手", !r.text.includes("还不能发布") && r.text.includes("这次只发选手"));
now -= 30 * DAY;

// ---------- 删除、首页、改动记录 ----------
e = eventList().find((x) => x.name === "很早的杯赛");
r = await editor.post(`/events/${e.id}/delete`);
check("删赛事", r.location === "/events?ok=deleted" && !eventList().some((x) => x.name === "很早的杯赛"));
r = await viewer.get("/");
check("首页：几位选手、几场赛事、正式文件谁发的、最近改动", r.text.includes("9 位选手、3 支队") && r.text.includes("场赛事")
  && r.text.includes("正式文件：黄指导") && r.text.includes("删除赛事「很早的杯赛」"));
r = await viewer.get("/log");
check("改动记录页", r.status === 200 && r.text.includes("从表格导入选手：新加 1 位，更新 1 位") && r.text.includes("战队改名 北辰 → 北辰战队"));
