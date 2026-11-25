// 选手库、赛程、发布、改动记录这几页（2026-11-17 第 2 步）。外框、颜色在 pages.js。
// 只读的人能看，看不到改的按钮；编辑能改；能发布的人（管理员、开了发布的编辑）才有发布按钮；平台开关只有管理员。

import { can } from "./auth.js";
import { EVENT_COLUMNS, MAIN_MAX, NO_STREAM, NO_STREAM_NAME, PLATFORMS, PLAYER_COLUMNS, STATUS_WORDS, TYPES, beijingText, checkPlayers, eventLabel, isoBeijing,
  parseBeijing, parseIso, roomUrl, status } from "./esports.js";
import { mainChoices, teamsOf } from "./data.js";
import { PUBLIC_URL, TARGETS, describe } from "./publish.js";
import { ago, escape, layout, logList, noteList } from "./pages.js";
import { PUBLIC_LOGO_URL } from "./logos.js";
import { logoBox, logoHref } from "./pages_logos.js";
import { regionName, regionTabs, teamAnchor } from "./pages_teams.js";
import { NONE } from "./teams.js";

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error).replace(/\n/g, "<br>")}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

function by(item, now) {
  return `${escape(item.updatedBy ?? item.createdBy ?? "")} ${escape(ago(item.updated ?? item.created, now))}改的`;
}

function confirmDelete(action, what, hidden = "") {
  return `<details class="act"><summary class="btn btn-danger">删除</summary>
    <div class="panel"><p class="small">删掉「${escape(what)}」？删了找不回来，要的话得重新添加。</p>
      <form method="post" action="${action}">${hidden}<button class="btn btn-danger btn-wide" type="submit">确定删除</button></form></div></details>`;
}

// ---------- 选手库 ----------

function platformOptions(selected) {
  return Object.entries(PLATFORMS).map(([key, p]) => `<option value="${key}"${key === selected ? " selected" : ""}>${p.name}</option>`).join("");
}

// 选手的平台多一个"不直播"（主舞台不能选它）；从 Liquipedia 加进来、直播间还没填的，先显示"请选平台"
function playerPlatformOptions(selected) {
  return (selected ? "" : '<option value="" selected>请选平台</option>') + platformOptions(selected)
    + `<option value="${NO_STREAM}"${selected === NO_STREAM ? " selected" : ""}>${NO_STREAM_NAME}</option>`;
}

function playerForm(action, p, teamNames, submit, view = "") {
  return `<form class="stack" method="post" action="${action}">
    ${p.rev ? `<input type="hidden" name="rev" value="${p.rev}">` : ""}${view ? `<input type="hidden" name="view" value="${escape(view)}">` : ""}
    <label class="field">选手<span class="help">直播间里的名字，比如 Kite</span>
      <input class="input" name="name" value="${escape(p.name)}" maxlength="40" required autocomplete="off"></label>
    <label class="field">战队<span class="help">从下拉里选已有的队，或者写一个新队名</span>
      <input class="input" name="team" value="${escape(p.team)}" maxlength="40" required list="team-names" autocomplete="off"></label>
    <div class="two">
      <label class="field">平台<span class="help">不开直播的选"不直播"</span><select class="input" name="platform" required>${playerPlatformOptions(p.platform)}</select></label>
      <label class="field">房间号<span class="help">直播间地址最后那一段：一般是数字；抖音也可以是主播的抖音号（字母、数字、下划线），Twitch 写登录名（字母、数字、下划线）。不直播的空着</span>
        <input class="input" name="room" value="${escape(p.room)}" maxlength="24" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    </div>
    <label class="field">Liquipedia 页面名<span class="help">可以空着；贴 Liquipedia 选手页的网址就行，会自动存成页面名（比如 Panic_(American_player)）</span>
      <input class="input" name="liquipedia" value="${escape(p.liquipedia)}" maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    <div><button class="btn btn-primary" type="submit">${submit}</button></div>
  </form>`;
}

function importBox(action, columns, example, draft = "") {
  return `<details class="card fold"${draft ? " open" : ""}><summary class="fold-head"><span>从 Excel 粘贴导入</span><span class="muted small">一次加很多行</span></summary>
  <form class="stack" method="post" action="${action}">
    <p class="small muted">在 Excel 或 WPS 里选中表格（带不带表头都行），复制，粘贴到下面。列的顺序：${columns.map(escape).join("、")}。
      名字已经有的会更新，没有的新加；有一行不对就整批都不存，会告诉你是第几行。</p>
    <textarea class="input mono" name="table" rows="6" required placeholder="${escape(example)}">${escape(draft)}</textarea>
    <div><button class="btn btn-primary" type="submit">导入</button></div>
  </form></details>`;
}

