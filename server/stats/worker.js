// Evo 匿名统计的接收程序（0.3.7 起，2026-10-22 用户决定）：Cloudflare Worker，数据存在 D1 数据库（表见 schema.sql）。
// 地址 https://stats.evotracker.cn。只有用户在 Evo 里同意了才会发过来（code/telemetry.py）。
//
// - POST /v1/report：{"id": 匿名编号, "version": Evo 版本号, "ping": 日活（每天一份，可以没有）, "matches": [每局，最多 100 个],
//   "usage": [每局对局中 Evo 自己的占用，最多 20 个，2026-11-15 以后的 Evo 才发，可以没有]}
//   日活按（编号, 日期）存一行，每局、每局的占用按（编号, 对局编号）存一行，再发同一个就覆盖
// - POST /v1/delete：{"id": 匿名编号}：删掉这个编号发过的所有数据（Evo 0.3.7、0.3.8 设置页的"关闭并删除已发送的数据"；
//   之后的版本去掉了这个按钮，2026-10-24 用户决定。老版本还在用，这里留着）
// - GET /v1/summary：全体玩家的汇总（Evo 数据页"全体玩家"，2026-10-24 用户决定；格式见"全体玩家的汇总"那一节）。
//   定时任务每天出一次存进 summary 表（每小时增量更新各天的部分和，见 refreshSummary），这里只读出来；还没算过就现算一次。
//   只有汇总的数字，没有任何一个编号的数据。Evo 里只有开了匿名统计的人才下载（限制在 Evo 那边，这个地址本身是公开的）
// - GET /v1/maps：地图轮换（Evo 总览页，2026-10-28 用户决定）：匹配、排位、轮换模式现在 / 下一张是什么图、什么时候换。
//   数据来自 Apex Legends Status 的接口（用户自己申请的 API Key，是 Worker 的机密变量 ALS_API_KEY：
//   wrangler secret put ALS_API_KEY，不进 git、不进 Evo；Key 要在对方网站上验证过 Discord 才能用，没验证时回 429）。
//   只有定时任务去取（每分钟看一眼，到了换图的时间、或者存着的超过 15 分钟才取，见 refreshMaps），整理成自己的格式
//   存进 summary 表（name = maps_v1）；这里只读出来，再多人打开也不会去上游（2026-10-28：原来每个 Cloudflare 机房
//   各取各的，换图那一刻一起去取，撞上对方每秒 5 次的限额）。取不到时照旧给上一次存的
// 只收认识的项，格式不对的那一局不存（整份格式不对就回 400）。不存 IP 地址、不写日志、不放 Cookie；
// 不回 CORS 头，网页里的脚本没法往这里发。
// 数据不自动删（2026-10-22 用户决定：留着做长期统计，比如看各赛季的变化）。
//
// 部署：cd server/stats && wrangler deploy（第一次：wrangler d1 create evo-stats --location apac，
// 把 database_id 填进 wrangler.toml，再 wrangler d1 execute evo-stats --remote --file schema.sql）
// 看统计：python tools/stats_report.py

const MAX_BODY = 256 * 1024;
const MAX_MATCHES = 100;
const ID = /^[0-9a-f]{32}$/;
const KEY = /^[0-9a-f]{20}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const SIZE = /^\d{2,5}x\d{2,5}$/;
const GAME_MODES = new Set(["fullscreen", "borderless", "windowed"]);
const ASPECTS = new Set(["auto", "16:9", "16:10", "4:3"]);
// 范围放得比游戏里可能的宽：只挡明显的乱数据，不误删少见的真实数据
const NUMBERS = { placement: [1, 99], kills: [0, 999], assists: [0, 999], knockdowns: [0, 999], damage: [0, 999999],
                  rp_delta: [-9999, 9999] };
const TEXTS = ["mode", "map", "legend", "tier", "rp_source"];
const MATE_NUMBERS = ["kills", "assists", "knockdowns", "damage"];  // 队友每局的数字（mate_stats，0.3.8 之后的 Evo 才发）
// 武器（weapons，0.4.2 起的 Evo 才发）：{武器编号: 秒数}，自己每把武器拿在手上几秒（只算这一局第一次拿到武器
// 60 秒以后的）；没记下武器是 null。编号见 Evo 的 config/weapons.json，新赛季会加，所以只查格式不查名单
const WEAPON = /^[a-z0-9_]{1,32}$/;
const MAX_WEAPONS = 40;
const WEAPON_SECONDS = [1, 7200];
const EDITABLE = new Set(["placement", "kills", "assists", "knockdowns", "participation", "damage", "rp_delta",
                         "mode", "map"]);

const MATE_COLUMNS = [1, 2].flatMap((i) => MATE_NUMBERS.map((f) => `mate${i}_${f}`));
// 对局中 Evo 自己的占用（usage，2026-11-15 用户决定）：每局从认出自己的界面到结束，Evo 全部进程平均占一个核的百分之几、
// 内存（专用工作集）的平均和最高。不含电脑的配置
const MAX_USAGE = 20;
const USAGE_MINUTES = [0, 600];
const USAGE_CPU_CORE = [0, 6400];  // 占一个核的 %：64 个核全占满是 6400
const USAGE_MEMORY = [1, 65536];  // MB

class Bad extends Error {}

function reply(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

// 可以为空的短文字（传奇、地图……）；太长、不是文字就当作格式不对
function text(value, max = 40) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || value.length > max) throw new Bad();
  return value;
}

function pattern(value, re) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !re.test(value)) throw new Bad();
  return value;
}

function number(value, [low, high]) {
  if (value === null || value === undefined) return null;
  if (!Number.isInteger(value) || value < low || value > high) throw new Bad();
  return value;
}

// 武器存成按编号排好的 JSON 文字，如 {"car":265,"nemesis":574}（汇总时用 json_each 拆开）
function weaponSeconds(value) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "object" || Array.isArray(value)) throw new Bad();
  const entries = Object.entries(value);
  if (entries.length > MAX_WEAPONS) throw new Bad();
  for (const [key, seconds] of entries) {
    if (!WEAPON.test(key) || number(seconds, WEAPON_SECONDS) === null) throw new Bad();
  }
  return JSON.stringify(Object.fromEntries(entries.sort(([a], [b]) => (a < b ? -1 : 1))));
}

// 可以带小数的数字（对局中的占用）
function decimal(value, [low, high]) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < low || value > high) throw new Bad();
  return value;
}

function usage(u) {
  if (typeof u !== "object" || u === null || typeof u.k !== "string" || !KEY.test(u.k)) throw new Bad();
  return { k: u.k, day: pattern(u.day, DAY), version: text(u.version, 20), minutes: decimal(u.minutes, USAGE_MINUTES),
           cpu_core: decimal(u.cpu_core, USAGE_CPU_CORE), memory: number(u.memory, USAGE_MEMORY),
           memory_peak: number(u.memory_peak, USAGE_MEMORY) };
}

function ping(p) {
  if (typeof p !== "object" || p === null) throw new Bad();
  const mode = p.game_mode ?? null;
  const aspect = p.aspect ?? null;
  if ((mode !== null && !GAME_MODES.has(mode)) || (aspect !== null && !ASPECTS.has(aspect))) throw new Bad();
  if (typeof p.day !== "string" || !DAY.test(p.day)) throw new Bad();
  return { day: p.day, screen: pattern(p.screen, SIZE), game_res: pattern(p.game_res, SIZE), game_mode: mode, aspect };
}

