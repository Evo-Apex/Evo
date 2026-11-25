// 从 Liquipedia 一键加进选手库（2026-11-18 用户要的：抓完以后不用一个一个手动加）。
// 战队页"把现役选手加进选手库"、收录的比赛"把这场的参赛队加进选手库"都走这里：
// - 选手名用 Liquipedia 上的 ID（手动修正过的用修正的），战队名用 Liquipedia 上的队名（想用中文名，在选手库"改队名"），
//   Liquipedia 页面名填好（资料页和选手库就连上了）
// - 直播间：Liquipedia 资料 links 里有斗鱼、虎牙、B站直播间、抖音直播间、Twitch 地址的，自动填上（别人已经在用的房间不填；
//   几个平台都有时国内的优先，Twitch 排最后，见 liquipedia.js 的 STREAM_LINKS）；
//   没有的平台空着，标"直播间待填"——不挡发布，发布时按"不直播"写进去、提醒一句（esports.js 的 build）
// - 选手库里已经有的（同一个 Liquipedia 页面名，或者同一个名字）跳过，不加重复
//
// 从 Liquipedia 补直播间（2026-11-25，Evo 能播 Twitch 以后；选手库 245 人里 218 人是"不直播"或"待填"，多是在 Twitch 播的
// 国外选手）：已经在选手库里的人，上面那个"一键加"会跳过，所以另做一页"从 Liquipedia 补直播间"（照一键配队徽：先列出来、
// 人勾一下、再一次补好）。只补平台是"待填"（空的）或"不直播"的人；已经有平台的不动。不放进定时任务里自动改：
// 选手库是人管的，lp/ 是抓来的，谁也不改谁的。

import { Refuse, randomHex, readDoc, updateDoc } from "./store.js";
import { PLAYERS, writeLog } from "./data.js";
import { NO_STREAM, length, normalRoom, platformName, roomOk, squash } from "./esports.js";
import { pageFromInput } from "./liquipedia.js";
import { eventKey, index } from "./lpsync.js";
import { merged, overrides } from "./overrides.js";

const emptyList = () => ({ version: 1, items: [] });

// 收录的一场比赛：名单里每支队的每个人 → [{page, name, team}]
export async function eventEntries(bucket, page) {
  const event = (await readDoc(bucket, eventKey(page), () => ({}))).data;
  if (!event.teams?.length) throw new Refuse("这场比赛的名单还没抓到（等抓取进度走到这一场）");
  return event.teams.flatMap((t) => t.players.map((p) => ({ page: p.page, name: p.id, team: t.team })));
}

// 一支战队的现役选手（我们抓到资料的人里，现在在这支队的）→ [{page, name, team}]
export function teamEntries(idx, ov, page) {
  const team = idx.teams[page];
  if (!team || team.missing) throw new Refuse("没有这支战队的资料");
  return Object.values(idx.players).filter((p) => !p.missing).map((p) => merged(p, ov[p.page]))
    .filter((p) => (p.teamPage === page && !p.fixed?.team) || (p.fixed?.team && p.team === team.name))
    .map((p) => ({ page: p.page, name: p.id, team: team.name }));
}

export async function addFromLiquipedia(bucket, by, entries, source, now) {
  if (!entries.length) throw new Refuse("没有可以加的人");
  const [idx, ov] = await Promise.all([index(bucket), overrides(bucket)]);
  const result = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const out = { added: 0, pending: 0, skipped: 0 };
    const taken = new Set(list.items.filter((p) => p.platform && p.platform !== "none").map((p) => `${p.platform}-${p.room}`));
    for (const e of entries) {
      const raw = idx.players[e.page];
      const p = raw && !raw.missing ? merged(raw, ov[e.page]) : null;
      const name = squash(p?.id ?? e.name);
      const team = squash(e.team);
      if (!name || !team || length(name) > 40 || length(team) > 40
          || list.items.some((x) => x.liquipedia === e.page || x.name === name)) {
        out.skipped++;
        continue;
      }
      const stream = (raw?.streams ?? []).find((s) => roomOk(s.platform, s.room) && !taken.has(`${s.platform}-${normalRoom(s.platform, s.room)}`));
      const record = { id: randomHex(4), name, team, platform: stream?.platform ?? "", room: stream ? normalRoom(stream.platform, stream.room) : "",
                       liquipedia: e.page, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
      if (stream) taken.add(`${record.platform}-${record.room}`);
      else out.pending++;
      list.items.push(record);
      out.added++;
    }
    if (list.items.length > 1500) throw new Refuse("选手最多 1500 个");
    return out;
  });
  await writeLog(bucket, by, now, `从 Liquipedia 加进选手库（${source}）：新加 ${result.added} 位（${result.pending} 位直播间待填），`
    + `跳过 ${result.skipped} 位（选手库里已经有了）`);
  return result;
}

// ---------- 从 Liquipedia 补直播间（2026-11-25） ----------

