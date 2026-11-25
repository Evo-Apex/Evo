// Evo 后台（2026-11-17 用户决定，原来叫"赛事数据的协作网页"，11-19 改名）：Cloudflare Worker，地址 https://team.evotracker.cn。
// 11-19 起也是 Evo 的数据中心：「数据」页显示日活、对局等趋势（statsroll.js 每 3 小时从匿名统计的数据库增量算好，pages_stats.js）。
// 几个人一起管选手库、赛程和从 Liquipedia 抓来的选手资料，管好了发布成 Evo 赛事页读的 esports.json。
// 计划和用户定的做法见 claude/todo.md「赛事数据管理网页」；部署、怎么用见 docs/底座.md"协作网页"。
//
// 第 1 步（现在）：登录、邀请、身份、成员管理，首页是空的骨架。
// - 没有账号密码：管理员在「成员」页生成邀请链接（/join/口令），微信发给对方；对方打开、点"进入"，
//   浏览器记住 90 天（Cookie）。链接 7 天内有效、用一次就作废。打开链接只显示是谁的邀请，点了"进入"才用掉：
//   微信、QQ 生成链接预览时会先打开一次链接，那样不会把邀请用掉
// - 身份：管理员（什么都能做）、编辑（能改，发布要单独开）、只读。至少留一个管理员
// - 第一个管理员的链接、管理员自己进不来时：python tools/team.py（在命令行里直接改存储桶里的成员文件）
// - 数据在不公开的 R2 存储桶 evo-team（成员见 auth.js）；不用 D1：D1 的免费额度整个账号共用，
//   统计服务器 2026-11-08 超过一次，一超当天所有数据库都报错
// - 不记请求日志（observability 关着）、不存 IP；搜索引擎不收录（robots.txt + noindex）
// - 只许本站的表单提交（看 Origin），页面只许运行本站的脚本（CSP），不许被别的网站嵌进去
//
// 本机试：cd server/team && wrangler dev（本机模拟的存储桶），python tools/team.py invite 名字 --local 拿链接
// 测试：node server/team/test.mjs
// 部署：cd server/team && wrangler deploy（第一次先 wrangler r2 bucket create evo-team）

import {
  Refuse, acceptInvite, can, cancelInvite, clearCookie, createInvite, findInvite, load, logout, removeMember,
  sessionCookie, setRole, validId, whoami,
} from "./auth.js";
import { APP_JS, homePage, invitePage, layout, loginPage, membersPage, messagePage, publishBanner } from "./pages.js";
import { eventsPage, fillStreamsPage, logPage, playersPage, publishPage, roomsPage } from "./pages_esports.js";
import { confirmPage, teamAnchor, teamsPage } from "./pages_teams.js";
import { NONE, REGIONS, deletePlayers, deleteTeams, saveRegions, teamOverview, teamRegions } from "./teams.js";
import { importEvents, importPlayers } from "./esports.js";
import {
  addEvent, addPlayer, deleteEvent, deletePlayer, events, forgetRooms, importEventRows, importPlayerRows, mergeTeam, movePlayer, players, published, recentLog,
  saveRooms, savedRooms, renameTeam, saveEvent, savePlayer, teamsOf,
} from "./data.js";
import { preview, publish, setPlatform, unpublished } from "./publish.js";
import { liquipediaPage } from "./pages_lp.js";
import { addFromLiquipedia, eventEntries, fillCandidates, fillStreams, teamEntries } from "./lpimport.js";
import { logoHref, logoPage, logosPage } from "./pages_logos.js";
import { LOGO_FILE, autoMatches, library, libraryKey, pickLogo, pickMany, removeLogo, search, suggest, teamLogos, uploadLogo } from "./logos.js";
import { manualProPage, newProPage, proHref, proPage, prosPage, teamProfilePage } from "./pages_pros.js";
import { statsPage } from "./pages_stats.js";
import { daily, isoDay, rollup } from "./statsroll.js";
import { createPro, deletePro, manualPros, onLiquipedia, savePro, visible } from "./manualpros.js";
import { merged, overrides, removeOverride, resolveConflict, setOverride } from "./overrides.js";
import {
  addTracked, confirmMatch, index, pending, pendingCount, progress, rejectMatch, rejects, removeTracked, requestRefresh, resultsOf,
  squadsOf, state, tick, tracked,
} from "./lpsync.js";

const MAX_FORM = 64 * 1024;  // 从 Excel 粘贴一大张表也够

const SECURITY = {
  // 图片：本站、上传前的预览（blob:）、下载服务器上的队徽
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self' data: blob: https://dl.evotracker.cn; "
    + "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
  // same-origin：本站之间照常带（表单提交要靠 Origin 认是不是本站），点到别的网站不带，邀请链接不会漏出去
  "Referrer-Policy": "same-origin",
  "X-Content-Type-Options": "nosniff",
  "X-Robots-Tag": "noindex, nofollow",
  "Cache-Control": "no-store",
};

function respond(body, { status = 200, type = "text/html; charset=utf-8", headers = {} } = {}) {
  return new Response(body, { status, headers: { ...SECURITY, "Content-Type": type, ...headers } });
}