function match(m) {
  if (typeof m !== "object" || m === null || typeof m.k !== "string" || !KEY.test(m.k)) throw new Bad();
  const mates = m.mates ?? [];
  if (!Array.isArray(mates) || mates.length > 2) throw new Bad();
  const edited = m.edited ?? [];
  if (!Array.isArray(edited) || edited.some((f) => !EDITABLE.has(f))) throw new Bad();
  if (m.review !== undefined && typeof m.review !== "boolean") throw new Bad();
  const out = { k: m.k, day: pattern(m.day, DAY), version: text(m.version, 20),  // 版本号只限长度：旧数据、演示数据可能不是 x.y.z
                mate1: text(mates[0]), mate2: text(mates[1]), review: m.review ? 1 : 0,
                edited: edited.length ? [...new Set(edited)].sort().join(",") : null };
  for (const f of TEXTS) out[f] = text(m[f]);
  for (const [f, range] of Object.entries(NUMBERS)) out[f] = number(m[f], range);
  const stats = m.mate_stats ?? [];  // 和 mates 一一对应；老版本的 Evo 没有这一项
  if (!Array.isArray(stats) || stats.length > 2) throw new Bad();
  for (const i of [0, 1]) {
    const one = stats[i] ?? {};
    if (typeof one !== "object" || Array.isArray(one)) throw new Bad();
    for (const f of MATE_NUMBERS) out[`mate${i + 1}_${f}`] = number(one[f], NUMBERS[f]);
  }
  out.weapons = weaponSeconds(m.weapons);
  return out;
}

async function readJson(request) {
  if (!(request.headers.get("Content-Type") || "").startsWith("application/json")) throw new Bad();
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY) throw new Bad();
  const raw = await request.text();
  if (raw.length > MAX_BODY) throw new Bad();
  const body = JSON.parse(raw);
  if (typeof body !== "object" || body === null || typeof body.id !== "string" || !ID.test(body.id)) throw new Bad();
  return body;
}

async function report(body, env) {
  const version = text(body.version, 20);
  const now = new Date().toISOString();
  const statements = [];
  if (body.ping !== undefined && body.ping !== null) {
    const p = ping(body.ping);
    statements.push(env.DB.prepare(
      `INSERT INTO pings (id, day, version, screen, game_res, game_mode, aspect, received_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT (id, day) DO UPDATE SET version = excluded.version, screen = excluded.screen,
         game_res = excluded.game_res, game_mode = excluded.game_mode, aspect = excluded.aspect,
         received_at = excluded.received_at`,
    ).bind(body.id, p.day, version, p.screen, p.game_res, p.game_mode, p.aspect, now));
  }
  const matches = body.matches ?? [];
  if (!Array.isArray(matches) || matches.length > MAX_MATCHES) throw new Bad();
  let rejected = 0;
  for (const raw of matches) {
    let m;
    try {
      m = match(raw);
    } catch (e) {
      if (!(e instanceof Bad)) throw e;
      rejected += 1; // 这一局格式不对：不存，别的照收（Evo 那边算发过了，不会一直重发）
      continue;
    }
    statements.push(env.DB.prepare(
      `INSERT INTO matches (id, k, day, version, mode, map, legend, mate1, mate2, placement, kills, assists,
         knockdowns, damage, rp_delta, tier, rp_source, review, edited, received_at, ${MATE_COLUMNS.join(", ")}, weapons)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ${MATE_COLUMNS.map(() => "?").join(", ")}, ?)
       ON CONFLICT (id, k) DO UPDATE SET day = excluded.day, version = excluded.version, mode = excluded.mode,
         map = excluded.map, legend = excluded.legend, mate1 = excluded.mate1, mate2 = excluded.mate2,
         placement = excluded.placement, kills = excluded.kills, assists = excluded.assists,
         knockdowns = excluded.knockdowns, damage = excluded.damage, rp_delta = excluded.rp_delta,
         tier = excluded.tier, rp_source = excluded.rp_source, review = excluded.review, edited = excluded.edited,
         received_at = excluded.received_at, ${MATE_COLUMNS.map((c) => `${c} = excluded.${c}`).join(", ")},
         weapons = excluded.weapons`,
    ).bind(body.id, m.k, m.day, m.version, m.mode, m.map, m.legend, m.mate1, m.mate2, m.placement, m.kills,
           m.assists, m.knockdowns, m.damage, m.rp_delta, m.tier, m.rp_source, m.review, m.edited, now,
           ...MATE_COLUMNS.map((c) => m[c]), m.weapons));
  }
  const usages = body.usage ?? [];
  if (!Array.isArray(usages) || usages.length > MAX_USAGE) throw new Bad();
  for (const raw of usages) {
    let u;
    try {
      u = usage(raw);
    } catch (e) {
      if (!(e instanceof Bad)) throw e;
      rejected += 1;
      continue;
    }
    statements.push(env.DB.prepare(
      `INSERT INTO usage (id, k, day, version, minutes, cpu_core, memory, memory_peak, received_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT (id, k) DO UPDATE SET day = excluded.day, version = excluded.version, minutes = excluded.minutes,
         cpu_core = excluded.cpu_core, memory = excluded.memory, memory_peak = excluded.memory_peak,
         received_at = excluded.received_at`,
    ).bind(body.id, u.k, u.day, u.version, u.minutes, u.cpu_core, u.memory, u.memory_peak, now));
  }
  if (statements.length) await env.DB.batch(statements);
  return reply({ ok: true, stored: matches.length + usages.length - rejected, rejected });
}

async function remove(body, env) {
  const results = await env.DB.batch([
    env.DB.prepare("DELETE FROM pings WHERE id = ?").bind(body.id),
    env.DB.prepare("DELETE FROM matches WHERE id = ?").bind(body.id),
    env.DB.prepare("DELETE FROM usage WHERE id = ?").bind(body.id),
    // 全体玩家汇总的中间结果（summary_pairs）里这个编号的也删掉，先记下他打过哪几天：那几天的部分和下一个小时重建（见 refreshSummary）。
    // 要重建的日子和之前没重建完的合在一起
    env.DB.prepare(
      `INSERT INTO summary (name, body, made_at)
       SELECT ?1, json_group_array(day), ?2 FROM (SELECT DISTINCT day FROM summary_pairs WHERE id = ?3) WHERE true
       ON CONFLICT (name) DO UPDATE SET made_at = excluded.made_at, body = (SELECT json_group_array(value)
         FROM (SELECT value FROM json_each(summary.body) UNION SELECT value FROM json_each(excluded.body)))`,
    ).bind(DAYS_DIRTY, new Date().toISOString(), body.id),
    env.DB.prepare("DELETE FROM summary_pairs WHERE id = ?").bind(body.id),
  ]);
  return reply({ ok: true, deleted: results.slice(0, 3).reduce((n, r) => n + (r.meta?.changes ?? 0), 0) });
}

