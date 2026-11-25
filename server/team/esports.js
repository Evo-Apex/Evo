// 赛事数据的规则：怎么检查选手库和赛程、怎么生成 Evo 赛事页读的 esports.json（2026-11-17 第 2 步）。
// 照搬 tools/esports.py（check_players / check_events / build）和 code/esports_data.py（clean），两边要一样：
// tests/test_team_publish.py 拿同一份数据让两边各生成一次，逐项比；平台名单也和 config/watch.json 比。
// 那两个文件改了规则，这里跟着改（找「赛事页和看直播」那个窗口商量过：格式 version 1 不变）。

import { Refuse } from "./store.js";
import { pageFromInput } from "./liquipedia.js";

// 同 config/watch.json 的 platforms（顺序也一样）。pattern 同那里的 room_pattern：房间号可以怎么写，没写的只能是数字。
// 抖音的地址后面也可以是主播的抖音号（字母、数字、下划线，不分大小写；2026-11-18 用户同意、Evo 那边先改的）；
// Twitch 写登录名（字母、数字、下划线，最长 25 位，不分大小写；2026-11-25 Evo 那边加的）
export const PLATFORMS = {
  douyu: { name: "斗鱼", room: "https://www.douyu.com/{room}" },
  huya: { name: "虎牙", room: "https://www.huya.com/{room}" },
  bilibili: { name: "B站", room: "https://live.bilibili.com/{room}" },
  douyin: { name: "抖音", room: "https://live.douyin.com/{room}", pattern: "[A-Za-z0-9_]{1,24}" },
  twitch: { name: "Twitch", room: "https://www.twitch.tv/{room}", pattern: "[A-Za-z0-9_]{1,25}" },
};
export const TYPES = { 联赛: "league", 训练赛: "scrim", 杯赛: "cup" };
export const USUAL_TEAMS = 20;
export const RECENT_DAYS = 7;
export const EXAMPLE = "示例";
export const VERSION = 1;
// 一场赛事的主舞台最多几路（同 esports_data.MAIN_MAX）。2026-11-21 用户定的 8 → 24：赛管把观战的主播也放进主舞台，8 路塞不下；
// 0.7.0 以前的 Evo 只显示前 8 路
export const MAIN_MAX = 24;
const DIGITS = "\\d{1,24}";
const LOGO = /^[a-z0-9]{8,40}\.(png|webp)$/;  // 同 esports_data.LOGO_RE
const ROOM_RES = Object.fromEntries(Object.entries(PLATFORMS).map(([key, p]) => [key, new RegExp(`^(?:${p.pattern ?? DIGITS})$`)]));

// 这个平台的房间号写得对不对（同 live_status.room_ok；抖音大小写都收）
export function roomOk(platform, room) {
  return Object.hasOwn(ROOM_RES, platform) && typeof room === "string" && ROOM_RES[platform].test(room);
}

// 抖音、Twitch 不分大小写：存的、发布的都转成小写，XXFPS 和 xxfps 才查得出是同一个房间，选手的 id 也不会变
// （同 tools/esports.py 的 check_room）
export function normalRoom(platform, room) {
  return platform === "douyin" || platform === "twitch" ? String(room).toLowerCase() : String(room);
}
const ID = /^[A-Za-z0-9_-]{1,40}$/;
const MAX_PLAYERS = 2000;   // Evo 读的时候只看前这么多（esports_data.clean）
const MAX_EVENTS = 200;
const MAX_EVENT_TEAMS = 60;
const DAY = 24 * 3600 * 1000;
const HOUR = 3600 * 1000;

export function roomUrl(platform, room) {
  return PLATFORMS[platform]?.room.replace("{room}", room) ?? "";
}

// ---------- 不直播的选手（2026-11-18 用户定的做法一；Evo 那边 7fd0220）----------
// 有的队员不开直播：平台选"不直播"（存成 none）、房间号空着；照样列在队里，Evo 看直播页照样列出来、标"不直播"。
// esports.json 里 {"id": "none-" + sha256(选手名)[:12], "platform": "none", "room": ""}（同 tools/esports.py 的 player_id）。
// 主舞台不能写"不直播"（那里必须是真的直播间）。
export const NO_STREAM = "none";
export const NO_STREAM_NAME = "不直播";