// 直播间待填的人按队列出来，默认收起（不挡发布：发布时按"不直播"算）
function pendingRooms(items) {
  const byTeam = new Map();
  for (const p of items.filter((x) => !x.platform)) {
    if (!byTeam.has(p.team)) byTeam.set(p.team, []);
    byTeam.get(p.team).push(p.name);
  }
  const count = [...byTeam.values()].reduce((n, names) => n + names.length, 0);
  if (!count) return "";
  return `<details class="note note-info"><summary><span>${count} 位选手直播间待填<span class="help">（${byTeam.size} 支队；不补也能发布，发布时按"不直播"算）</span></span></summary>
    <ul class="plain team-pend">${[...byTeam].map(([team, names]) => `<li><b>${escape(team)}</b>：${names.map(escape).join("、")}</li>`).join("")}</ul>
    <p><a class="btn" href="/players/rooms">在一张表里填 ›</a></p></details>`;
}

// ---------- 一张表批量修改选手（2026-11-19 用户要的批量填直播间；2026-11-21 赛管毛毛要的"支持多条修改"：名字、战队也能改，按赛区看，勾选几位一起删）----------
// view：pending 直播间待填的（默认）、all 全部、大区 / none 某个赛区。按队分组。只存改过的，有一个不对整批不存（data.js 的 saveRooms）。
// 勾选的几位点"删掉勾选的…"先到确认页（worker.js 的 /players/bulk-delete）。draft：保存出错时填的样子（form 原样），照它显示
export function roomsPage(me, items, { now, view = "pending", regions = {}, error = "", ok = "", draft = null, banner = "" }) {
  const editable = can(me, "edit");
  const regionOf = (p) => regions[p.team]?.region ?? NONE;
  const pendingCount = items.filter((p) => !p.platform).length;
  const shown = view === "all" ? items
    : view === "pending" ? items.filter((p) => !p.platform || (draft && Object.hasOwn(draft, `p_${p.id}`)))
      : items.filter((p) => regionOf(p) === view || (draft && Object.hasOwn(draft, `p_${p.id}`)));
  const teams = [...new Set(shown.map((p) => p.team))];
  const value = (p, key, current) => (draft && Object.hasOwn(draft, `${key}_${p.id}`) ? draft[`${key}_${p.id}`] : current);
  const off = editable ? "" : " disabled";
  const row = (p) => {
    const platform = value(p, "p", p.platform);
    const room = value(p, "r", p.room);
    return `<li class="bulk-row bulk-edit" id="p-${p.id}">
      <div class="bulk-who">${editable ? `<label class="del-pick"><input type="checkbox" name="d" value="${p.id}" aria-label="勾选 ${escape(p.name)}（删掉用）"><span class="small muted">勾选</span></label>` : ""}
        ${p.platform ? "" : '<span class="badge badge-warn">待填</span>'}
        ${p.platform && p.platform !== NO_STREAM ? `<a class="small" href="${escape(roomUrl(p.platform, p.room))}" target="_blank" rel="noopener noreferrer">打开直播间</a>` : ""}</div>
      <input type="hidden" name="v_${p.id}" value="${p.rev ?? ""}">
      <input class="input" name="n_${p.id}" value="${escape(value(p, "n", p.name))}" aria-label="${escape(p.name)} 的名字" maxlength="40" required autocomplete="off"${off}>
      <input class="input" name="t_${p.id}" value="${escape(value(p, "t", p.team))}" list="team-names" aria-label="${escape(p.name)} 的战队" maxlength="40" required autocomplete="off"${off}>
      <select class="input" name="p_${p.id}" aria-label="${escape(p.name)} 的平台"${off}>
        ${platform ? "" : '<option value="" selected>直播间待填</option>'}${platformOptions(platform)}
        <option value="${NO_STREAM}"${platform === NO_STREAM ? " selected" : ""}>${NO_STREAM_NAME}</option></select>
      <input class="input" name="r_${p.id}" value="${escape(room)}" placeholder="房间号" aria-label="${escape(p.name)} 的房间号" maxlength="24"
        autocomplete="off" autocapitalize="off" spellcheck="false"${off}>
    </li>`;
  };
  const groups = teams.map((team) => `<section class="bulk-team" id="${teamAnchor(team)}"><h3>${escape(team)}${view === "pending" || view === "all"
    ? ` <span class="badge">${escape(regionName(regions[team]?.region))}</span>` : ""}</h3>
    <div class="bulk-head" aria-hidden="true"><span>名字</span><span>战队</span><span>平台</span><span>房间号</span></div>
    <ul class="list bulk-list">${shown.filter((p) => p.team === team).map(row).join("")}</ul></section>`).join("");
  const counts = {};
  for (const p of items) counts[regionOf(p)] = (counts[regionOf(p)] ?? 0) + 1;
  const href = (k) => (k === "pending" ? "/players/rooms" : `/players/rooms?view=${k}`);
  const tabs = regionTabs(view, counts, href, items.length, { label: "位选手",
    extra: [`<a href="/players/rooms"${view === "pending" ? ' aria-current="page"' : ""}>直播间待填（${pendingCount}）</a>`] });
  const datalist = `<datalist id="team-names">${[...teamsOf(items).keys()].map((t) => `<option value="${escape(t)}">`).join("")}</datalist>`;
  return layout({ title: "批量修改选手", me, current: "players", banner, body: `
<p class="small"><a href="/players">‹ 选手库</a></p>
<div class="stack"><h1>批量修改选手</h1>
  <p class="muted">一张表里改很多人的名字、战队、平台和房间号，点一次保存。战队可以从下拉里选，也可以写新队名（就是换队）。房间号是直播间地址最后那一段：一般是数字；抖音也可以是主播的抖音号，Twitch 写登录名。
    不开直播的选"不直播"，房间号空着。只存改过的；有一个人不对就整批都不存，会说是谁。要删的人先勾上，再点"删掉勾选的…"（删之前会再问一次）。</p></div>
${notes({ error, ok })}
${tabs}
${shown.length ? `${datalist}<form class="stack" method="post" action="/players/rooms"><input type="hidden" name="view" value="${escape(view)}">
  ${groups}
  ${editable ? `<div class="bulk-bar"><button class="btn btn-primary" type="submit">保存改过的</button>
    <button class="btn btn-danger" type="submit" formaction="/players/bulk-delete" formnovalidate>删掉勾选的…</button>
    <span class="small muted">改了还没保存的，换页、刷新就没了</span></div>` : ""}
</form>` : `<p class="muted">${view === "pending" ? "没有直播间待填的人了。" : "这里还没有人。"}</p>`}` });
}