// ---------- 全体玩家的汇总（GET /v1/summary）----------
// 范围（全部 / 近 7 天 / 本赛季）× 模式（排位 / 匹配 / 外卡 / 未知）× 段位（排位才分：全部 / 白金及以下 / 钻石 / 大师以上），
// 键是 "范围.模式.段位"，如 "all.ranked.all"、"7d.ranked.diamond"、"season.pubs.all"、"all.wildcard.all"、"all.unknown.all"。
// 模式口径与客户端 code/modes.py 一致：ranked 是排位，wildcard 是外卡，pubs 包含匹配和其他模式，
// unknown 单独统计未识别模式。各游戏语言识别到的外卡共用 wildcard。每份汇总包含以下字段：
// - players、matches：多少个编号、多少局
// - legends：每个传奇一行，按出场次数从多到少。全队三个人都算（队友多半是随机匹配到的；名次全队一样）：
//   n 出场次数、pick 选用率（n ÷ 所有出场次数）、win 胜率、top5 前五率、place 场均名次、kills 场均击杀、
//   damage 场均伤害。表现只算不是待确认的局；击杀、伤害只算读到了的（队友的要 0.3.8 之后的 Evo 才发）
// - combos：全队三个传奇（不管谁用的哪个）每种一行，局数最多的 TOP_COMBOS 种：legends（按名字排好）、
//   n 局数、rate 选取率（n ÷ 全队传奇都认出的局数）、place 场均名次。同一局两个一样的传奇（认错了）不算
// - people：每个编号（自己的局，打了 MIN_PLAYER_GAMES 局以上）的场均名次 place、胜率 win、场均击杀 kills、
//   场均伤害 damage，各自从小到大排好；人多时只留 MAX_POINTS 个等间隔的点。Evo 拿自己的数字和它比，算"超过 X% 的玩家"。
//   2026-10-28 六维图（Evo 总览页、数据页）加了：场均助攻 assists、前五率 top5、击杀参与率 kp（(击杀 + 助攻) ÷ 全队击杀，
//   全队击杀 = 自己的 + 两名队友的，只算三个人的击杀都读到了的局；Evo 那边用老板分算，道理一样：队友的击杀不是自己的
//   助攻就是老板分）、英雄池 pool（局数占 POOL_SHARE 以上的传奇有几个，待确认的局也算）。Evo 用它们的平均和前 5% 画图
// - weapons（2026-10-28）：每把武器一行，按拿在手上的时间从多到少。只有自己的（队友的武器认不出），只算每局第一次
//   拿到武器 60 秒以后的（Evo 那边算好发来）：key 武器编号、n 拿过它的局数、share 占所有武器拿在手上时间的多少、
//   rate 在多少比例的对局里拿过（n ÷ weapon_games）。weapon_games：记下了武器的局数（0.4.1 以前的 Evo 不发武器）。
//   待确认的局也算（和传奇的出场次数一样，武器和名次、RP 读得准不准无关）
// tiers（只分范围）：每个编号最近一局排位的段位，各段位多少人
// "近 7 天"按用户电脑上的日期，从 UTC 的今天往前数 6 天算起（recent_from）
// "本赛季"（2026-11-03 加的）也按用户电脑上的日期（Evo 只发日期，不发几点、不发赛季），从开赛后第一个整天算起（SEASON.from）：
// 开赛那天一半是上赛季的局（开赛在太平洋时间上午 10 点，国内是第二天凌晨 2 点），所以那天不算
//
// 读数据库（2026-11-19 改成按天增量算，用户同意）：原来每次从头扫 matches 表约 20 遍，随局数一起涨（2026-11-08 约 5 万局时一次
// 83 万行，超过一次 D1 免费额度（整个账号一天读 500 万行），之后改成每天算一次；11-19 约 11 万局时一次 224 万行）。现在先按人、按天存好部分和：
// - summary_pairs：每名玩家每天一行，那天他每一局里汇总用得到的几项（snapshot），按对局编号 k 存
// - summary_days：每天一行，那天全体的部分和（pairParts 加起来：传奇、组合、武器按"模式|段位组"分；每名玩家的和、英雄池；段位）
// 定时任务每小时只读这一小时收到的局（received_at 有索引）和这些局所在的那几行（catchUp）：重发的局换掉旧的那一项，那一天的
// 部分和减去这名玩家原来的、加上新的（都是整数，加减没有误差）。每天第一次运行时把各天的部分和加起来，得到"全部 / 近 7 天 /
// 本赛季"（assemble）：每一项的算法和原来的 SQL 一样，结果一模一样（2026-11-19 用线上数据的副本核对过，见 docs/底座.md"匿名统计"）。
// 每天还挑一天从 matches 从头重算、和存着的比（verifyDay），对不上就换成重算的、记在状态里：万一增量算漏了什么，也会自己纠正。
// 第一次、DAYS_VERSION 改了时从头重建（rebuildDays，每小时建 REBUILD_DAYS 天，建完以前照旧给上一次的汇总）；
// /v1/delete 删了数据时只重建这个编号打过的那几天
// 汇总格式版本须与客户端 global_stats.FORMAT 一致。
const SUMMARY_NAME = "v1";
// 定时任务（wrangler.toml 的 crons 里也写着；另一个 "* * * * *" 是地图轮换）：每小时第 7 分钟一次。汇总每个 UTC 日子只出一次
const SUMMARY_CRON = "7 * * * *";
const DAYS_STATE = "v1_days";  // summary 表里记增量算到哪了的那一行（body 见 refreshSummary）
const DAYS_DIRTY = "v1_dirty";  // /v1/delete 删过数据：body 是要重建的日子（JSON 数组），下一个小时重建
const DAYS_VERSION = 3;  // 重新汇总已有战绩，保留其他模式的大逃杀归属
const OVERLAP = 10 * 60 * 1000;  // 每次往前多读 10 分钟收到的局，和正在写进来的请求错开（同一局再算一遍，结果不变）
// 一次最多读这么多新收到的局（Evo 出新版本、大家重发旧局时会多），剩下的下一个小时接着读，读完才出汇总。
// 一个请求的局收到时间都一样（最多 100 局），所以每次都能往前读
const MAX_CHANGED = 10000;
const REBUILD_DAYS = 4;  // 重建时每小时建几天（一天约 5 千局；建的这一次不做别的，免得一次算太久）
const VERIFY_HOUR = 12;  // 每天 UTC 几点以后核对（和出汇总错开，每次运行算的东西少一些）
const PARAM_SIZE = 500 * 1024;  // 一句 SQL 带的 JSON 最长多少（写 summary_pairs 时分成几句）
const RECENT_DAYS = 7;
// 本赛季：第几赛季、从哪天算起。新赛季开始时和 Evo 的 config/seasons.json 一起改，再 wrangler deploy
const SEASON = { number: 31, from: "2026-11-04" };
const RANGES = ["all", "7d", "season"];
const TOP_COMBOS = 30;
const MIN_PLAYER_GAMES = 3;
const MAX_POINTS = 201;
const POOL_SHARE = 0.05;  // 英雄池：局数占这么多以上的传奇才算（和 Evo 的 radar.POOL_SHARE 一样）
const TIER_ORDER = ["菜鸟", "青铜", "白银", "黄金", "白金", "钻石", "大师", "Apex 猎杀者"];
const LOW_TIERS = new Set(["菜鸟", "青铜", "白银", "黄金", "白金"]);
// 段位组："low" 白金及以下、"diamond"、"master" 大师以上，没有段位或认不出是 ""（原来 SQL 里的 NULL）
function tierGroup(tier) {
  if (LOW_TIERS.has(tier)) return "low";
  if (tier === "钻石") return "diamond";
  return tier === "大师" || tier === "Apex 猎杀者" ? "master" : "";
}
// 部分和里每个数组各位是什么（和原来 SQL 查出来的列一样；PEOPLE_FIELDS 也是 assemble 里 p[0]、p[1]…… 的意思）
const LEGEND_FIELDS = ["n", "np", "sp", "wins", "top5", "nk", "sk", "nd", "sd"];
const COMBO_FIELDS = ["n", "np", "sp"];
const WEAPON_FIELDS = ["n", "s"];
const PEOPLE_FIELDS = ["n", "np", "sp", "wins", "top5", "nk", "sk", "na", "sa", "nd", "sd", "nkp", "kpa", "kps"];