function redirect(location, headers = {}) {
  return new Response(null, { status: 303, headers: { ...SECURITY, Location: location, ...headers } });
}

// 表单只认本站提交的（别的网站放一个表单骗你点，带不上本站的 Origin）
function sameOrigin(request) {
  const origin = request.headers.get("Origin");
  return origin !== null && origin === new URL(request.url).origin;
}

async function readForm(request) {
  const length = Number(request.headers.get("Content-Length") ?? 0);
  if (length > MAX_FORM) throw new Refuse("提交的内容太长了");
  const form = await request.formData();
  const get = (key) => {
    const value = form.get(key);
    return typeof value === "string" ? value : "";
  };
  return { name: get("name"), role: get("role"), publish: get("publish") === "1" };
}

// 选手、赛事的表单：每一项原样拿出来（勾选的队伍是一串）
async function readFields(request) {
  const length = Number(request.headers.get("Content-Length") ?? 0);
  if (length > MAX_FORM) throw new Refuse("提交的内容太长了：一次少粘贴一些");
  const out = { teams: [] };
  if (!/^(application\/x-www-form-urlencoded|multipart\/form-data)/.test(request.headers.get("Content-Type") ?? "")) return out;
  const form = await request.formData();
  const lists = { main_platform: [], main_room: [], main_name: [], forget: [], d: [], del: [] };
  for (const [key, value] of form.entries()) {
    if (typeof value !== "string") continue;
    if (key === "teams") out.teams.push(value);
    else if (Object.hasOwn(lists, key)) lists[key].push(value);
    else out[key] = value;
  }
  if (form.has("main_platform")) {  // 赛事表单里主舞台的几行
    out.main = lists.main_platform.map((platform, i) => ({ platform, room: lists.main_room[i] ?? "", name: lists.main_name[i] ?? "" }));
  }
  out.forget = lists.forget;  // 赛程页"记下的直播间"里勾了要忘掉的（11-21）
  out.d = lists.d;  // 批量修改页勾选要删的选手（编号）
  out.del = lists.del;  // 战队页勾选要删的队（队名）
  if ((out.table ?? "").length > MAX_FORM) throw new Refuse("提交的内容太长了：一次少粘贴一些");
  return out;
}

// 改好以后回到列表页上显示的那句话（地址里只放代号，不放原话）
const OK = {
  added: "加好了", saved: "保存好了", deleted: "删掉了", renamed: "队名改好了，赛程里也跟着改了",
  moved: "换好队了（赛程没动）", moved_emptied: "换好队了。原来那支队没人了：赛程里还写着它的话，在赛程里换掉，或者用\"并到另一支队\"",
  merged: "并好了：选手都挪过去了，赛程里也跟着改了",
  forgot: "忘掉了：主舞台的下拉里不再有它们（赛程里已经填着的不动）", nochange: "没有改动",
  test: "测试文件发好了（写进去以后读回来核对过，一字不差）", official: "正式文件发好了：玩家下次打开 Evo 的赛事页就能看到",
  platform: "平台开关改好了",
  tracked: "收录了：几分钟内开始抓（每 3 分钟抓一步）", untracked: "不再收录了", refresh: "排上了：几分钟内开始新的一轮",
  matched: "填好了：选手库里记上了 Liquipedia 页面名", rejected: "记下了：以后不再这样建议",
  fixed: "修正好了：页面上用你们改的", unfixed: "撤掉了：改回用 Liquipedia 的", resolved: "处理好了",
  logo: "队徽换好了：下次发布 Evo 就能看到", unlogo: "队徽去掉了",
  created: "建好了：资料页里能搜到他", 
};

function okText(params) {
  const count = (key) => Number.parseInt(params.get(key), 10) || 0;
  if (params.get("ok") === "fromlp") {
    return `从 Liquipedia 加进来 ${count("added")} 位${count("pending") ? `（${count("pending")} 位直播间待填：在下面补上，或者平台选"不直播"）` : ""}`
      + `${count("skipped") ? `，跳过 ${count("skipped")} 位（选手库里已经有了）` : ""}`;
  }
  if (params.get("ok") === "filled") {
    return `补上了 ${count("n")} 位的直播间：下次发布 Evo 就能看到`
      + `${count("skipped") ? `（${count("skipped")} 位在你打开页面以后被人改过，没盖掉）` : ""}`;
  }
  if (params.get("ok") === "logos") {
    return `配好了 ${count("n")} 支队的队徽：下次发布 Evo 就能看到`
      + `${count("skipped") ? `（${count("skipped")} 支在你打开页面以后别人已经配了，没盖掉）` : ""}`;
  }
  if (params.get("ok") === "rooms") {
    const n = count("n");
    return n ? `保存好了：改了 ${n} 位` : "没有改动（和原来一样，没存）";
  }
  if (params.get("ok") === "regions") return count("n") ? `保存好了：改了 ${count("n")} 支队的赛区` : "没有改动（和原来一样，没存）";
  if (params.get("ok") === "pdeleted") return `删掉了 ${count("n")} 位选手`;
  if (params.get("ok") === "tdeleted") return `删掉了 ${count("teams")} 支队（连同 ${count("players")} 位选手）`;
  if (params.get("ok") === "imported") {
    const added = Number.parseInt(params.get("added"), 10) || 0;
    const changed = Number.parseInt(params.get("changed"), 10) || 0;
    return `导入好了：新加 ${added} 条，更新 ${changed} 条`;
  }
  return OK[params.get("ok")] ?? "";
}