// region（11-21 赛管要的分赛区）：all 全部，或者一个大区 / none 没标赛区；regions 是 teams.js 的 teamRegions。
// 每支队、每个人有编号（#t-…、#p-…）：改完、换队、删人以后回到原来的地方，不跳回最上面（用户 11-21："改动一条保存直接刷新到最上面了，又要拖下来找"）
export function playersPage(me, items, { now, error = "", ok = "", draft = null, draftTable = "", logos = {}, banner = "", regions = {}, region = "all" }) {
  const editable = can(me, "edit");
  const regionOf = (team) => regions[team]?.region ?? NONE;
  const allTeams = teamsOf(items);
  const teams = new Map([...allTeams].filter(([team]) => region === "all" || regionOf(team) === region));
  const view = region === "all" ? "" : region;
  const hiddenView = view ? `<input type="hidden" name="view" value="${escape(view)}">` : "";
  const problems = checkPlayers(items).errors;
  const datalist = `<datalist id="team-names">${[...allTeams.keys()].map((t) => `<option value="${escape(t)}">`).join("")}</datalist>`;
  const blank = { name: "", team: "", platform: "douyu", room: "", liquipedia: "" };
  const add = editable ? `<details class="card fold"${!items.length || draft ? " open" : ""}><summary class="fold-head"><span>添加选手</span><span class="muted small">一个一个加</span></summary>
    ${playerForm("/players/add", draft ? { ...blank, ...draft, rev: undefined } : blank, allTeams, "添加")}</details>` : "";
  const sections = [...teams.keys()].map((team) => {
    const members = items.filter((p) => p.team === team);
    const size = `<span class="badge">${members.length} 人</span>`;  // 一支队几个人不限（11-18 用户定的）
    const rename = editable ? `<details class="act"><summary class="btn">改队名</summary>
      <form class="panel" method="post" action="/teams/rename"><input type="hidden" name="from" value="${escape(team)}">
        <label class="field">新队名<span class="help">选手库和赛程里一起改</span>
          <input class="input" name="to" value="${escape(team)}" maxlength="40" required></label>
        <button class="btn btn-primary" type="submit">改名</button></form></details>` : "";
    const others = [...allTeams.keys()].filter((t) => t !== team);
    // 两支队并成一支（11-19 用户要的）：这一队的人都挪过去，赛程跟着改。队名用页面上那一份下拉（datalist），不每个按钮放一份 40 个选项
    const merge = editable && others.length ? `<details class="act"><summary class="btn">并到另一支队</summary>
      <form class="panel" method="post" action="/teams/merge"><input type="hidden" name="from" value="${escape(team)}">
        <label class="field">并进哪一队<span class="help">点输入框从下拉里选，或者打几个字</span>
          <input class="input" name="to" list="team-names" maxlength="40" required autocomplete="off"></label>
        <p class="small muted">「${escape(team)}」的 ${members.length} 位选手都挪过去，赛程里的「${escape(team)}」也换成它（同一场里两支都有的只留一个）；
          那一队没有队徽的话用「${escape(team)}」的。</p>
        <button class="btn btn-primary" type="submit">并过去</button></form></details>` : "";
    // 选手换队（11-19 用户要的）：从下拉里选，或者写一个新队名
    const move = (p) => `<details class="act"><summary class="btn">换队</summary>
      <form class="panel" method="post" action="/players/${p.id}/move"><input type="hidden" name="rev" value="${p.rev}">${hiddenView}
        <label class="field">换到哪一队<span class="help">点输入框从下拉里选；选手库里还没有的队，直接写新队名</span>
          <input class="input" name="team" list="team-names" maxlength="40" required autocomplete="off"></label>
        <p class="small muted">只挪「${escape(p.name)}」一个人，赛程不动。整支队改名、并队用队名旁边的按钮。</p>
        <button class="btn btn-primary" type="submit">换过去</button></form></details>`;
    const rows = members.map((p) => `<li class="row" id="p-${p.id}">
      <div class="row-main">
        <div class="row-name">${escape(p.name)}</div>
        <div class="row-meta">${!p.platform ? '<span class="badge badge-warn">直播间待填</span>' : p.platform === NO_STREAM ? '<span class="badge">不直播</span>'
          : `<a href="${escape(roomUrl(p.platform, p.room))}" target="_blank" rel="noopener noreferrer">${PLATFORMS[p.platform].name} ${escape(p.room)}</a>`}
          ${p.liquipedia ? `<span>Liquipedia：${escape(p.liquipedia)}</span>` : ""}<span>${by(p, now)}</span></div>
      </div>
      ${editable ? `<div class="row-actions">
        <details class="act"><summary class="btn">改</summary><div class="panel">${playerForm(`/players/${p.id}/save`, p, allTeams, "保存", view)}</div></details>
        ${move(p)}
        ${confirmDelete(`/players/${p.id}/delete`, p.name, hiddenView)}</div>` : ""}
    </li>`).join("");
    const logo = logos[team]?.logo;
    const badge = `<a class="logo-link" href="${logoHref(team)}" title="队徽">${logo ? logoBox(PUBLIC_LOGO_URL + logo, team, "small")
      : '<span class="logo-box logo-small logo-empty" aria-hidden="true">＋</span>'}<span class="small">${logo ? "队徽" : "加队徽"}</span></a>`;
    const where = regions[team] ? `<a class="badge" href="/teams${regionOf(team) === NONE ? "" : `?region=${regionOf(team)}`}" title="在战队页改赛区">${escape(regionName(regionOf(team)))}${regions[team].guessed ? "（猜的）" : ""}</a>` : "";
    return `<section class="card" id="${teamAnchor(team)}"><div class="title-row team-head">${badge}<h2>${escape(team)}</h2>${size}${where}<span class="grow"></span>${rename}${merge}</div>
      <ul class="list">${rows}</ul></section>`;
  }).join("");
  return layout({ title: "选手库", me, current: "players", banner, body: `
<div class="stack"><h1>选手库</h1>
  <p class="muted">${items.length} 位选手、${allTeams.size} 支队。一支队几个人都行（不开直播的平台选"不直播"），一个选手只能在一支队。点平台和房间号能打开直播间核对。</p>
  ${items.length ? `<p class="links">${editable ? `<a href="/players/rooms${view ? `?view=${view}` : ""}">在一张表里批量修改 ›</a>` : ""}<a href="/teams">战队和赛区 ›</a>${editable ? '<a href="/logos">一键配队徽 ›</a>' : ""}</p>` : ""}</div>
${notes({ error, ok })}
${noteList("error", `发布前要改好（${problems.length} 处）：`, problems)}
${pendingRooms(items)}
${datalist}${add}
${items.length ? regionTabs(region, Object.fromEntries([...new Set([...allTeams.keys()].map(regionOf))].map((k) => [k, [...allTeams.keys()].filter((t) => regionOf(t) === k).length])),
  (k) => (k === "all" ? "/players" : `/players?region=${k}`), allTeams.size) : ""}
${teams.size > 1 ? `<nav class="team-index" aria-label="跳到某一队">${[...teams.keys()].map((t) => `<a href="#${teamAnchor(t)}">${escape(t)}</a>`).join("")}</nav>` : ""}
${sections || `<p class="muted">${items.length ? "这个赛区还没有队。" : "还没有选手。"}</p>`}
${editable ? importBox("/players/import", PLAYER_COLUMNS, "Kite\t星火电竞\t斗鱼\t520013\t", draftTable) : ""}` });
}