function round(value, digits) {
  const f = 10 ** digits;
  return Math.round(value * f) / f;
}

function ratio(part, whole, digits) {
  return whole ? round(part / whole, digits) : null;
}

// 这一行（某个模式 m、段位组 g、是不是近 7 天、是不是本赛季）算进哪几份汇总。
// m 是用于每日汇总的模式，other 单独保留到最后合并。
// - all：每局都算一次（含历史未知模式），段位组不细分
// - br：大逃杀 = 排位 + 匹配 + 外卡（有名次的玩法），段位组不细分；未知不算
const BR_MODES = new Set(["ranked", "pubs", "wildcard"]);

function sliceKeys(row) {
  const ranges = ["all", ...(row.recent ? ["7d"] : []), ...(row.season ? ["season"] : [])];
  const groups = row.m === "ranked" && row.g ? ["all", row.g] : ["all"];
  const statsMode = row.m === "other" ? "pubs" : row.m;
  const keys = ranges.flatMap((r) => groups.map((g) => `${r}.${statsMode}.${g}`));
  for (const r of ranges) {
    keys.push(`${r}.all.all`);
    if (BR_MODES.has(row.m)) keys.push(`${r}.br.all`);
  }
  return keys;
}

function emptySlices() {
  const out = {};
  for (const r of RANGES) {
    for (const g of ["all", "low", "diamond", "master"]) out[`${r}.ranked.${g}`] = {};
    out[`${r}.pubs.all`] = {};
    out[`${r}.wildcard.all`] = {};
    out[`${r}.unknown.all`] = {};
    out[`${r}.all.all`] = {};
    out[`${r}.br.all`] = {};
  }
  return out;
}

const enough = (n) => n >= MIN_PLAYER_GAMES;  // 每名玩家的场均：打了这么多局以上才算

// {键: 数组} 逐位加进 target（target 里没有的键放一份拷贝）
function mergeVectors(target, source) {
  for (const [key, v] of Object.entries(source)) {
    const t = target[key];
    if (!t) target[key] = v.slice();
    else for (let i = 0; i < v.length; i++) t[i] += v[i];
  }
}

// 一名玩家的 {s, l} 加进 target[id]
function mergePerson(target, id, P) {
  const Q = target[id];
  if (!Q) {
    target[id] = { s: P.s.slice(), l: { ...P.l } };
    return;
  }
  for (let i = 0; i < P.s.length; i++) Q.s[i] += P.s[i];
  for (const [name, n] of Object.entries(P.l)) Q.l[name] = (Q.l[name] ?? 0) + n;
}

function points(values, digits) {
  const sorted = values.map((v) => round(v, digits)).sort((a, b) => a - b);
  if (sorted.length <= MAX_POINTS) return sorted;
  return Array.from({ length: MAX_POINTS }, (_, i) => sorted[Math.round((i * (sorted.length - 1)) / (MAX_POINTS - 1))]);
}

// 一局里汇总用得到的几项，存进 summary_pairs（顺序见 pairParts）。
// 每日汇总保留大逃杀的具体模式，另外记录未知和其他。
const GAME_COLUMNS = `id, k, day, mode, tier, review, placement, legend, mate1, mate2, kills, assists, damage, mate1_kills, mate1_damage,
  mate2_kills, mate2_damage, weapons`;
// 每日汇总的模式分组，其他模式在生成公开统计时并进匹配。
function modeGroup(mode) {
  if (BR_MODES.has(mode)) return mode;
  return mode === null || mode === undefined ? "unknown" : "other";
}
function snapshot(r) {
  return [modeGroup(r.mode), r.tier, r.review, r.placement, r.legend, r.mate1, r.mate2, r.kills, r.assists, r.damage,
          r.mate1_kills, r.mate1_damage, r.mate2_kills, r.mate2_damage, r.weapons === null ? null : JSON.parse(r.weapons)];
}

const newGroup = () => ({ l: {}, c: {}, w: {}, wg: 0 });
const emptyDay = () => ({ g: {}, p: {}, t: {} });

// 一名玩家一天的局（{k: snapshot}）→ 部分和，每一项和原来的 SQL 一样算（见上面的说明：表现只算不是待确认的局，击杀、伤害只算读到了的……）：
// - g（全体的）：按"模式|段位组"（如 "ranked|diamond"、"pubs|"、"wildcard|"）分，段位组只有排位才有，
//   l 传奇 {名字: LEGEND_FIELDS}（全队三个人都算）、
//   c 组合 {"a|b|z": COMBO_FIELDS}、w 武器 {编号: WEAPON_FIELDS}、wg 记下了武器的局数
// - p（这名玩家的）：按"模式|段位组"分，s 自己的 PEOPLE_FIELDS、l 自己每个传奇用了几局（英雄池）
// - t：这一天排位局里的段位（排好序）
function pairParts(games) {
  const g = {};
  const p = {};
  const t = new Set();
  for (const [mode, tier, review, place, legend, mate1, mate2, kills, assists, damage, k1, d1, k2, d2, weapons]
    of Object.values(games)) {
    const mg = `${mode}|${mode === "ranked" ? tierGroup(tier) : ""}`;
    const ok = review === 0;  // 不是待确认的局（SQL 里 review = 0）
    const placed = ok && place !== null;
    const G = (g[mg] ??= newGroup());
    for (const [name, k, d] of [[legend, kills, damage], [mate1, k1, d1], [mate2, k2, d2]]) {
      if (name === null) continue;
      const v = (G.l[name] ??= [0, 0, 0, 0, 0, 0, 0, 0, 0]);
      v[0] += 1;
      if (placed) {
        v[1] += 1;
        v[2] += place;
        if (place === 1) v[3] += 1;
        if (place <= 5) v[4] += 1;
      }
      if (ok && k !== null) { v[5] += 1; v[6] += k; }
      if (ok && d !== null) { v[7] += 1; v[8] += d; }
    }
    // 组合：三个传奇都认出、互不相同；按名字排好（JS 按 UTF-16 比、SQL 的 min、max 按 UTF-8 比，常用汉字两种比法一样）
    if (legend !== null && mate1 !== null && mate2 !== null && legend !== mate1 && legend !== mate2 && mate1 !== mate2) {
      const v = (G.c[[legend, mate1, mate2].sort().join("|")] ??= [0, 0, 0]);
      v[0] += 1;
      if (placed) { v[1] += 1; v[2] += place; }
    }
    if (weapons !== null) {
      G.wg += 1;
      for (const [w, seconds] of Object.entries(weapons)) {
        const v = (G.w[w] ??= [0, 0]);
        v[0] += 1;
        v[1] += seconds;
      }
    }
    const P = (p[mg] ??= { s: new Array(PEOPLE_FIELDS.length).fill(0), l: {} });
    const s = P.s;
    s[0] += 1;
    if (placed) {
      s[1] += 1;
      s[2] += place;
      if (place === 1) s[3] += 1;
      if (place <= 5) s[4] += 1;
    }
    if (ok && kills !== null) { s[5] += 1; s[6] += kills; }
    if (ok && assists !== null) { s[7] += 1; s[8] += assists; }
    if (ok && damage !== null) { s[9] += 1; s[10] += damage; }
    // 击杀参与率算哪些局：三个人的击杀和自己的助攻都读到了（队友的击杀 0.3.8 之后的 Evo 才发）
    if (ok && kills !== null && assists !== null && k1 !== null && k2 !== null) {
      s[11] += 1;
      s[12] += kills + assists;
      s[13] += kills + k1 + k2;
    }
    if (legend !== null) P.l[legend] = (P.l[legend] ?? 0) + 1;
    if (mode === "ranked" && tier !== null) t.add(tier);
  }
  return { g, p, t: [...t].sort() };
}