export function platformName(key) {
  if (!key) return "直播间待填";
  return key === NO_STREAM ? NO_STREAM_NAME : PLATFORMS[key]?.name ?? key;
}

// sha256 → 16 进制（同步算：生成 esports.json 是同步的；Workers 的 crypto.subtle 只有异步的）
const K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
  0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70,
  0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
const rotr = (x, n) => (x >>> n) | (x << (32 - n));

export function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const total = Math.ceil((bytes.length + 9) / 64) * 64;
  const block = new Uint8Array(total);
  block.set(bytes);
  block[bytes.length] = 0x80;
  const view = new DataView(block.buffer);
  view.setUint32(total - 8, Math.floor((bytes.length * 8) / 2 ** 32));
  view.setUint32(total - 4, (bytes.length * 8) >>> 0);
  const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const w = new Uint32Array(64);
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, k] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      [k, g, f, e, d, c, b, a] = [g, f, e, (d + t1) >>> 0, c, b, a, (t1 + t2) >>> 0];
    }
    [a, b, c, d, e, f, g, k].forEach((x, i) => { h[i] = (h[i] + x) >>> 0; });
  }
  return h.map((x) => x.toString(16).padStart(8, "0")).join("");
}

// 选手在 esports.json 里的 id：开直播的是"平台-房间号"，不直播的是 none-<选手名 sha256 的前 12 位>
export function playerId(p) {
  return p.platform === NO_STREAM ? `${NO_STREAM}-${sha256Hex(p.name).slice(0, 12)}` : `${p.platform}-${p.room}`;
}

// 一位选手写进 esports.json 的那一项。**Twitch 的选手写成旧版本也认得的兼容写法**（2026-11-25）：0.7.2 及以前的 Evo
// 不认识 twitch，会把这样的选手从队伍里整个丢掉；写成 platform "none"、真正的平台和房间号放在 stream 里，
// 旧版本照旧当"不直播"，新版本（esports_data.clean_player）从 stream 里认出来。同 tools/esports.py 的 player_entry。
// 主舞台、主播的 Twitch 直接写 platform "twitch"（走的是另外两处）。
export function playerEntry(p) {
  const entry = { id: playerId(p), name: p.name, team: p.team, platform: p.platform, room: p.room, liquipedia: p.liquipedia ?? "" };
  if (p.platform === "twitch") Object.assign(entry, { platform: NO_STREAM, room: "", stream: { platform: "twitch", room: p.room } });
  return entry;
}


// ---------- 文字、时间 ----------

// 去掉两头空格、中间连续的空白并成一个（同 esports_data._text 和 tools/esports.py 读表时的做法）
export function squash(value) {
  return String(value ?? "").split(/\s+/).filter(Boolean).join(" ");
}

export function length(text) {
  return [...text].length;  // 按字算（同 Python 的 len），不按 UTF-16
}

const TIME = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[\sT]+(\d{1,2})[:：](\d{2})(?::\d{2})?$/;

// "2026-11-17 19:00"（北京时间；Excel 改成 2026/11/17 19:00、网页上的 2026-11-17T19:00 也认）→ 毫秒；写错了 null
export function parseBeijing(text) {
  const found = TIME.exec(squash(text));
  if (!found) return null;
  const [y, mo, d, h, mi] = found.slice(1).map(Number);
  const ms = Date.UTC(y, mo - 1, d, h - 8, mi);
  const back = new Date(ms + 8 * HOUR);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d || h > 23 || mi > 59) return null;
  return ms;
}

// 毫秒 → "2026-11-17T19:00:00+08:00"（同 Python 带时区的 isoformat）
export function isoBeijing(ms) {
  return new Date(ms + 8 * HOUR).toISOString().slice(0, 19) + "+08:00";
}