// ---------- 赛程 ----------

function datetimeValue(iso) {
  const ms = parseIso(iso);
  return ms === null ? "" : beijingText(ms).replace(" ", "T");
}

function asIso(text) {
  const ms = parseBeijing(text ?? "");
  return ms === null ? "" : isoBeijing(ms);
}

// 主舞台：播导播视角的直播间、观战的主播（Evo 看直播页队伍栏最上面一栏）。已有的几行 + 空着的几行（新的一场 8 行，改的时候 4 行），最多 24 行。
// 名字那一格在最前面，下拉是记下的直播间和选手库里开直播的人（页面上一份 datalist main-rooms）：选了以后 /app.js 填好平台和房间号，
// 没有脚本时保存的时候照名字补（data.js 的 fillMain）
function mainFields(main) {
  const blank = Math.min(main.length ? 4 : 8, MAIN_MAX - main.length);
  const rows = [...main, ...Array.from({ length: blank }, () => ({ platform: "", room: "", name: "" }))];
  const row = (m, i) => `<div class="main-row">
      <input class="input" name="main_name" value="${escape(m.name)}" list="main-rooms" placeholder="名字：打几个字从下拉里选" aria-label="主舞台第 ${i + 1} 路的名字" maxlength="40" autocomplete="off">
      <select class="input" name="main_platform" aria-label="主舞台第 ${i + 1} 路的平台"><option value="">平台</option>${platformOptions(m.platform)}</select>
      <input class="input" name="main_room" value="${escape(m.room)}" placeholder="房间号" aria-label="主舞台第 ${i + 1} 路的房间号" maxlength="24" autocomplete="off" autocapitalize="off" spellcheck="false"></div>`;
  return `<fieldset><legend>主舞台（可以空着）</legend>
    <span class="help">官方直播、解说、观战的主播，Evo 里放在队伍栏最上面，照这里的顺序。名字那一格打几个字，从下拉里选填过的直播间或选手库里的人，平台和房间号会自动填好；
      新的直播间三格都填。最多 ${MAIN_MAX} 路（0.7.0 以前的 Evo 只显示前 8 路，官方直播、解说放前面）；第一路在播的话，Evo 默认先看它。空着的行不用管</span>
    ${rows.map(row).join("")}</fieldset>`;
}