export async function handle(request, env, now = Date.now()) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  const bucket = env.DATA;

  // HEAD（测速、看网站通不通的工具常用）：照 GET 走一遍，只回响应头
  if (method === "HEAD") {
    const response = await handle(new Request(request, { method: "GET" }), env, now);
    return new Response(null, { status: response.status, headers: response.headers });
  }
  if (path === "/robots.txt") return respond("User-agent: *\nDisallow: /\n", { type: "text/plain; charset=utf-8" });
  if (path === "/app.js") return respond(APP_JS, { type: "text/javascript; charset=utf-8" });
  if (path === "/favicon.ico") return new Response(null, { status: 204, headers: SECURITY });
  if (method !== "GET" && method !== "POST") return respond("", { status: 405 });
  if (method === "POST" && !sameOrigin(request)) {
    return respond(messagePage("没有保存", "这次提交不是从本站的页面发出的，没有保存。请回到页面重新点一次。"), { status: 403 });
  }

  // 邀请链接
  const join = path.match(/^\/join\/([^/]+)$/);
  if (join) {
    const token = join[1];
    if (method === "GET") {
      const invite = await findInvite(bucket, token, now);
      if (!invite) {
        return respond(messagePage("链接不能用了", "这个邀请链接已经用过、过期了，或者被作废了。请找管理员要一个新的。"), { status: 410 });
      }
      return respond(invitePage(invite, path));
    }
    const accepted = await acceptInvite(bucket, token, now, request.headers.get("User-Agent"));
    if (!accepted) {
      return respond(messagePage("链接不能用了", "这个邀请链接已经用过、过期了，或者被作废了。请找管理员要一个新的。"), { status: 410 });
    }
    return redirect("/", { "Set-Cookie": sessionCookie(accepted.token) });
  }

  const who = await whoami(bucket, request, now);
  if (!who) {
    if (method === "POST") return redirect("/");
    if (path === "/") return respond(loginPage());
    return respond(loginPage(), { status: 401 });
  }
  const me = who.member;

  if (path === "/logout" && method === "POST") {
    await logout(bucket, who.sessionHash);
    return redirect("/", { "Set-Cookie": clearCookie() });
  }
  if (path === "/" && method === "GET") {
    const [playerItems, eventItems, recent, last, idx, no, ov, mp] = await Promise.all([players(bucket), events(bucket), recentLog(bucket, 8),
      published(bucket), index(bucket), rejects(bucket), overrides(bucket), manualPros(bucket)]);
    const pend = pending(playerItems, idx, no, ov, mp);
    const dayStats = (await daily(bucket)).days[isoDay(now + 8 * 3600 * 1000 - 24 * 3600 * 1000)];  // 北京时间的昨天
    return respond(homePage(me, { now, recent, banner: publishBanner(me, await unpublished(env, now), now), stats: { dau: dayStats?.dau ?? null,
      players: playerItems.length, teams: teamsOf(playerItems).size,
      events: eventItems.length, published: last, lpPlayers: Object.values(idx.players).filter((x) => !x.missing).length,
      pending: pendingCount(pend) } }));
  }
  if (path === "/log" && method === "GET") return respond(logPage(me, await recentLog(bucket), now));
  // 数据（11-19）：有后台权限的人都能看（用户定的）；只读算好的文件，不读数据库
  if (path === "/stats" && method === "GET") {
    const range = ["30", "90", "all"].includes(url.searchParams.get("range")) ? url.searchParams.get("range") : "30";
    return respond(statsPage(me, { doc: await daily(bucket), range, now }));
  }
  const notFound = () => respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
  const readOnly = () => respond(messagePage("不能改", "只读的身份只能看，要改请找管理员。", me), { status: 403 });

  // ---------- 从 Liquipedia 补直播间（11-25；规矩在 lpimport.js） ----------
  if (path === "/players/fill-streams") {
    const show = async (options = {}, status = 200) => {
      const [items, idx, changed] = await Promise.all([players(bucket), index(bucket), unpublished(env, now)]);
      return respond(fillStreamsPage(me, { found: fillCandidates(items, idx), banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") return show({ ok: okText(url.searchParams) });
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      const picks = Object.keys(form).filter((k) => /^use_\d+$/.test(k) && form[k] === "1").map((k) => {
        const i = k.slice(4);
        return { id: form[`id_${i}`] ?? "", platform: form[`platform_${i}`] ?? "", room: form[`room_${i}`] ?? "" };
      });
      const result = await fillStreams(bucket, me, picks, now);
      return redirect(`/players/fill-streams?ok=filled&n=${result.done}&skipped=${result.skipped}`);
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 一键配队徽（11-19） ----------
  if (path === "/logos" || path === "/logos/bulk") {
    const show = async (options = {}, status = 200) => {
      const [items, logos, playerItems, changed] = await Promise.all([library(bucket), teamLogos(bucket), players(bucket), unpublished(env, now)]);
      return respond(logosPage(me, { found: autoMatches([...teamsOf(playerItems).keys()], logos, items), libraryCount: items.length,
        banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") return path === "/logos" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (path !== "/logos/bulk") return notFound();
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      const picks = Object.keys(form).filter((k) => /^use_\d+$/.test(k) && form[k] === "1")
        .map((k) => ({ team: form[`team_${k.slice(4)}`] ?? "", file: form[`file_${k.slice(4)}`] ?? "" }));
      if (!picks.length) throw new Refuse("一支都没勾：勾上要用的，再点\"用勾上的这些\"");
      const result = await pickMany(env, me, picks, now);
      return redirect(`/logos?ok=logos&n=${result.done}&skipped=${result.skipped}`);
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 队徽 ----------
  if (path.startsWith("/logo-library/")) {  // 队标库里的图（不公开的存储桶，登录了才看得到）
    const file = path.slice("/logo-library/".length);
    const object = method === "GET" && LOGO_FILE.test(file) ? await bucket.get(libraryKey(file)) : null;
    if (!object) return notFound();
    return new Response(object.body, { headers: { ...SECURITY, "Content-Type": "image/png", "Cache-Control": "private, max-age=86400" } });
  }
  if (path === "/logo" || path.startsWith("/logo/")) {
    const show = async (team, options = {}, status = 200) => {
      const [items, logos, playerItems] = await Promise.all([library(bucket), teamLogos(bucket), players(bucket)]);
      if (!teamsOf(playerItems).has(team)) return respond(messagePage("没有这支队", "选手库里没有这个队名（可能刚改了名）。", me), { status: 404 });
      const q = url.searchParams.get("q") ?? "";
      return respond(logoPage(me, { team, current: logos[team], suggestions: suggest(team, items), results: search(q, items), q,
        libraryCount: items.length, now, ...options }), { status });
    };
    if (path === "/logo" && method === "GET") return show(url.searchParams.get("team") ?? "", { ok: okText(url.searchParams) });
    if (method !== "POST") return notFound();
    if (!can(me, "edit")) return readOnly();
    let team = "";
    try {
      if (path === "/logo/upload") {
        if (Number(request.headers.get("Content-Length") ?? 0) > 200 * 1024) throw new Refuse("图片太大了：最多 60 KB（网页上会自动缩，缩不下来就换一张）");
        const form = await request.formData();
        team = String(form.get("team") ?? "");
        const file = form.get("file");
        await uploadLogo(env, me, team, file && typeof file !== "string" ? await file.arrayBuffer() : null, now);
      } else {
        const form = await readFields(request);
        team = form.team ?? "";
        if (path === "/logo/pick") await pickLogo(env, me, team, form.file ?? "", now);
        else if (path === "/logo/remove") await removeLogo(bucket, me, team, now);
        else return notFound();
      }
      return redirect(logoHref(team, `&ok=${path === "/logo/remove" ? "unlogo" : "logo"}`));
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show(team, { error: error.message }, 400);
    }
  }

  // ---------- 选手库 ----------
  if (path === "/players" || path.startsWith("/players/") || ["/teams", "/teams/rename", "/teams/merge", "/teams/regions", "/teams/delete"].includes(path)) {
    // 赛区（11-21）：地址里的 region / view 只认这几个，别的当作"全部"
    const regionParam = (text) => (Object.hasOwn(REGIONS, text) || text === NONE ? text : "all");
    const region = regionParam(url.searchParams.get("region") ?? "");
    const show = async (options = {}, status = 200) => {
      const [items, logos, changed] = await Promise.all([players(bucket), teamLogos(bucket), unpublished(env, now)]);
      return respond(playersPage(me, items, { now, logos, region, regions: await teamRegions(bucket, items), banner: publishBanner(me, changed, now),
                                              ...options }), { status });
    };
    // 一张表批量修改（11-19 批量填直播间，11-21 名字、战队也能改，按赛区看）。view：pending（默认）、all、大区、none
    const viewParam = (text) => (text === "all" || text === "pending" ? text : regionParam(text) === "all" ? "pending" : text);
    const showRooms = async (options = {}, status = 200) => {
      const [items, changed] = await Promise.all([players(bucket), unpublished(env, now)]);
      return respond(roomsPage(me, items, { now, regions: await teamRegions(bucket, items), banner: publishBanner(me, changed, now), ...options }),
                     { status });
    };
    // 战队页（11-21）
    const showTeams = async (options = {}, status = 200) => {
      const [rows, changed] = await Promise.all([teamOverview(bucket, now), unpublished(env, now)]);
      return respond(teamsPage(me, rows, { now, region, banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") {
      if (path === "/players/rooms") {
        const view = url.searchParams.get("all") === "1" ? "all" : viewParam(url.searchParams.get("view") ?? "");
        return showRooms({ view, ok: okText(url.searchParams) });
      }
      if (path === "/teams") return showTeams({ ok: okText(url.searchParams) });
      return path === "/players" ? show({ ok: okText(url.searchParams) }) : notFound();
    }
    if (!can(me, "edit")) return readOnly();
    let form = {};
    try {
      form = await readFields(request);
      const view = viewParam(form.view ?? (form.all === "1" ? "all" : ""));
      const back = regionParam(form.view ?? "") === "all" ? "" : `&region=${form.view}`;  // 改完回到选手库的同一个赛区标签
      if (path === "/players/rooms") {
        try {
          const n = await saveRooms(bucket, me, form, now);
          return redirect(`/players/rooms?ok=rooms&n=${n}${view === "pending" ? "" : `&view=${view}`}`);
        } catch (error) {
          if (!(error instanceof Refuse)) throw error;
          return showRooms({ view, error: error.message, draft: form }, 400);
        }
      }
      if (path === "/players/bulk-delete") {  // 批量修改页勾选的几位：先问一次，再删（11-21）
        try {
          if (form.confirm !== "1") {
            const items = await players(bucket);
            const picked = items.filter((p) => (form.d ?? []).includes(p.id));
            if (!picked.length) throw new Refuse("没有勾选要删的选手：在要删的人前面打勾，再点“删掉勾选的…”");
            const backTo = `/players/rooms${view === "pending" ? "" : `?view=${view}`}`;
            return respond(confirmPage(me, { title: `删掉 ${picked.length} 位选手？`, lead: "下面这几位会从选手库里删掉（赛程里的队不动）：",
              items: picked.map((p) => ({ text: p.name, meta: p.team })), action: "/players/bulk-delete",
              hidden: [...picked.map((p) => ["d", p.id]), ["view", view]], back: backTo, button: `确定删掉这 ${picked.length} 位` }));
          }
          const n = await deletePlayers(bucket, me, form.d ?? [], now);
          return redirect(`/players/rooms?ok=pdeleted&n=${n}${view === "pending" ? "" : `&view=${view}`}`);
        } catch (error) {
          if (!(error instanceof Refuse)) throw error;
          return showRooms({ view, error: error.message }, 400);
        }
      }
      if (path === "/teams/regions" || path === "/teams/delete") {
        const regionBack = regionParam(form.region ?? "") === "all" ? "" : `region=${form.region}`;
        try {
          if (path === "/teams/regions") {
            const n = await saveRegions(bucket, me, form, now);
            return redirect(`/teams?ok=regions&n=${n}${regionBack ? `&${regionBack}` : ""}`);
          }
          const picked = [...new Set((form.del ?? []).map((t) => t.trim()).filter(Boolean))];
          if (!picked.length) throw new Refuse("没有勾选要删的队：在要删的队前面打勾，再点“删掉勾选的队…”");
          if (form.confirm !== "1") {
            const items = await players(bucket);
            const members = (t) => items.filter((p) => p.team === t);
            return respond(confirmPage(me, { title: `删掉 ${picked.length} 支队？`, lead: "下面这几支队连同队里的选手一起删掉（队徽、赛区也去掉）：",
              items: picked.map((t) => ({ text: t, meta: `${members(t).length} 位选手：${members(t).map((p) => p.name).join("、")}` })),
              action: "/teams/delete", hidden: [...picked.map((t) => ["del", t]), ["region", form.region ?? ""]],
              back: `/teams${regionBack ? `?${regionBack}` : ""}`, button: `确定删掉这 ${picked.length} 支队` }));
          }
          const result = await deleteTeams(bucket, me, picked, now);
          return redirect(`/teams?ok=tdeleted&teams=${result.teams}&players=${result.players}${regionBack ? `&${regionBack}` : ""}`);
        } catch (error) {
          if (!(error instanceof Refuse)) throw error;
          return showTeams({ error: error.message }, 400);
        }
      }
      if (path === "/players/add") {
        await addPlayer(bucket, me, form, now);
        return redirect("/players?ok=added");
      }
      if (path === "/players/import") {
        const { rows, errors } = importPlayers(form.table);
        if (errors.length) throw new Refuse(`没有导入，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
        if (!rows.length) throw new Refuse("没读到任何一行：在 Excel 里选中表格、复制，再粘贴进来");
        const result = await importPlayerRows(bucket, me, rows, now);
        return redirect(`/players?ok=imported&added=${result.added}&changed=${result.changed}`);
      }
      if (path === "/teams/rename") {
        await renameTeam(bucket, me, form.from, form.to, now);
        return redirect("/players?ok=renamed");
      }
      if (path === "/teams/merge") {
        await mergeTeam(bucket, me, form.from, form.to, now);
        return redirect("/players?ok=merged");
      }
      const action = path.match(/^\/players\/([0-9a-f]{8})\/(save|delete|move)$/);
      if (!action) return notFound();
      // 改完回到那个人（#p-…）、删了回到他那一队（#t-…）；换队以后他可能在别的赛区，回到"全部"
      if (action[2] === "move") {
        const moved = await movePlayer(bucket, me, action[1], form.team, form.rev, now);
        return redirect(`/players?ok=${moved.emptied ? "moved_emptied" : "moved"}#p-${action[1]}`);
      }
      if (action[2] === "save") {
        await savePlayer(bucket, me, action[1], form, now);
        return redirect(`/players?ok=saved${back}#p-${action[1]}`);
      }
      const gone = await deletePlayer(bucket, me, action[1], now);
      return redirect(`/players?ok=deleted${back}#${teamAnchor(gone.team)}`);
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message, draft: path === "/players/add" ? form : null,
                    draftTable: path === "/players/import" ? form.table : "" }, 400);
    }
  }

  // ---------- 赛程 ----------
  if (path === "/events" || path.startsWith("/events/")) {
    const show = async (options = {}, status = 200) => {
      const [eventItems, playerItems, changed, rooms] = await Promise.all([events(bucket), players(bucket), unpublished(env, now), savedRooms(bucket)]);
      return respond(eventsPage(me, eventItems, playerItems, { now, rooms, banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") {
      if (path !== "/events") return notFound();
      const copyId = url.searchParams.get("copy");  // 点了某一场的"复制"：添加表单照它填好
      const copy = copyId && can(me, "edit") ? (await events(bucket)).find((e) => e.id === copyId) ?? null : null;
      return show({ ok: okText(url.searchParams), copy, error: copyId && !copy ? "要复制的那一场找不到了（可能刚被删了）" : "" });
    }
    if (!can(me, "edit")) return readOnly();
    let form = {};
    try {
      form = await readFields(request);
      if (path === "/events/add") {
        await addEvent(bucket, me, form, now);
        return redirect("/events?ok=added");
      }
      if (path === "/events/import") {
        const known = [...teamsOf(await players(bucket)).keys()];
        const { rows, errors } = importEvents(form.table, known);
        if (errors.length) throw new Refuse(`没有导入，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
        if (!rows.length) throw new Refuse("没读到任何一行：在 Excel 里选中表格、复制，再粘贴进来");
        const result = await importEventRows(bucket, me, rows, now);
        return redirect(`/events?ok=imported&added=${result.added}&changed=${result.changed}`);
      }
      if (path === "/events/rooms/forget") {  // 忘掉几个记下的主舞台直播间（11-21）
        await forgetRooms(bucket, me, form.forget ?? [], now);
        return redirect("/events?ok=forgot#rooms");
      }
      const action = path.match(/^\/events\/([0-9a-f]{8})\/(save|delete)$/);
      if (!action) return notFound();
      if (action[2] === "save") await saveEvent(bucket, me, action[1], form, now);
      else await deleteEvent(bucket, me, action[1], now);
      // 改完回到那一场（11-21：赛管说保存以后跳回最上面、又要往下找）
      return redirect(action[2] === "save" ? `/events?ok=saved#e-${action[1]}` : "/events?ok=deleted");
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message, draft: path === "/events/add" ? form : null,
                    draftTable: path === "/events/import" ? form.table : "" }, 400);
    }
  }

  // ---------- Liquipedia ----------
  if (path === "/liquipedia" || path.startsWith("/liquipedia/")) {
    const show = async (options = {}, status = 200) => {
      const [trackedItems, idx, s, playerItems, no, ov, mp] = await Promise.all([tracked(bucket), index(bucket), state(bucket), players(bucket),
        rejects(bucket), overrides(bucket), manualPros(bucket)]);
      return respond(liquipediaPage(me, { tracked: trackedItems, idx, progress: progress(s, now), pend: pending(playerItems, idx, no, ov, mp),
        keySet: Boolean(String(env.LIQUIPEDIA_API_KEY ?? "").trim()), now, ...options }), { status });
    };
    if (method === "GET") return path === "/liquipedia" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      if (path === "/liquipedia/track") {
        await addTracked(bucket, me, form.page, now);
        return redirect("/liquipedia?ok=tracked");
      }
      if (path === "/liquipedia/untrack") {
        await removeTracked(bucket, me, form.page ?? "", now);
        return redirect("/liquipedia?ok=untracked");
      }
      if (path === "/liquipedia/to-roster") {
        const r = await addFromLiquipedia(bucket, me, await eventEntries(bucket, form.page ?? ""), `比赛 ${form.page ?? ""}`, now);
        return redirect(`/players?ok=fromlp&added=${r.added}&pending=${r.pending}&skipped=${r.skipped}`);
      }
      if (path === "/liquipedia/refresh") {
        await requestRefresh(bucket);
        return redirect("/liquipedia?ok=refresh");
      }
      if (path === "/liquipedia/match") {
        if (form.answer === "yes") await confirmMatch(bucket, me, form.player ?? "", form.page ?? "", now);
        else await rejectMatch(bucket, me, form.player ?? "", form.page ?? "", now);
        return redirect(`/liquipedia?ok=${form.answer === "yes" ? "matched" : "rejected"}#confirm`);
      }
      return notFound();
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 资料：选手、战队（Liquipedia 抓来的 + 手动修正） ----------
  if (path === "/pros" || path.startsWith("/pros/") || path.startsWith("/teams/")) {
    const decode = (text) => {
      try {
        return decodeURIComponent(text);
      } catch {
        return null;
      }
    };
    const load = async () => {
      const [idx, ov, manual, mp] = await Promise.all([index(bucket), overrides(bucket), players(bucket), manualPros(bucket)]);
      const linkedBy = new Map();
      for (const m of manual) {
        if (!m.liquipedia) continue;
        if (!linkedBy.has(m.liquipedia)) linkedBy.set(m.liquipedia, []);
        linkedBy.get(m.liquipedia).push(m);
      }
      const teams = Object.values(idx.teams).filter((t) => !t.missing);
      // 手动建的资料（11-19）和 Liquipedia 抓来的放在一起；Liquipedia 有了这个页面的，用 Liquipedia 的
      return { idx, ov, linkedBy, teams, knownTeams: new Set(teams.map((t) => t.page)), mp, manual,
               all: [...Object.values(idx.players).filter((p) => !p.missing).map((p) => merged(p, ov[p.page])), ...visible(mp, idx)] };
    };
    if (path === "/pros" && method === "GET") {
      const d = await load();
      return respond(prosPage(me, { q: url.searchParams.get("q") ?? "", players: d.all, teams: d.teams, linkedBy: d.linkedBy, now,
        ok: okText(url.searchParams) }));
    }
    // 手动建一份选手资料（Liquipedia 上没有页面的；11-19 用户定的，管理员和编辑都能建）
    if (path === "/pros/new") {
      if (!can(me, "edit")) return readOnly();
      const names = async () => (await players(bucket)).map((p) => p.name);
      if (method === "GET") {
        const q = (k) => url.searchParams.get(k) ?? "";
        return respond(newProPage(me, { draft: { page: q("page"), link: q("link"), team: q("team") }, playerNames: await names() }));
      }
      let form = {};
      try {
        form = await readFields(request);
        const page = await createPro(bucket, me, form, await index(bucket), now);
        return redirect(`${proHref(page)}?ok=created`);
      } catch (error) {
        if (!(error instanceof Refuse)) throw error;
        return respond(newProPage(me, { draft: form, playerNames: await names(), error: error.message }), { status: 400 });
      }
    }
    const toRoster = path.match(/^\/teams\/([^/]+)\/to-roster$/);
    if (toRoster) {
      if (method !== "POST") return notFound();
      if (!can(me, "edit")) return readOnly();
      const page = decode(toRoster[1]);
      try {
        const d = await load();
        const r = await addFromLiquipedia(bucket, me, teamEntries(d.idx, d.ov, page), `战队 ${page}`, now);
        return redirect(`/players?ok=fromlp&added=${r.added}&pending=${r.pending}&skipped=${r.skipped}`);
      } catch (error) {
        if (!(error instanceof Refuse)) throw error;
        return respond(messagePage("没有加", error.message, me), { status: 400 });
      }
    }
    const teamMatch = path.match(/^\/teams\/([^/]+)$/);
    if (teamMatch) {
      if (method !== "GET") return notFound();
      const page = decode(teamMatch[1]);
      const d = await load();
      const t = d.idx.teams[page];
      if (!t || t.missing) return respond(messagePage("没有这支战队的资料", "可能还没抓到，或者地址写错了。", me), { status: 404 });
      const squads = await squadsOf(bucket);
      const current = d.all.filter((p) => (p.teamPage === page && !p.fixed?.team) || ((p.fixed?.team || p.manual) && p.team === t.name));
      const past = [];
      for (const p of d.all) {
        for (const s of squads[p.page] ?? []) {
          if (s.teamPage === page && (!s.type || s.type === "player") && (s.leave || s.status !== "active")) past.push({ p, s });
        }
      }
      past.sort((a, b) => (b.s.leave || "").localeCompare(a.s.leave || ""));
      const inRoster = new Set((await players(bucket)).map((p) => p.liquipedia).filter(Boolean));
      return respond(teamProfilePage(me, { t, current, past, now, inRoster }));
    }
    const pro = path.match(/^\/pros\/([^/]+)(?:\/(fix|unfix|resolve|edit|delete))?$/);
    const page = pro ? decode(pro[1]) : null;
    if (!pro || page === null) return notFound();
    const show = async (options = {}, status = 200) => {
      const d = await load();
      const raw = d.idx.players[page];
      if (!onLiquipedia(d.idx, page)) {
        const item = d.mp[page];  // 手动建的（11-19）
        if (!item) return respond(messagePage("没有这位选手的资料", "可能还没抓到，或者 Liquipedia 上没有这个页面（可以在资料页手动建一份）。", me), { status: 404 });
        return respond(manualProPage(me, { p: visible({ [page]: item }, d.idx)[0], item, linked: d.linkedBy.get(page) ?? [], now, ...options }), { status });
      }
      const [squads, results] = await Promise.all([squadsOf(bucket), resultsOf(bucket, page)]);
      return respond(proPage(me, { p: merged(raw, d.ov[page]), squads: squads[page] ?? [], results, linked: d.linkedBy.get(page) ?? [],
        knownTeams: d.knownTeams, now, shadowed: d.mp[page] ?? null, ...options }), { status });
    };
    if (!pro[2]) return method === "GET" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (method !== "POST") return notFound();
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      if (pro[2] === "fix") {
        await setOverride(bucket, me, page, form.field ?? "", form.value ?? "", form.reason ?? "", await index(bucket), now);
        return redirect(`${proHref(page)}?ok=fixed`);
      }
      if (pro[2] === "unfix") {
        await removeOverride(bucket, me, page, form.field ?? "", now);
        return redirect(`${proHref(page)}?ok=unfixed`);
      }
      if (pro[2] === "edit") {
        await savePro(bucket, me, page, form, now);
        return redirect(`${proHref(page)}?ok=saved`);
      }
      if (pro[2] === "delete") {
        const shown = onLiquipedia(await index(bucket), page);
        await deletePro(bucket, me, page, now);
        return redirect(shown ? `${proHref(page)}?ok=deleted` : "/pros?ok=deleted");
      }
      await resolveConflict(bucket, me, page, form.field ?? "", form.keep === "ours", now);
      return redirect("/liquipedia?ok=resolved#confirm");
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return pro[2] === "resolve" ? redirect("/liquipedia#confirm") : show({ error: error.message }, 400);
    }
  }

  // ---------- 发布 ----------
  if (path === "/publish" || path.startsWith("/publish/")) {
    const show = async (options = {}, status = 200) => {
      const [view, last, changed] = await Promise.all([preview(env, now), published(bucket), unpublished(env, now)]);
      return respond(publishPage(me, view, last, { now, banner: publishBanner(me, changed, now, true), ...options }), { status });
    };
    if (method === "GET") return path === "/publish" ? show({ ok: okText(url.searchParams) }) : notFound();
    try {
      if (path === "/publish/test" || path === "/publish/official") {
        if (!can(me, "publish")) return respond(messagePage("不能发布", "发布要找管理员（或者请管理员给你开发布）。", me), { status: 403 });
        const target = path.slice("/publish/".length);
        await publish(env, me, target, now);
        return redirect(`/publish?ok=${target}`);
      }
      if (path === "/publish/platform") {
        if (me.role !== "admin") return respond(messagePage("不能改", "平台开关只有管理员能改。", me), { status: 403 });
        const form = await readFields(request);
        await setPlatform(env, me, form.target, form.platform, form.on === "1", now);
        return redirect("/publish?ok=platform");
      }
      return notFound();
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 成员（只有管理员） ----------
  if (path === "/members" || path.startsWith("/members/") || path.startsWith("/invites/")) {
    if (!can(me, "members")) return respond(messagePage("只有管理员能看", "成员页只有管理员能打开。", me), { status: 403 });
    const view = (options, status = 200) => respond(membersPage(me, options.data ?? who.data,
      { origin: url.origin, now, ...options }), { status });
    if (path === "/members" && method === "GET") return view({ ok: url.searchParams.get("ok") ?? "" });
    if (method !== "POST") return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });

    const action = path.match(/^\/(members|invites)\/([^/]+)\/(invite|role|remove|link|cancel)$/)
      ?? (path === "/members/invite" ? [path, "members", "", "invite"] : null);
    if (!action) return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
    const [, kind, id, verb] = action;
    try {
      if (kind === "members" && verb === "invite" && !id) {
        const created = await createInvite(bucket, me, await readForm(request), now);
        return view({ created, data: (await load(bucket)).data });
      }
      if (!validId(id)) throw new Refuse("没有这个成员（可能刚被别人移出了）");
      if (kind === "members" && verb === "link") {
        const created = await createInvite(bucket, me, { memberId: id }, now);
        return view({ created, data: (await load(bucket)).data });
      }
      if (kind === "members" && verb === "role") {
        await setRole(bucket, id, await readForm(request));
        return redirect("/members?ok=role");
      }
      if (kind === "members" && verb === "remove") {
        await removeMember(bucket, id);
        return redirect(id === me.id ? "/" : "/members?ok=removed");
      }
      if (kind === "invites" && verb === "cancel") {
        await cancelInvite(bucket, id);
        return redirect("/members?ok=cancelled");
      }
      return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return view({ error: error.message, data: (await load(bucket)).data }, 400);
    }
  }

  return notFound();
}

export default {
  // 定时任务（wrangler.toml 的 crons，每 3 分钟）：从 Liquipedia 抓一步（lpsync.js）；「数据」页的数离上次超过 3 小时就增量算一次
  // （statsroll.js）。两件事互不影响：一件出错另一件照做
  async scheduled(controller, env, ctx) {
    const now = Date.now();
    ctx.waitUntil(Promise.allSettled([tick(env, now), rollup(env, now)]));
  },
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (error) {
      if (error instanceof Refuse) return respond(messagePage("没有保存", error.message), { status: 400 });
      return respond(layout({ title: "出错了", narrow: true, body: `<section class="card stack"><h1>服务器出错了</h1>
        <p>请过一会儿再试。一直这样的话告诉管理员。</p></section>` }), { status: 500 });
    }
  },
};