// 把一名玩家一天的 g 加进（sign = 1）或减出（sign = -1）那一天全体的；减到 0 局的项删掉（和从头算时没有这一项一样）
function addGroups(day, groups, sign) {
  for (const [mg, G] of Object.entries(groups)) {
    const D = (day.g[mg] ??= newGroup());
    for (const f of ["l", "c", "w"]) {
      for (const [key, values] of Object.entries(G[f])) {
        const d = (D[f][key] ??= values.map(() => 0));
        for (let i = 0; i < values.length; i++) d[i] += sign * values[i];
        if (d[0] === 0) delete D[f][key];
      }
    }
    D.wg += sign * G.wg;
    if (!D.wg && [D.l, D.c, D.w].every((o) => !Object.keys(o).length)) delete day.g[mg];
  }
}

// 那一天里这名玩家的部分和从 before 换成 after（第一次出现时 before 是 null）
function putPair(day, id, before, after) {
  if (before) addGroups(day, before.g, -1);
  addGroups(day, after.g, 1);
  if (Object.keys(after.p).length) day.p[id] = after.p;
  else delete day.p[id];
  if (after.t.length) day.t[id] = after.t;
  else delete day.t[id];
}

// 从 matches 里一天的局从头算：{pairs: {编号: {k: snapshot}}, body: 那一天的部分和}
function buildDay(rows) {
  const pairs = {};
  for (const r of rows) (pairs[r.id] ??= {})[r.k] = snapshot(r);
  const body = emptyDay();
  for (const [id, games] of Object.entries(pairs)) putPair(body, id, null, pairParts(games));
  return { pairs, body };
}

const isEmptyDay = (body) => !Object.keys(body.g).length && !Object.keys(body.p).length && !Object.keys(body.t).length;

// 两份部分和是不是一样（不管键的先后）
function same(a, b) {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null || Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => Object.hasOwn(b, k) && same(a[k], b[k]));
}

// 原来 SQL 里 GROUP BY 出来的行的先后（段位组 NULL、diamond、low、master；近 7 天、本赛季 NULL、0、1）：
// 组合的局数、场均名次都一样时按这个先后排（原来的 sort 是稳定的，一样的就照 SQL 出来的先后），所以要照样排才能一模一样
const G_ORDER = { "": 0, diamond: 1, low: 2, master: 3 };
const flagOrder = (v) => (v === null ? 0 : v + 1);
function compareNames(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
}