// 主舞台名字的下拉：记下的直播间 + 选手库里开直播的人。data-p、data-r 给 /app.js 填平台和房间号
function mainRoomList(choices) {
  return `<datalist id="main-rooms">${choices.map((c) => `<option value="${escape(c.name)}" label="${escape(`${PLATFORMS[c.platform]?.name ?? c.platform} ${c.room}${c.from === "player" ? ` · 选手库（${c.team}）` : ""}`)}"
    data-p="${escape(c.platform)}" data-r="${escape(c.room)}"></option>`).join("")}</datalist>`;
}

// 记下的直播间（11-21）：列出来，填错的、不用了的可以勾掉
function roomsFold(rooms, editable) {
  if (!rooms.length) return "";
  const item = (r) => `<li class="row"><label class="pick-row">${editable ? `<input type="checkbox" name="forget" value="${escape(r.key)}">` : ""}
    <span><b>${escape(r.name)}</b> <span class="muted small">${escape(PLATFORMS[r.platform]?.name ?? r.platform)} ${escape(r.room)}</span></span></label></li>`;
  return `<details class="card fold" id="rooms"><summary class="fold-head"><span>记下的直播间（${rooms.length}）</span><span class="muted small">填主舞台时从下拉里选</span></summary>
    <form class="stack" method="post" action="/events/rooms/forget">
      <p class="small muted">填过的主舞台直播间都记在这里，名字用最近一次填的；选手库里开直播的人不用记，下拉里本来就有。填错的、不用了的勾上点"忘掉"，赛程里已经填着的不动。</p>
      <ul class="list">${rooms.map(item).join("")}</ul>
      ${editable ? '<div><button class="btn" type="submit">忘掉勾选的</button></div>' : ""}</form></details>`;
}