const open = (p) => !p.platform || p.platform === NO_STREAM;  // 直播间待填（空的）或者不直播：可以补
const roomKey = (platform, room) => `${platform}-${normalRoom(platform, room)}`;
// 2026-11-18 以前存的 Liquipedia 页面名有的是整个网址（同 data.js 的 players）
const pageOf = (p) => (/^https?:\/\//i.test(p.liquipedia ?? "") ? pageFromInput(p.liquipedia) ?? p.liquipedia : p.liquipedia ?? "");

// 这位选手在 Liquipedia 资料里能用的直播间（房间号整理好），照 STREAM_LINKS 的顺序（国内的在前，Twitch 最后）
function liquipediaStreams(idx, page) {
  const raw = page ? idx.players?.[page] : null;
  if (!raw || raw.missing) return null;  // 没抓到这一页的资料
  return (raw.streams ?? []).filter((s) => roomOk(s.platform, s.room)).map((s) => ({ platform: s.platform, room: normalRoom(s.platform, s.room) }));
}

// 能补的人（不改数据）。items：选手库（data.js 的 players）；idx：lp/index.json。返回
//   matches 能补的 [{id, name, team, was（"" 待填 / "none" 不直播）, platform, room}]：挑第一个没被别人占着的直播间
//   taken   Liquipedia 上有直播间、但都被选手库里别人占着的 [{id, name, team, platform, room, owner}]
//           （同一批里两位写的是同一个直播间：先列到的那位算能补，后一位进这里）
//   noLink  抓到了资料、但 links 里没有能用的直播间的人数；noData：Liquipedia 资料还没抓到的人数；noPage：没填 Liquipedia 页面名的人数
export function fillCandidates(items, idx) {
  const out = { matches: [], taken: [], noLink: 0, noData: 0, noPage: 0 };
  const owners = new Map(items.filter((p) => !open(p)).map((p) => [roomKey(p.platform, p.room), p.name]));
  for (const p of items) {
    if (!open(p)) continue;
    const page = pageOf(p);
    if (!page) {
      out.noPage++;
      continue;
    }
    const streams = liquipediaStreams(idx, page);
    if (!streams) {
      out.noData++;
      continue;
    }
    if (!streams.length) {
      out.noLink++;
      continue;
    }
    const free = streams.find((s) => !owners.has(roomKey(s.platform, s.room)));
    const base = { id: p.id, name: p.name, team: p.team };
    if (free) {
      owners.set(roomKey(free.platform, free.room), p.name);
      out.matches.push({ ...base, was: p.platform === NO_STREAM ? NO_STREAM : "", ...free });
    } else {
      out.taken.push({ ...base, ...streams[0], owner: owners.get(roomKey(streams[0].platform, streams[0].room)) });
    }
  }
  return out;
}

// 补上勾选的：picks 是 [{id（选手库里的编号）, platform, room}]。先全部核对（选手还在、Liquipedia 资料里确实写着这个直播间），
// 有一个不对整批不补；然后一个个补：打开页面以后这位已经被人改成别的平台了、这个房间被别人占了，跳过（不盖掉）。
// 补的那位改平台、房间号，记上谁、什么时候改的，rev 加 1（同 data.js 手动改一位）
export async function fillStreams(bucket, by, picks, now) {
  const wanted = [...new Map(picks.map((x) => [String(x.id ?? ""), x])).values()];  // 同一位勾了两次只算一次
  if (!wanted.length) throw new Refuse("一个都没勾：勾上要补的，再点\"用勾上的这些\"");
  const idx = await index(bucket);
  const result = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const errors = [];
    for (const pick of wanted) {
      const item = list.items.find((x) => x.id === String(pick.id ?? ""));
      const platform = String(pick.platform ?? "");
      const room = roomOk(platform, pick.room) ? normalRoom(platform, pick.room) : "";
      if (!item) errors.push("有一位选手找不到了（可能刚被别人删了）");
      else if (!room || !(liquipediaStreams(idx, pageOf(item)) ?? []).some((s) => s.platform === platform && s.room === room)) {
        errors.push(`「${item.name}」：Liquipedia 资料里没有 ${platformName(platform)} ${pick.room ?? ""}（资料可能刚更新过，刷新这一页再勾）`);
      }
    }
    if (errors.length) throw new Refuse(`没有补，有 ${errors.length} 处不对：\n${errors.join("\n")}`);
    const taken = new Set(list.items.filter((p) => !open(p)).map((p) => roomKey(p.platform, p.room)));
    const done = [];
    let skipped = 0;
    for (const pick of wanted) {
      const item = list.items.find((x) => x.id === String(pick.id));
      const key = roomKey(pick.platform, pick.room);
      if (!open(item) || taken.has(key)) {
        skipped++;
        continue;
      }
      item.platform = pick.platform;
      item.room = normalRoom(pick.platform, pick.room);
      item.updated = now;
      item.updatedBy = by.name;
      item.rev = (item.rev ?? 0) + 1;
      taken.add(key);
      done.push(`${item.name}（${platformName(item.platform)} ${item.room}）`);
    }
    return { done, skipped };
  });
  if (result.done.length) {
    const shown = result.done.slice(0, 40).join("、") + (result.done.length > 40 ? ` 等 ${result.done.length} 位` : "");
    await writeLog(bucket, by, now, `从 Liquipedia 补直播间（${result.done.length} 位）：${shown}`);
  }
  return { done: result.done.length, skipped: result.skipped };
}