// 各天的部分和（Map 日期 → body，没有日期的局是 ""）→ 全体玩家的汇总
function assemble(days, now) {
  const today = Date.parse(now.toISOString().slice(0, 10) + "T00:00:00Z");
  const recentFrom = new Date(today - (RECENT_DAYS - 1) * 86400000).toISOString().slice(0, 10);
  // 先把"近 7 天、本赛季"两个标记一样的日子加在一起（它们算进的汇总份数一样），再分到各份汇总里，少做很多遍加法
  const classes = new Map();  // "近 7 天|本赛季" → {recent, season, g: {模式|段位组: 全体的}, p: {模式|段位组: {编号: 这名玩家的}}}
  const latestTier = {};  // 编号 → [最近一局排位的日期, 那天的段位]
  for (const [day, body] of days) {
    // 和原来 SQL 里的 day >= ? 一样：没有日期的局两样都是 NULL（只算进"全部"）
    const recent = day === "" ? null : day >= recentFrom ? 1 : 0;
    const season = day === "" ? null : day >= SEASON.from ? 1 : 0;
    const name = `${recent}|${season}`;
    if (!classes.has(name)) classes.set(name, { recent, season, g: {}, p: {} });
    const C = classes.get(name);
    for (const [mg, G] of Object.entries(body.g)) {
      const D = (C.g[mg] ??= newGroup());
      mergeVectors(D.l, G.l);
      mergeVectors(D.c, G.c);
      mergeVectors(D.w, G.w);
      D.wg += G.wg;
    }
    for (const [id, groups] of Object.entries(body.p)) {
      for (const [mg, P] of Object.entries(groups)) mergePerson((C.p[mg] ??= {}), id, P);
    }
    if (day !== "") {
      for (const [id, list] of Object.entries(body.t)) if (!latestTier[id] || day > latestTier[id][0]) latestTier[id] = [day, list];
    }
  }
  // 每份汇总的和（数组，各位见 LEGEND_FIELDS 等）；组合多记一个 order：原来 SQL 出来的先后（见 G_ORDER）
  const legends = emptySlices();
  const combos = emptySlices();
  const weapons = emptySlices();
  const people = emptySlices();
  const weaponGames = {};
  for (const C of classes.values()) {
    for (const mg of new Set([...Object.keys(C.g), ...Object.keys(C.p)])) {
      const [m, g] = mg.split("|");
      const order = (G_ORDER[g] * 3 + flagOrder(C.recent)) * 3 + flagOrder(C.season);
      const G = C.g[mg];
      const P = C.p[mg] ?? {};
      for (const key of sliceKeys({ m, g: g || null, recent: C.recent, season: C.season })) {
        if (G) {
          mergeVectors(legends[key], G.l);
          mergeVectors(weapons[key], G.w);
          weaponGames[key] = (weaponGames[key] ?? 0) + G.wg;
          const target = combos[key];
          for (const [names, v] of Object.entries(G.c)) {
            const c = target[names];
            if (!c) target[names] = [v[0], v[1], v[2], order];
            else {
              c[0] += v[0];
              c[1] += v[1];
              c[2] += v[2];
              if (order < c[3]) c[3] = order;
            }
          }
        }
        for (const [id, Q] of Object.entries(P)) mergePerson(people[key], id, Q);
      }
    }
  }

  // 下面每一项的算法和原来一样（原来是带名字的一行，现在是数组：n = v[0]……见 LEGEND_FIELDS 等）
  const slices = {};
  for (const key of Object.keys(legends)) {
    const picks = Object.values(legends[key]).reduce((sum, l) => sum + l[0], 0);
    const squads = Object.values(combos[key]).reduce((sum, c) => sum + c[0], 0);
    const ids = Object.values(people[key]).map((p) => p.s);
    const held = Object.values(weapons[key]).reduce((sum, w) => sum + w[1], 0);
    // 组合：局数多的在前，一样多时场均名次小的在前，再一样时照原来 SQL 出来的先后（order、再按名字）
    const comboList = Object.entries(combos[key]).map(([names, c]) => [names, c, ratio(c[2], c[1], 2)]);
    comboList.sort(([a, x, pa], [b, y, pb]) => y[0] - x[0] || (pa ?? 99) - (pb ?? 99) || x[3] - y[3]
      || compareNames(a.split("|"), b.split("|")));
    slices[key] = {
      players: ids.length,
      matches: ids.reduce((sum, p) => sum + p[0], 0),
      legends: Object.entries(legends[key])
        .sort(([a, x], [b, y]) => y[0] - x[0] || (a < b ? -1 : 1))
        .map(([name, l]) => ({
          name, n: l[0], pick: ratio(l[0], picks, 4), win: ratio(l[3], l[1], 4), top5: ratio(l[4], l[1], 4),
          place: ratio(l[2], l[1], 2), kills: ratio(l[6], l[5], 2), damage: ratio(l[8], l[7], 0),
        })),
      combos: comboList.slice(0, TOP_COMBOS)
        .map(([names, c, place]) => ({ legends: names.split("|"), n: c[0], rate: ratio(c[0], squads, 4), place })),
      // 每名玩家的场均：s = [n, np, sp, wins, top5, nk, sk, na, sa, nd, sd, nkp, kpa, kps]
      people: {
        place: points(ids.filter((p) => enough(p[1])).map((p) => p[2] / p[1]), 2),
        win: points(ids.filter((p) => enough(p[1])).map((p) => p[3] / p[1]), 4),
        kills: points(ids.filter((p) => enough(p[5])).map((p) => p[6] / p[5]), 2),
        damage: points(ids.filter((p) => enough(p[9])).map((p) => p[10] / p[9]), 0),
        assists: points(ids.filter((p) => enough(p[7])).map((p) => p[8] / p[7]), 2),
        top5: points(ids.filter((p) => enough(p[1])).map((p) => p[4] / p[1]), 4),
        kp: points(ids.filter((p) => enough(p[11]) && p[13] > 0).map((p) => Math.min(1, p[12] / p[13])), 4),
        // 英雄池：每个编号用了几个传奇（局数占 POOL_SHARE 以上的），局数太少的编号不算
        pool: points(Object.values(people[key]).map((p) => Object.values(p.l))
          .filter((ns) => enough(ns.reduce((a, b) => a + b, 0)))
          .map((ns) => {
            const total = ns.reduce((a, b) => a + b, 0);
            return ns.filter((n) => n / total >= POOL_SHARE).length;
          }), 0),
      },
      weapon_games: weaponGames[key] ?? 0,
      weapons: Object.entries(weapons[key])
        .sort(([a, x], [b, y]) => y[1] - x[1] || (a < b ? -1 : 1))
        .map(([name, w]) => ({ key: name, n: w[0], share: ratio(w[1], held, 4), rate: ratio(w[0], weaponGames[key], 4) })),
    };
  }

  // 段位分布：每个编号最近一局排位的段位（同一天有两个段位时算高的那个）。原来 SQL 按编号排好出来，counts 里段位的先后照它
  const rank = (tier) => TIER_ORDER.indexOf(tier);
  const tierIds = Object.keys(latestTier).sort();
  const tiers = {};
  for (const [range, from] of [["all", ""], ["7d", recentFrom], ["season", SEASON.from]]) {
    const counts = {};
    let players = 0;
    for (const id of tierIds) {
      const [day, list] = latestTier[id];
      if (day < from) continue;
      let best = null;
      for (const tier of list) if (best === null || rank(tier) > rank(best)) best = tier;  // 一样高（认不出的段位）取排在前面的
      players += 1;
      counts[best] = (counts[best] ?? 0) + 1;
    }
    tiers[range] = { players, counts };
  }
  return { v: 1, made_at: now.toISOString(), recent_from: recentFrom, season: SEASON.number, season_from: SEASON.from,
           slices, tiers };
}

function saveRow(db, name, body, madeAt, dataAt) {
  return db.prepare(
    `INSERT INTO summary (name, body, made_at, data_at) VALUES (?, ?, ?, ?)
     ON CONFLICT (name) DO UPDATE SET body = excluded.body, made_at = excluded.made_at, data_at = excluded.data_at`,
  ).bind(name, body, madeAt, dataAt);
}

const saveState = (db, state, now) => saveRow(db, DAYS_STATE, JSON.stringify(state), now.toISOString(), null);

function saveDay(db, day, body) {
  return isEmptyDay(body)
    ? db.prepare("DELETE FROM summary_days WHERE day = ?").bind(day)
    : db.prepare("INSERT INTO summary_days (day, body) VALUES (?, ?) ON CONFLICT (day) DO UPDATE SET body = excluded.body")
      .bind(day, JSON.stringify(body));
}

// matches 里一天的局（没有日期的局是 ""）：用 day 的索引，只读那一天的行
function dayRows(db, day) {
  return db.prepare(`SELECT ${GAME_COLUMNS} FROM matches WHERE day IS ?1`).bind(day === "" ? null : day);
}

// 写 summary_pairs：[[日期, 编号, {k: snapshot}], ...]，几百行合成一句（一句带的 JSON 不超过 PARAM_SIZE）
function savePairs(db, rows) {
  const out = [];
  let chunk = [];
  let size = 0;
  const flush = () => {
    if (!chunk.length) return;
    out.push(db.prepare(
      `INSERT INTO summary_pairs (day, id, games)
       SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]') FROM json_each(?1) WHERE true
       ON CONFLICT (day, id) DO UPDATE SET games = excluded.games`,
    ).bind(`[${chunk.join(",")}]`));
    chunk = [];
    size = 0;
  };
  for (const [day, id, games] of rows) {
    const item = JSON.stringify([day, id, JSON.stringify(games)]);
    if (size + item.length > PARAM_SIZE) flush();
    chunk.push(item);
    size += item.length + 1;
  }
  flush();
  return out;
}

// 从 matches 重建一天：summary_pairs 里那一天的整个换掉、summary_days 那一行换掉（返回要写的几句）
function replaceDay(db, day, rows) {
  const { pairs, body } = buildDay(rows);
  return [db.prepare("DELETE FROM summary_pairs WHERE day = ?").bind(day),
          ...savePairs(db, Object.entries(pairs).map(([id, games]) => [day, id, games])), saveDay(db, day, body)];
}