function eventForm(action, e, teams, submit) {
  const typeOptions = Object.keys(TYPES).map((t) => `<label class="option option-inline"><input type="radio" name="type" value="${t}"${e.type === t ? " checked" : ""} required><b>${t}</b></label>`).join("");
  const chosen = new Set(e.teams ?? []);
  const missing = [...chosen].filter((t) => !teams.has(t));  // 选手库里已经没有的（删了、改名了）也列出来，免得悄悄丢掉
  const boxes = [...teams.entries(), ...missing.map((t) => [t, 0])].map(([team, n]) => `<label class="pick"><input type="checkbox" name="teams" value="${escape(team)}"${chosen.has(team) ? " checked" : ""}>
      <span>${escape(team)}</span><span class="${n ? "muted" : "warn"} small">${n ? `${n} 人` : "不在选手库里"}</span></label>`).join("");
  return `<form class="stack" method="post" action="${action}">
    ${e.rev ? `<input type="hidden" name="rev" value="${e.rev}">` : ""}
    <label class="field">名称<input class="input" name="name" value="${escape(e.name)}" maxlength="80" required autocomplete="off"></label>
    <fieldset><legend>类型</legend><div class="inline-options">${typeOptions}</div></fieldset>
    <label class="field">阶段<span class="help">可以空着，比如 B 组对 C 组</span>
      <input class="input" name="stage" value="${escape(e.stage)}" maxlength="80" autocomplete="off"></label>
    <div class="two">
      <label class="field">开始（北京时间）<input class="input" type="datetime-local" name="start" value="${datetimeValue(e.start)}" required></label>
      <label class="field">结束（北京时间）<input class="input" type="datetime-local" name="end" value="${datetimeValue(e.end)}" required></label>
    </div>
    ${mainFields(e.main ?? [])}
    <fieldset><legend>参赛队伍</legend><span class="help">从选手库里勾；一般是 20 支</span>
      ${teams.size || missing.length ? `<div class="picks">${boxes}</div>` : `<p class="small warn">选手库里还没有队伍，先去加选手。</p>`}</fieldset>
    <div><button class="btn btn-primary" type="submit">${submit}</button></div>
  </form>`;
}