// "2026-11-17T19:00:00+08:00" → 毫秒（带时区的才认，同 esports_data.parse_time）
export function parseIso(text) {
  if (typeof text !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

export function beijingText(ms) {
  return new Date(ms + 8 * HOUR).toISOString().slice(0, 16).replace("T", " ");
}

// ---------- 一条选手、一场赛事（保存时就查，错了不让存） ----------

function required(value, label, max) {
  const text = squash(value);
  if (!text) throw new Refuse(`「${label}」不能空着`);
  if (length(text) > max) throw new Refuse(`「${label}」最多 ${max} 个字`);
  return text;
}

// 平台写中文名或英文都认
export function platformKey(value) {
  const text = squash(value);
  if (Object.hasOwn(PLATFORMS, text)) return text;
  const found = Object.entries(PLATFORMS).find(([, p]) => p.name === text || p.name.toLowerCase() === text.toLowerCase());
  return found ? found[0] : null;
}

export function cleanPlayerFields(form) {
  const name = required(form.name, "选手", 40);
  const team = required(form.team, "战队", 40);
  const typedPlatform = squash(form.platform);
  if (!typedPlatform) throw new Refuse("选一个平台（不开直播的选\"不直播\"）");
  const platform = [NO_STREAM, NO_STREAM_NAME].includes(typedPlatform) ? NO_STREAM : platformKey(typedPlatform);
  if (!platform) throw new Refuse(`平台「${typedPlatform}」不认识，只能是 ${[...Object.values(PLATFORMS).map((p) => p.name), NO_STREAM_NAME].join(" / ")}`);
  const raw = squash(form.room);
  if (platform === NO_STREAM && raw) throw new Refuse(`不直播的选手「房间号」要空着（现在是「${raw}」）`);
  if (platform !== NO_STREAM && !roomOk(platform, raw)) {
    throw new Refuse(platform === "douyin" ? `房间号「${raw}」只能是字母、数字、下划线（直播间地址 live.douyin.com/ 后面那一段）`
      : platform === "twitch" ? `房间号「${raw}」只能是字母、数字、下划线（直播间地址 twitch.tv/ 后面那一段）`
      : `房间号「${raw}」只能是数字（直播间地址最后那串数字；虎牙要数字房间号）`);
  }
  const room = normalRoom(platform, raw);
  // 贴 Liquipedia 选手页的网址也认，存成页面名（https://liquipedia.net/apexlegends/3Mz → 3Mz）
  const typed = squash(form.liquipedia);
  const liquipedia = typed ? pageFromInput(typed) : "";
  if (liquipedia === null) throw new Refuse(`看不懂 Liquipedia 页面名「${typed}」：贴选手页的网址（https://liquipedia.net/apexlegends/…），或者页面名`);
  if (length(liquipedia) > 100) throw new Refuse("Liquipedia 页面名最多 100 个字");
  return { name, team, platform, room, liquipedia };
}

// ---------- 主舞台（2026-11-18 用户要的：有的主播播导播视角，Evo 看直播页队伍栏最上面一栏）----------
// 每场赛事最多 8 路，每路 {platform, room, name}：房间号的规矩同选手（抖音可以是抖音号、转小写），名字 1–40 个字，
// 同一场里同一个房间不写两次。规矩照 tools/esports.py 的 check_main / check_room（「赛事页和看直播」窗口 85508ef）。
// 主舞台的主播不进选手库、不算哪支队。

function checkStream(platformText, roomText, nameText) {
  const platform = platformKey(platformText);
  if (!platform) return { error: `主舞台的平台「${squash(platformText)}」不认识，只能写 ${Object.values(PLATFORMS).map((p) => p.name).join(" / ")}` };
  const room = squash(roomText);
  if (!roomOk(platform, room)) {
    return { error: platform === "douyin" ? `主舞台的房间号「${room}」只能是字母、数字、下划线（直播间地址 live.douyin.com/ 后面那一段）`
      : platform === "twitch" ? `主舞台的房间号「${room}」只能是字母、数字、下划线（直播间地址 twitch.tv/ 后面那一段）`
      : `主舞台的房间号「${room}」只能是数字（直播间地址最后那串数字）` };
  }
  const name = squash(nameText);
  if (!name) return { error: "主舞台的名字不能空着（比如：官方直播）" };
  if (length(name) > 40) return { error: "主舞台的名字最多 40 个字" };
  return { stream: { platform, room: normalRoom(platform, room), name } };
}

// 一组 {platform, room, name}（表单里的几行，或者粘贴的那一格拆出来的）→ 主舞台；有错抛 Refuse（只报第一个）
export function cleanMain(rows) {
  const filled = rows.filter((r) => squash(r.platform) || squash(r.room) || squash(r.name));
  if (filled.length > MAIN_MAX) throw new Refuse(`主舞台最多 ${MAIN_MAX} 路（现在 ${filled.length} 路）`);
  const out = [];
  for (const row of filled) {
    const { stream, error } = checkStream(row.platform, row.room, row.name);
    if (error) throw new Refuse(error);
    if (out.some((s) => s.platform === stream.platform && s.room === stream.room)) {
      throw new Refuse(`主舞台里 ${PLATFORMS[stream.platform].name} 房间 ${stream.room} 写了两次`);
    }
    out.push(stream);
  }
  return out;
}

// 赛程表格第 7 列「主舞台」：平台 房间号 名字，多路用顿号「、」隔开（同 tools/esports.py 的 MAIN_RE）
export function parseMainText(text) {
  const rows = [];
  for (const entry of String(text ?? "").split("、").map(squash).filter(Boolean)) {
    const found = /^(\S+)\s+(\S+)\s+(.+)$/.exec(entry);
    if (!found) throw new Refuse(`主舞台「${entry}」要写成：平台 房间号 名字（如 B站 7734200 官方直播），多路用顿号「、」隔开`);
    rows.push({ platform: found[1], room: found[2], name: found[3] });
  }
  return cleanMain(rows);
}

export function cleanEventFields(form, knownTeams) {
  const name = required(form.name, "名称", 80);
  const stage = squash(form.stage);
  if (length(stage) > 80) throw new Refuse("「阶段」最多 80 个字");
  const type = squash(form.type);
  if (!Object.hasOwn(TYPES, type)) throw new Refuse(`类型「${type}」不认识，只能是 ${Object.keys(TYPES).join(" / ")}`);
  const start = parseBeijing(form.start);
  const end = parseBeijing(form.end);
  if (start === null) throw new Refuse(`开始「${squash(form.start)}」要写成 2026-11-17 19:00（北京时间）`);
  if (end === null) throw new Refuse(`结束「${squash(form.end)}」要写成 2026-11-17 19:00（北京时间）`);
  if (end <= start) throw new Refuse("结束要晚于开始");
  const teams = [];
  for (const raw of form.teams ?? []) {
    const team = squash(raw);
    if (!team) continue;
    if (teams.includes(team)) throw new Refuse(`队伍「${team}」写了两次`);
    if (knownTeams && !knownTeams.includes(team)) throw new Refuse(`队伍「${team}」不在选手库里${closest(team, knownTeams)}`);
    teams.push(team);
  }
  if (!teams.length) throw new Refuse("至少要选一支队伍");
  if (teams.length > MAX_EVENT_TEAMS) throw new Refuse(`一场最多 ${MAX_EVENT_TEAMS} 支队`);
  const main = Array.isArray(form.main) ? cleanMain(form.main) : [];
  return { name, type, stage, start: isoBeijing(start), end: isoBeijing(end), teams, main };
}

// "（是不是「星火电竞」？）"：只找很像的（同 tools/esports.py 用 difflib 的意思，这里用编辑距离）
export function closest(text, options) {
  let best = null;
  let bestScore = 0;
  for (const option of options) {
    const score = similarity(text, option);
    if (score > bestScore) [best, bestScore] = [option, score];
  }
  return best && bestScore >= 0.6 ? `（是不是「${best}」？）` : "";
}

function similarity(a, b) {
  const x = [...a];
  const y = [...b];
  const row = Array.from({ length: y.length + 1 }, (_, i) => i);
  for (let i = 1; i <= x.length; i++) {
    let previous = row[0];
    row[0] = i;
    for (let j = 1; j <= y.length; j++) {
      const saved = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (x[i - 1] === y[j - 1] ? 0 : 1));
      previous = saved;
    }
  }
  return 1 - row[y.length] / Math.max(x.length, y.length, 1);
}

// ---------- 整张表的检查（发布前；选手库页上也显示） ----------

// 选手库：同 check_players（一个选手只能在一支队、同一个平台房间不能重复）。
// 一支队几个人不限（2026-11-18 用户定的："允许战队不足 3 人或者超过 3 人，不加 3 人的限制"）
export function checkPlayers(players) {
  const errors = [];
  const names = new Map();
  const rooms = new Map();
  const teams = new Map();
  for (const p of players) {
    if (names.has(p.name)) errors.push(`选手「${p.name}」写了两次（一个选手只能在一支队）`);
    names.set(p.name, p);
    const key = `${p.platform}-${p.room}`;
    if (p.platform && p.platform !== NO_STREAM) {  // 不直播的没有房间、待填的还没有房间，都不查重复
      if (rooms.has(key)) errors.push(`${platformName(p.platform)} 房间 ${p.room} 写了两次（「${rooms.get(key).name}」和「${p.name}」）`);
      rooms.set(key, p);
    }
    if (!teams.has(p.team)) teams.set(p.team, []);
    teams.get(p.team).push(p);
  }
  for (const [team, members] of teams) {
  }
  return { errors, teams: [...teams.keys()] };
}

// 一场赛事在提示里的叫法：「名称 · 阶段」（A 组、B 组同名时分得清）
export function eventLabel(e) {
  return `「${e.name}${e.stage ? ` · ${e.stage}` : ""}」`;
}

// 已经结束了（结束时间到了）：有问题只提醒、不挡发布（2026-11-19 用户定的）
export function ended(e, now) {
  return now !== undefined && now !== null && parseIso(e.end) !== null && parseIso(e.end) <= now;
}

// 赛程：同 check_events（队伍都在选手库里、不重复）；一般 20 支队，不是的只提醒。
// 已经结束的赛事有问题不挡发布（2026-11-19 用户定的：比如选手挪到别的队以后，旧赛程里还写着原来的队）：
// 只提醒，发布时不放选手库里没有的那支队（build）。不给 now 的时候照旧都算错（同 tools/esports.py）
export function checkEvents(events, knownTeams, now = null) {
  const errors = [];
  const notes = [];
  for (const e of events) {
    const done = ended(e, now);
    const problems = [];
    for (const team of new Set(e.teams)) {
      if (e.teams.filter((t) => t === team).length > 1) problems.push(`队伍「${team}」写了两次`);
      if (!knownTeams.includes(team)) problems.push(`队伍「${team}」不在选手库里${closest(team, knownTeams)}`);
    }
    if (done) {
      for (const p of problems) notes.push(`${eventLabel(e)}（已经结束）：${p}，发布时不放这支队`);
    } else {
      errors.push(...problems.map((p) => `${eventLabel(e)}：${p}`));
      if (e.teams.length !== USUAL_TEAMS) notes.push(`${eventLabel(e)}有 ${e.teams.length} 支队（一般是 ${USUAL_TEAMS} 支），没写错就不用管`);
    }
  }
  return { errors, notes };
}

// ---------- 生成 esports.json（同 tools/esports.py 的 build） ----------

// players、events 是网页存的样子（按添加的顺序）；switches 是线上现在的平台开关；now 毫秒；
// teams 是 {队名: {"logo": 文件名, ...}}（logos.js 的 manual/teams.json），只放这次用到、文件名对的队，一个都没有就不写这一项。
// players 放选手库里的**全部**选手（2026-11-19 用户定的："默认职业选手就是主播"，Evo 电竞页的"主播"一块列所有开直播的选手；
// 同 tools/esports.py 的 build(everyone=True)）；原来只放这几场赛事用到的队的选手
export function build(players, events, switches, now, teams = {}) {
  // 直播间待填的（平台空着，比如从 Liquipedia 一键加进来的外国选手）不挡发布，这次按"不直播"发，提醒一句
  // （2026-11-18 用户在线上先这样改的，11-19 拿回 git）
  const pending = players.filter((p) => !p.platform).map((p) => p.name);
  players = players.map((p) => (p.platform ? { ...p, room: normalRoom(p.platform, p.room) } : { ...p, platform: NO_STREAM, room: "" }));
  const errors = [];
  const notes = [];
  if (pending.length) {
    notes.push(`${pending.length} 位选手直播间还没填，这次按"不直播"发布（补上以后再发一次就行）：`
      + pending.slice(0, 10).map((n) => `「${n}」`).join("、") + (pending.length > 10 ? " 等" : ""));
  }
  const roster = checkPlayers(players);
  errors.push(...roster.errors);
  const schedule = checkEvents(events, roster.teams, now);
  errors.push(...schedule.errors);
  notes.push(...schedule.notes);
  // 已经结束的赛事：选手库里没有的队不放（上面提醒过），写了两次的只放一次；一支队都不剩的整场不放
  const teamsOut = (e) => [...new Set(e.teams)].filter((t) => !ended(e, now) || roster.teams.includes(t));
  events = events.filter((e) => teamsOut(e).length);

  const indexed = events.map((e, line) => ({ e, line, start: parseIso(e.start), end: parseIso(e.end) }));
  const old = indexed.filter((x) => now - x.end > RECENT_DAYS * DAY);
  if (old.length) notes.push(`${old.length} 场赛事结束超过 ${RECENT_DAYS} 天，不放进去（可以在赛程页删掉）：${old.map((x) => eventLabel(x.e)).join("、")}`);
  const kept = [];
  const ids = new Set();
  for (const x of indexed.filter((y) => !old.includes(y)).sort((a, b) => a.start - b.start || a.line - b.line)) {
    const base = `${isoBeijing(x.start).slice(0, 16).replace(/[-:]/g, "").replace("T", "-")}-${TYPES[x.e.type]}`;
    let id = base;
    for (let n = 2; ids.has(id); n++) id = `${base}-${n}`;  // 同一时刻开始的同类赛事：后面的加 -2、-3
    ids.add(id);
    kept.push({ id, name: x.e.name, type: x.e.type, stage: x.e.stage, start: isoBeijing(x.start), end: isoBeijing(x.end),
                teams: teamsOut(x.e) });
    if (x.e.main?.length) {  // 主舞台：有才写这一项（同 tools/esports.py）
      kept[kept.length - 1].main = x.e.main.map((m) => ({ id: `${m.platform}-${normalRoom(m.platform, m.room)}`, name: m.name,
        platform: m.platform, room: normalRoom(m.platform, m.room) }));
      // 和这场的选手是同一个直播间：Evo 看直播页会去掉这一路（按 id 认人），这里提醒一下
      for (const m of kept[kept.length - 1].main) {
        const same = players.find((p) => x.e.teams.includes(p.team) && `${p.platform}-${p.room}` === m.id);
        if (same) notes.push(`${eventLabel(x.e)}的主舞台「${m.name}」和选手「${same.name}」是同一个直播间：Evo 里不会多放这一路`);
      }
    }
  }
  const used = new Set(kept.flatMap((e) => e.teams));
  const data = {
    version: VERSION,
    updated: isoBeijing(Math.floor(now / 1000) * 1000),
    platforms: Object.fromEntries(Object.keys(PLATFORMS).map((key) => [key, switches?.[key] !== false])),
    players: players.map((p) => playerEntry(p)),
    events: kept,
  };
  const logos = {};
  for (const team of [...used].sort()) {
    const logo = teams?.[team]?.logo;
    if (typeof logo === "string" && LOGO.test(logo)) logos[team] = { logo };
  }
  if (Object.keys(logos).length) data.teams = logos;
  const examples = [...data.players.map((p) => p.name), ...data.events.map((e) => e.name),
                    ...data.events.flatMap((e) => (e.main ?? []).map((m) => m.name))].filter((t) => t.includes(EXAMPLE));
  if (examples.length) errors.push(`还有示例数据（${examples.slice(0, 5).join("、")}），删掉再发布`);
  // 没有赛事也能发：选手（电竞页的"主播"一块）照样有用；连选手都没有才不发
  if (!kept.length && !data.players.length) errors.push("没有可以发布的内容（选手库和赛程都是空的）");
  else if (!kept.length) notes.push("没有进行中、即将开始或者 7 天内结束的赛事：这次只发选手（Evo 电竞页的主播一块）");
  if (!errors.length) {
    const checked = clean(data);  // Evo 读的时候会不会去掉哪一条（主舞台的路数也要一样）
    const streams = (found) => found.events.reduce((n, e) => n + (e.main?.length ?? 0), 0);
    if (checked.players.length !== data.players.length || checked.events.length !== data.events.length || streams(checked) !== streams(data)) {
      errors.push("生成的文件里有 Evo 会去掉的条目（名字里有特别的字符？），告诉 Claude 查一下");
    }
  }
  return { data, errors, notes };
}

// ---------- Evo 读的时候怎么筛（同 esports_data.clean） ----------

function text(value, limit, mandatory = true) {
  if (typeof value !== "string") return mandatory ? null : "";
  const t = squash(value);
  if (length(t) > limit || (mandatory && !t)) return null;
  return t;
}

function cleanPlayer(raw) {
  if (!raw || typeof raw !== "object") return null;
  const name = text(raw.name, 40);
  const team = text(raw.team, 40);
  if (typeof raw.id !== "string" || !ID.test(raw.id) || name === null || team === null) return null;
  let platform = raw.platform;
  let room = raw.room;
  // 不直播的：房间号必须正好是空的（同 esports_data.clean_player）
  if (platform === NO_STREAM) {
    if (room !== "") return null;
    // 旧版本兼容写法（2026-11-25，同 esports_data.clean_player）：stream 里的平台认识、房间号对，就当这位在那边播；
    // 写得不对就忽略它、照旧"不直播"
    const stream = raw.stream;
    if (stream && typeof stream === "object" && !Array.isArray(stream) && stream.platform !== NO_STREAM
        && roomOk(stream.platform, stream.room)) {
      platform = stream.platform;
      room = stream.room;
    }
  } else if (!roomOk(platform, room)) {
    return null;
  }
  return { id: raw.id, name, team, platform, room: normalRoom(platform, room), liquipedia: text(raw.liquipedia, 100, false) || "" };
}

// 主舞台的一路（同 esports_data.clean_stream；Twitch 的登录名读进来也存小写，id 不变）
function cleanStream(raw) {
  if (!raw || typeof raw !== "object") return null;
  const name = text(raw.name, 40);
  if (typeof raw.id !== "string" || !ID.test(raw.id) || name === null) return null;
  if (typeof raw.platform !== "string" || !roomOk(raw.platform, raw.room)) return null;
  return { id: raw.id, name, platform: raw.platform, room: raw.platform === "twitch" ? String(raw.room).toLowerCase() : raw.room };
}

function cleanEvent(raw) {
  if (!raw || typeof raw !== "object") return null;
  const name = text(raw.name, 80);
  const stage = text(raw.stage, 80, false);
  const start = parseIso(raw.start);
  const end = parseIso(raw.end);
  if (typeof raw.id !== "string" || !ID.test(raw.id) || name === null || stage === null) return null;
  if (!Object.hasOwn(TYPES, raw.type) || start === null || end === null || end <= start || !Array.isArray(raw.teams)) return null;
  const teams = [];
  for (const t of raw.teams) {
    const team = text(t, 40);
    if (team !== null && !teams.includes(team)) teams.push(team);
  }
  const event = { id: raw.id, name, type: raw.type, stage, start: raw.start, end: raw.end, teams: teams.slice(0, MAX_EVENT_TEAMS) };
  const main = Array.isArray(raw.main) ? unique(raw.main.slice(0, MAIN_MAX), cleanStream) : [];
  if (main.length) event.main = main;
  return event;
}

function unique(items, check) {
  const out = [];
  const seen = new Set();
  for (const raw of items) {
    const item = check(raw);
    if (item && !seen.has(item.id)) {
      out.push(item);
      seen.add(item.id);
    }
  }
  return out;
}

// 整个文件不对（不是这个格式）返回 null
export function clean(data) {
  if (!data || typeof data !== "object" || data.version !== VERSION) return null;
  if (!Array.isArray(data.players) || !Array.isArray(data.events)) return null;
  const switches = data.platforms && typeof data.platforms === "object" ? data.platforms : {};
  return {
    version: VERSION,
    updated: parseIso(data.updated) !== null ? data.updated : null,
    platforms: Object.fromEntries(Object.keys(PLATFORMS).map((key) => [key, switches[key] !== false])),
    players: unique(data.players.slice(0, MAX_PLAYERS), cleanPlayer),
    events: unique(data.events.slice(0, MAX_EVENTS), cleanEvent),
  };
}

// 进行中 / 即将开始 / 已结束
export function status(event, now) {
  if (now < parseIso(event.start)) return "upcoming";
  return now < parseIso(event.end) ? "live" : "ended";
}

export const STATUS_WORDS = { live: "进行中", upcoming: "即将开始", ended: "已结束" };

// ---------- 从 Excel 粘贴（或 CSV） ----------

// 一段表格文字 → 二维数组。Excel 复制出来是用 Tab 隔开的；CSV 用逗号。格子里有引号、换行时按 CSV 的规矩处理
export function parseTable(input) {
  const source = String(input ?? "").replace(/^﻿/, "").replace(/\r\n?/g, "\n");
  const first = source.split("\n", 1)[0];
  const sep = first.includes("\t") || !first.includes(",") ? "\t" : ",";
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    if (quoted) {
      if (c === '"' && source[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"' && cell === "") quoted = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  row.push(cell);
  rows.push(row);
  return rows.map((r) => r.map(squash)).filter((r) => r.some(Boolean));
}

export const PLAYER_COLUMNS = ["选手", "战队", "平台", "房间号", "Liquipedia页面名"];
export const EVENT_COLUMNS = ["名称", "类型", "阶段", "开始", "结束", "队伍"];  // 后面可以多一列「主舞台」

// 第一行是表头（和现在的 CSV 一样）就跳过
function body(rows, columns) {
  return rows.length && rows[0][0] === columns[0] && rows[0][1] === columns[1] ? rows.slice(1) : rows;
}

// 粘贴的选手 → [{行号, 字段}]；有错的行把错写进 errors（"第 3 行：……"）
export function importPlayers(input) {
  const errors = [];
  const out = [];
  for (const [i, cells] of body(parseTable(input), PLAYER_COLUMNS).entries()) {
    const [name, team, platform, room, liquipedia = ""] = cells;
    try {
      out.push({ line: i + 1, ...cleanPlayerFields({ name, team, platform, room, liquipedia }) });
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      errors.push(`第 ${i + 1} 行：${error.message}`);
    }
  }
  return { rows: out, errors };
}

// 粘贴的赛程（队伍用顿号「、」隔开） → [{行号, 字段}]
export function importEvents(input, knownTeams) {
  const errors = [];
  const out = [];
  for (const [i, cells] of body(parseTable(input), EVENT_COLUMNS).entries()) {
    const [name, type, stage, start, end, teamText = "", mainText = ""] = cells;
    const teams = teamText.split("、").map(squash).filter(Boolean);
    try {
      if (teams.length === 1 && /[，,;；/]/.test(teams[0])) throw new Refuse("队伍要用顿号「、」隔开");
      out.push({ line: i + 1, ...cleanEventFields({ name, type, stage, start, end, teams }, knownTeams), main: cells.length >= 7 ? parseMainText(mainText) : undefined });
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      errors.push(`第 ${i + 1} 行：${error.message}`);
    }
  }
  return { rows: out, errors };
}