// 读 summary_pairs 里这几名玩家这几天的行：[[日期, 编号], ...] → Map("日期\t编号" → {k: snapshot})。按主键一行一行找
async function loadPairs(db, keys) {
  const reads = [];
  for (let i = 0; i < keys.length; i += 500) {
    reads.push(db.prepare(
      `SELECT p.day, p.id, p.games FROM json_each(?1) AS j CROSS JOIN summary_pairs AS p
       ON p.day = json_extract(j.value, '$[0]') AND p.id = json_extract(j.value, '$[1]')`,
    ).bind(JSON.stringify(keys.slice(i, i + 500))));
  }
  const out = new Map();
  if (reads.length) {
    for (const r of await db.batch(reads)) for (const row of r.results) out.set(`${row.day}\t${row.id}`, JSON.parse(row.games));
  }
  return out;
}

// 读 summary_days 里这几天：Map(日期 → 部分和)
async function loadDays(db, days) {
  if (!days.length) return new Map();
  const rows = (await db.prepare("SELECT day, body FROM summary_days WHERE day IN (SELECT value FROM json_each(?1))")
    .bind(JSON.stringify(days)).all()).results;
  return new Map(rows.map((r) => [r.day, JSON.parse(r.body)]));
}

// 从头重建的开始：清掉两张表，记下现在最新一局的收到时间（之后收到的局，建完再增量读）和要建哪几天
async function startRebuild(db, now) {
  const [newest, days, undated] = (await db.batch([
    db.prepare("SELECT MAX(received_at) AS t FROM matches"),
    // 一天一天跳着找（每次只读索引里的一两行），不用把 matches 读一遍
    db.prepare(`WITH RECURSIVE d(day) AS (SELECT MIN(day) FROM matches
       UNION ALL SELECT (SELECT MIN(day) FROM matches WHERE day > d.day) FROM d WHERE d.day IS NOT NULL)
     SELECT day FROM d WHERE day IS NOT NULL`),
    db.prepare("SELECT 1 AS x FROM matches WHERE day IS NULL LIMIT 1"),
  ])).map((r) => r.results);
  const state = { v: DAYS_VERSION, mark: newest[0]?.t ?? "", more: false, todo: [...(undated.length ? [""] : []),
                  ...days.map((r) => r.day)], checked: null, verified: "", fixed: 0 };
  await db.batch([db.prepare("DELETE FROM summary_pairs"), db.prepare("DELETE FROM summary_days"), saveState(db, state, now)]);
  return state;
}

// 重建 state.todo 里前 n 天
async function rebuildDays(db, state, n, now) {
  const todo = state.todo.slice(0, n);
  const results = await db.batch(todo.map((day) => dayRows(db, day)));
  state.todo = state.todo.slice(todo.length);
  await db.batch([...todo.flatMap((day, i) => replaceDay(db, day, results[i].results)), saveState(db, state, now)]);
}

// 增量：读上次以后收到的局（往前多读 OVERLAP；上次没读完时从停下的地方接着读，一次最多 MAX_CHANGED 局），
// 换掉 summary_pairs 里这几名玩家这几天的，更新那几天的部分和。received_at 有索引，只读这几行
async function catchUp(db, state, now) {
  const before = JSON.stringify(state);
  const since = state.more || !state.mark ? state.mark : new Date(Date.parse(state.mark) - OVERLAP).toISOString();
  const changed = (await db.prepare(
    `SELECT ${GAME_COLUMNS}, received_at FROM matches WHERE received_at >= ?1 ORDER BY received_at LIMIT ?2`,
  ).bind(since, MAX_CHANGED).all()).results;
  state.more = changed.length === MAX_CHANGED;
  const last = changed[changed.length - 1]?.received_at ?? "";
  if (last > state.mark) state.mark = last;
  const groups = new Map();  // "日期\t编号" → {day, id, games: 新收到的局 {k: snapshot}}
  for (const r of changed) {
    const day = r.day ?? "";
    const key = `${day}\t${r.id}`;
    if (!groups.has(key)) groups.set(key, { day, id: r.id, games: {} });
    groups.get(key).games[r.k] = snapshot(r);
  }
  const old = await loadPairs(db, [...groups.values()].map((g) => [g.day, g.id]));
  const changes = [...groups].filter(([key, g]) => {
    const stored = old.get(key);
    return !stored || !Object.entries(g.games).every(([k, s]) => same(stored[k], s));  // 和存着的一样（重发了没改的局）就不用动
  });
  const days = await loadDays(db, [...new Set(changes.map(([, g]) => g.day))]);
  const pairs = [];
  for (const [key, g] of changes) {
    const stored = old.get(key) ?? null;
    const after = { ...stored, ...g.games };
    if (!days.has(g.day)) days.set(g.day, emptyDay());
    putPair(days.get(g.day), g.id, stored && pairParts(stored), pairParts(after));
    pairs.push([g.day, g.id, after]);
  }
  if (!pairs.length && JSON.stringify(state) === before) return;
  await db.batch([...savePairs(db, pairs), ...[...days].map(([day, body]) => saveDay(db, day, body)), saveState(db, state, now)]);
}

// 核对：两天以前的日子轮着挑一天（最近两天一直在变），从 matches 从头算，和增量算出来的比；对不上就换成重算的
async function verifyDay(db, state, now) {
  const today = now.toISOString().slice(0, 10);
  const until = new Date(Date.parse(today) - 2 * 86400000).toISOString().slice(0, 10);
  const next = "SELECT day FROM summary_days WHERE day > ?1 AND day < ?2 ORDER BY day LIMIT 1";
  const pick = (await db.prepare(next).bind(state.checked, until).first())?.day
    ?? (await db.prepare(next).bind("\u0000", until).first())?.day  // 轮完一遍：从头（没有日期的 "" 也在内）
    ?? (await db.prepare("SELECT day FROM summary_days WHERE day = ''").first())?.day;
  state.verified = today;
  const writes = [];
  if (pick !== undefined) {
    const [rows, stored] = await db.batch([dayRows(db, pick), db.prepare("SELECT body FROM summary_days WHERE day = ?").bind(pick)]);
    if (!same(buildDay(rows.results).body, JSON.parse(stored.results[0]?.body ?? "null") ?? emptyDay())) {
      state.fixed += 1;
      writes.push(...replaceDay(db, pick, rows.results));
    }
    state.checked = pick;
  }
  await db.batch([...writes, saveState(db, state, now)]);
}