// copy：点了某一场的"复制"（2026-11-19 用户要的：A 组、B 组，每天的训练赛，队伍和主舞台不用再录一遍），添加表单照它填好
export function eventsPage(me, items, playerItems, { now, error = "", ok = "", draft = null, draftTable = "", banner = "", copy = null, rooms = [] }) {
  const editable = can(me, "edit");
  const teams = teamsOf(playerItems);
  const groups = { live: [], upcoming: [], ended: [] };
  for (const e of items) groups[status(e, now)].push(e);
  groups.live.sort((a, b) => parseIso(a.start) - parseIso(b.start));
  groups.upcoming.sort((a, b) => parseIso(a.start) - parseIso(b.start));
  groups.ended.sort((a, b) => parseIso(b.end) - parseIso(a.end));
  const row = (e) => {
    const old = now - parseIso(e.end) > 7 * 24 * 3600 * 1000;
    return `<li class="row" id="e-${e.id}">
    <div class="row-main">
      <div class="row-name"><span class="badge badge-${status(e, now)}">${STATUS_WORDS[status(e, now)]}</span>${escape(e.name)}</div>
      <div class="row-meta"><span>${escape(e.type)}${e.stage ? ` · ${escape(e.stage)}` : ""}</span>
        <span>${escape(beijingText(parseIso(e.start)))}–${escape(beijingText(parseIso(e.end)).slice(11))}</span>
        <span>${e.teams.length} 支队</span>${e.main?.length ? `<span>主舞台 ${e.main.length} 路</span>` : ""}
        ${old ? '<span class="badge">结束超过 7 天，不会发布</span>' : ""}<span>${by(e, now)}</span></div>
    </div>
    ${editable ? `<div class="row-actions">
      <details class="act"><summary class="btn">改</summary><div class="panel panel-wide">${eventForm(`/events/${e.id}/save`, e, teams, "保存")}</div></details>
      <a class="btn" href="/events?copy=${e.id}#add" title="照这一场填好添加表单（队伍、主舞台都带上）">复制</a>
      ${confirmDelete(`/events/${e.id}/delete`, e.name)}</div>` : ""}
  </li>`;
  };
  const section = (key, title) => (groups[key].length ? `<section class="card"><h2>${title}（${groups[key].length}）</h2>
    <ul class="list">${groups[key].map(row).join("")}</ul></section>` : "");
  const blank = { name: "", type: "联赛", stage: "", start: "", end: "", teams: [] };
  // 添加出错时留着填的内容（时间是网页上的 2026-11-17T19:00，换成文件里的写法再显示）
  const kept = draft ? { ...blank, ...draft, rev: undefined, start: asIso(draft.start), end: asIso(draft.end),
                          main: (draft.main ?? []).filter((m) => m.platform || m.room || m.name).slice(0, MAIN_MAX) }
    : copy ? { name: copy.name, type: copy.type, stage: copy.stage, start: copy.start, end: copy.end, teams: [...copy.teams], main: [...(copy.main ?? [])] }
      : blank;
  const copied = copy && !draft ? `<p class="note note-info">照${escape(eventLabel(copy))}填好了（队伍、主舞台都带上了）：改一下阶段、时间，再点下面的"添加"。</p>` : "";
  const add = editable ? `<details class="card fold" id="add"${!items.length || draft || copy ? " open" : ""}><summary class="fold-head"><span>添加赛事</span><span class="muted small">联赛、训练赛、杯赛</span></summary>
    <div class="stack">${copied}${eventForm("/events/add", kept, teams, "添加")}</div></details>` : "";
  return layout({ title: "赛程", me, current: "events", banner, body: `
<div class="stack"><h1>赛程</h1>
  <p class="muted">时间都是北京时间。进行中、即将开始的会出现在 Evo 的赛事页；结束超过 7 天的不会发布。</p></div>
${notes({ error, ok })}
${editable ? mainRoomList(mainChoices(rooms, playerItems)) : ""}
${add}
${section("live", "进行中")}${section("upcoming", "即将开始")}${section("ended", "已结束")}
${roomsFold(rooms, editable)}
${items.length ? "" : `<p class="muted">还没有赛事。</p>`}
${editable ? importBox("/events/import", [...EVENT_COLUMNS, "主舞台（可以没有：平台 房间号 名字，多路用顿号隔开）"], "亚太北区职业联赛 第 3 周\t联赛\tB 组对 C 组\t2026-11-17 19:00\t2026-11-17 23:00\t星火电竞、北辰\tB站 7734200 官方直播", draftTable) : ""}` });
}

// ---------- 发布 ----------

function fileSummary(info) {
  if (!info) return `<p class="muted">线上还没有这个文件。</p>`;
  return `<p>${escape(info.summary)}${info.logos ? `，${info.logos} 支队有队徽` : ""}</p><p class="small muted">文件里写的更新时间：${escape(info.updated || "没有")}`
    + `${info.platformsOff.length ? `；关掉的平台：${escape(info.platformsOff.join("、"))}` : "；五个平台都开着"}</p>`;
}

function eventList(info) {
  if (!info?.events.length) return "";
  return `<ul class="list compact">${info.events.map((e) => `<li class="row"><div class="row-main">
    <div class="row-name"><span class="badge">${escape(e.status)}</span>${escape(e.text)}</div>
    <div class="row-meta"><span>${escape(e.when)}</span><span>${e.teams} 支队、${e.players} 人</span></div></div></li>`).join("")}</ul>`;
}