// 定时任务（每小时）、还没算过时的 GET /v1/summary 叫它。force：一次做完（不分几个小时）、不管今天出过没有。
// 返回新出的汇总（这次没出是 null）。
// 状态（summary 表 DAYS_STATE 那一行的 body）：{v: DAYS_VERSION, mark: 读到哪一局的收到时间, more: 上次没读完,
//   todo: 还要重建的日子, checked: 上次核对的是哪天, verified: 哪天核对过, fixed: 核对时对不上、换掉的次数（正常一直是 0）}
async function refreshSummary(env, force) {
  const db = env.DB;
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  const head = Object.fromEntries((await db.prepare(
    `SELECT name, made_at, CASE WHEN name = ?1 THEN NULL ELSE body END AS body FROM summary WHERE name IN (?1, ?2, ?3)`,
  ).bind(SUMMARY_NAME, DAYS_STATE, DAYS_DIRTY).all()).results.map((r) => [r.name, r]));
  let state = head[DAYS_STATE] ? JSON.parse(head[DAYS_STATE].body) : null;
  if (state?.v !== DAYS_VERSION) state = await startRebuild(db, now);
  const dirty = head[DAYS_DIRTY];
  if (dirty) {
    // /v1/delete 删过数据：那几天加进要重建的日子（只删读到的这一个标记：这一会儿又有人删了的话，标记还在，下次再加）
    state.todo = [...new Set([...state.todo, ...JSON.parse(dirty.body)])];
    await db.batch([saveState(db, state, now),
                    db.prepare("DELETE FROM summary WHERE name = ? AND made_at = ?").bind(DAYS_DIRTY, dirty.made_at)]);
  }
  if (state.todo.length) {
    await rebuildDays(db, state, force ? state.todo.length : REBUILD_DAYS, now);
    if (!force) return null;  // 这一次只重建；建完以后下一个小时接着增量读、出汇总
  }
  do {
    await catchUp(db, state, now);
  } while (force && state.more);
  if (state.more) return null;  // 新收到的局没读完：下一个小时接着读，读完再出汇总
  if (!force && head[SUMMARY_NAME]?.made_at.slice(0, 10) === today) {
    if (now.getUTCHours() >= VERIFY_HOUR && state.verified !== today) await verifyDay(db, state, now);
    return null;
  }
  // 每天第一次：各天的部分和加起来，出汇总
  const rows = (await db.prepare("SELECT day, body FROM summary_days").all()).results;
  const made = JSON.stringify(assemble(new Map(rows.map((r) => [r.day, JSON.parse(r.body)])), now));
  await saveRow(db, SUMMARY_NAME, made, now.toISOString(), state.mark || null).run();
  return made;
}

async function summary(env) {
  const row = await env.DB.prepare("SELECT body FROM summary WHERE name = ?").bind(SUMMARY_NAME).first();
  const body = row?.body ?? (await refreshSummary(env, true));
  return new Response(body, {
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=300" },
  });
}

// ---------- 地图轮换（GET /v1/maps）----------

const MAPS_UPSTREAM = "https://api.apexlegendsstatus.com/maprotation?version=2";  // version=2 才有排位和轮换模式
const MAP_MODES = { pubs: "battle_royale", ranked: "ranked", mixtape: "ltm" };  // 我们的名字 → 上游的名字
const MAPS_NAME = "maps_v1";  // 存在 summary 表的哪一行（body = {v, modes}，made_at = 取的时间，data_at = 最早换图的时间（秒））
const MAPS_RECHECK = 15 * 60;  // 存着的超过这么久（秒），没到换图的时间也重新取一次

// 上游的一场 → {map: 英文名, code, event: 轮换模式的玩法（如 "TDM"，别的模式是 null）, start, end}（时间是秒）；不像样返回 null
function rotationSlot(o) {
  if (!o || typeof o !== "object") return null;
  const map = typeof o.map === "string" ? o.map.trim().slice(0, 60) : "";
  const end = Number(o.end);
  if (!map || !Number.isFinite(end) || end <= 0) return null;
  const start = Number(o.start);
  return {
    map,
    code: typeof o.code === "string" ? o.code.slice(0, 60) : null,
    event: typeof o.eventName === "string" && o.eventName.trim() ? o.eventName.trim().slice(0, 60) : null,
    start: Number.isFinite(start) && start > 0 ? start : null,
    end,
  };
}

// 去上游取一次，整理成 {pubs | ranked | mixtape: {current, next}}；取不到、格式不对返回 null
async function fetchRotation(env) {
  const upstream = await fetch(`${MAPS_UPSTREAM}&auth=${encodeURIComponent(env.ALS_API_KEY)}`);
  if (!upstream.ok) return null;
  let raw;
  try {
    raw = JSON.parse(await upstream.text());  // Key 不对时上游回 200 + 一句纯文字，不是 JSON
  } catch (e) {
    return null;
  }
  const modes = {};
  for (const [name, field] of Object.entries(MAP_MODES)) {
    const current = rotationSlot(raw?.[field]?.current);
    if (current) modes[name] = { current, next: rotationSlot(raw[field].next) };
  }
  return Object.keys(modes).length ? modes : null;
}

// 定时任务每分钟一次：还没存过、到了换图的时间（上游晚几秒才换的话，下一分钟再取）、或者存着的超过 MAPS_RECHECK，
// 才去上游取；取到了存起来，取不到就留着上一次的
async function refreshMaps(env, now = Math.floor(Date.now() / 1000)) {
  if (!env.ALS_API_KEY) return false;
  const row = await env.DB.prepare("SELECT made_at, data_at FROM summary WHERE name = ?").bind(MAPS_NAME).first();
  if (row && now < Number(row.data_at) && now - Math.floor(Date.parse(row.made_at) / 1000) < MAPS_RECHECK) return false;
  const modes = await fetchRotation(env);
  if (!modes) return false;
  const soonest = Math.min(...Object.values(modes).map((m) => m.current.end));
  await env.DB.prepare(
    `INSERT INTO summary (name, body, made_at, data_at) VALUES (?, ?, ?, ?)
     ON CONFLICT (name) DO UPDATE SET body = excluded.body, made_at = excluded.made_at, data_at = excluded.data_at`,
  ).bind(MAPS_NAME, JSON.stringify({ v: 1, modes }), new Date(now * 1000).toISOString(), String(soonest)).run();
  return true;
}

// 回给 Evo 的：{v: 1, now: 服务器现在的时间（秒，Evo 用来校正电脑时钟）, modes: {pubs | ranked | mixtape: {current, next}}}。
// 只读存着的（不去上游）；还没存过（刚部署、定时任务还没跑）回 503
async function maps(env) {
  const row = await env.DB.prepare("SELECT body FROM summary WHERE name = ?").bind(MAPS_NAME).first();
  if (!row) return reply({ ok: false, error: "not ready" }, 503);
  return new Response(JSON.stringify({ ...JSON.parse(row.body), now: Math.floor(Date.now() / 1000) }), {
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && pathname === "/") {
      return new Response("Evo 匿名统计的接收地址（只收 Evo 用户同意后发送的数据）。说明见 https://evotracker.cn/privacy.html\n",
                          { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }
    if (request.method === "GET" && pathname === "/v1/summary") {
      try {
        return await summary(env);
      } catch (e) {
        return reply({ ok: false, error: "server error" }, 500);
      }
    }
    if (request.method === "GET" && pathname === "/v1/maps") {
      try {
        return await maps(env);
      } catch (e) {
        return reply({ ok: false, error: "server error" }, 500);
      }
    }
    const handler = { "/v1/report": report, "/v1/delete": remove }[pathname];
    if (handler === undefined) return reply({ ok: false, error: "not found" }, 404);
    if (request.method !== "POST") return reply({ ok: false, error: "method not allowed" }, 405);
    try {
      return await handler(await readJson(request), env);
    } catch (e) {
      if (e instanceof Bad || e instanceof SyntaxError) return reply({ ok: false, error: "bad request" }, 400);
      return reply({ ok: false, error: "server error" }, 500);
    }
  },

  // 定时任务（wrangler.toml 的 crons）：每小时第 7 分钟增量更新全体玩家的汇总（见 refreshSummary）；每分钟看一眼地图轮换要不要重新取
  async scheduled(event, env, ctx) {
    ctx.waitUntil(event.cron === SUMMARY_CRON ? refreshSummary(env, false) : refreshMaps(env));
  },
};