export function publishPage(me, view, last, { now, error = "", ok = "", banner = "" }) {
  const result = view.built.official;
  const allowed = can(me, "publish");
  const blocked = result.errors.length > 0;
  const check = blocked ? noteList("error", `还不能发布，有 ${result.errors.length} 处要改：`, result.errors)
    : `<p class="note note-ok">检查过了，可以发布。</p>`;
  const reminders = noteList("info", "提醒（不改也能发布）：", result.notes, 3);
  const target = (key) => {
    const t = TARGETS[key];
    const live = describe(view.live[key].data, now);
    const record = last[key];
    const button = !allowed ? `<p class="small muted">发布要找管理员（或者请管理员给你开发布）。</p>`
      : blocked ? `<button class="btn" type="button" disabled>先把上面的问题改好</button>`
      : key === "test" ? `<form method="post" action="/publish/test"><button class="btn btn-primary" type="submit">发布到测试文件</button></form>`
      : `<details class="act"><summary class="btn btn-primary">发布到正式文件</summary><div class="panel">
          <p class="small">所有玩家打开 Evo 的赛事页，看到的就会变成"要发布的内容"那样。先发过测试文件、核对过了吗？</p>
          <form method="post" action="/publish/official"><button class="btn btn-primary btn-wide" type="submit">确定发布到正式文件</button></form></div></details>`;
    const switches = me.role === "admin" && view.live[key].data ? `<div class="switches"><span class="small muted">平台开关：</span>${Object.entries(PLATFORMS).map(([pk, p]) => {
      const on = view.live[key].data.platforms[pk];
      return `<details class="act"><summary class="btn btn-small ${on ? "" : "btn-danger"}">${p.name}：${on ? "开" : "关"}</summary><div class="panel">
        <p class="small">${on ? `关掉以后，Evo 不在窗口里播${p.name}，改成"去${p.name}看"。平台出问题、播不了的时候用，不用发新版本。` : `打开以后，Evo 又在窗口里播${p.name}。`}</p>
        <form method="post" action="/publish/platform"><input type="hidden" name="target" value="${key}"><input type="hidden" name="platform" value="${pk}">
          <input type="hidden" name="on" value="${on ? "0" : "1"}"><button class="btn ${on ? "btn-danger" : "btn-primary"} btn-wide" type="submit">确定${on ? "关掉" : "打开"}${p.name}</button></form></div></details>`;
    }).join("")}</div>` : "";
    return `<section class="card stack">
      <div class="title-row"><h2>${t.name}</h2><a class="small" href="${PUBLIC_URL}/${t.file}" target="_blank" rel="noopener noreferrer">${t.file}</a></div>
      <p class="small muted">${key === "test" ? "玩家的 Evo 不读它：先发到这里，点上面的文件名看一眼，没问题再发正式文件。" : "玩家的 Evo 赛事页读的就是它。"}
        ${record ? `上次是 ${escape(record.by)} ${escape(ago(record.at, now))}发的（${record.events} 场赛事、${record.players} 位选手）。` : "这个网页还没发过。"}</p>
      <h3>线上现在</h3>${fileSummary(live)}
      ${switches}
      ${button}
    </section>`;
  };
  const coming = describe(result.data, now);
  return layout({ title: "发布", me, current: "publish", banner, body: `
<div class="stack"><h1>发布到 Evo</h1>
  <p class="muted">把选手库和赛程生成 Evo 赛事页读的文件（放在 dl.evotracker.cn 上）。先发测试文件，核对没问题再发正式文件。</p></div>
${notes({ error, ok })}
${check}${reminders}
<section class="card stack"><h2>要发布的内容</h2>${blocked ? `<p class="small muted">（有问题的地方改好以后才能发；下面是现在能生成的样子）</p>` : ""}
  ${fileSummary(coming)}${eventList(coming)}</section>
${target("test")}${target("official")}` });
}

// ---------- 改动记录 ----------

export function logPage(me, items, now) {
  return layout({ title: "改动记录", me, current: "", body: `
<div class="stack"><h1>改动记录</h1><p class="muted">最近 ${items.length} 条（最多留 300 条），新的在前。时间是北京时间。</p></div>
<section class="card">${logList(items, now)}</section>` });
}
