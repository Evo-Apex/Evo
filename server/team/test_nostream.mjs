// 不直播的选手的测试（2026-11-18）。由 test.mjs 运行。和 tools/esports.py 逐项对照的在 tests/test_team_publish.py。
// 走一遍：表单里有"不直播"、房间号可以空着、写了房间号要空着、两个不直播的不算重复、照样列在队里、列表上标出来、
// 改成开直播 / 改成不直播、粘贴导入、发布时的编号（none-<选手名 sha256 前 12 位>）、Evo 读的时候不去掉、主舞台不能写不直播。

import { createHash } from "node:crypto";
import { hash, newToken, update } from "./auth.js";
import { PLAYERS } from "./data.js";
import { clean, sha256Hex } from "./esports.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
const now = Date.UTC(2026, 10, 18, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const players = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const sha12 = (text) => createHash("sha256").update(text, "utf8").digest("hex").slice(0, 12);

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
const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const add = (name, team, platform, room, liquipedia = "") => editor.post("/players/add", { name, team, platform, room, liquipedia });

check("sha256 和 Node 自带的一样（中文、正好 55 / 56 / 64 字节的边界）", ["甲二", "", "a".repeat(55), "b".repeat(56), "c".repeat(64), "表情😀".repeat(20)]
  .every((t) => sha256Hex(t) === createHash("sha256").update(t, "utf8").digest("hex")));

let r = await editor.get("/players");
check("平台多一个不直播、房间号不是必填", r.text.includes('<option value="none">不直播</option>') && !/name="room"[^>]*required/.test(r.text));
r = await add("甲一", "安静队", "douyu", "11");
r = await add("甲二", "安静队", "none", "");
check("不直播的可以加，房间号空着", r.location === "/players?ok=added" && players().find((p) => p.name === "甲二").platform === "none"
  && players().find((p) => p.name === "甲二").room === "");
r = await add("甲三", "安静队", "不直播", "", "Jia3");
check("两个不直播的不算重复（平台写中文也认）", r.location === "/players?ok=added" && players().length === 3);
r = await add("甲四", "别的队", "none", "123");
check("不直播还写了房间号 → 提示要空着", r.status === 400 && r.text.includes("不直播的选手「房间号」要空着（现在是「123」）"));
r = await add("甲四", "别的队", "快手", "1");
check("不认识的平台 → 提示里有不直播", r.status === 400 && r.text.includes("只能是 斗鱼 / 虎牙 / B站 / 抖音 / Twitch / 不直播"));
r = await editor.get("/players");
check("列表上标着不直播", r.text.includes('<span class="badge">不直播</span>') && !r.text.includes("安静队」有"));
check("改动记录写着不直播", env.DATA.json("log.json").items.some((x) => x.text === "添加选手 甲二（安静队，不直播）"));

// 改成开直播、再改回不直播
const jia2 = () => players().find((p) => p.name === "甲二");
r = await editor.post(`/players/${jia2().id}/save`, { name: "甲二", team: "安静队", platform: "bilibili", room: "22" });
check("改成开直播", jia2().platform === "bilibili" && jia2().room === "22"
  && env.DATA.json("log.json").items[0].text === "修改选手 甲二：平台 不直播 → B站，房间号 （空） → 22");
r = await editor.post(`/players/${jia2().id}/save`, { name: "甲二", team: "安静队", platform: "none", room: "" });
check("改回不直播", jia2().platform === "none" && jia2().room === "");

// 粘贴导入
r = await editor.post("/players/import", { table: "乙一\t吵闹队\t斗鱼\t31\t\n乙二\t吵闹队\t不直播\t\t\n乙三\t吵闹队\t不直播\t\tYi3" });
check("粘贴导入：平台写不直播、房间号空着", r.location === "/players?ok=imported&added=3&changed=0"
  && players().filter((p) => p.team === "吵闹队" && p.platform === "none").length === 2);
r = await editor.post("/players/import", { table: "丙一\t别的队\t不直播\t5" });
check("粘贴导入：不直播写了房间号 → 说第几行", r.status === 400 && r.text.includes("第 1 行：不直播的选手「房间号」要空着"));

// 主舞台不能写不直播
await editor.post("/events/add", [["name", "训练赛"], ["type", "训练赛"], ["stage", ""], ["start", "2026-11-18T19:00"], ["end", "2026-11-18T22:00"],
  ["teams", "安静队"], ["teams", "吵闹队"], ["main_platform", "none"], ["main_room", "1"], ["main_name", "官方"]]);
r = await editor.post("/events/import", { table: "杯赛\t杯赛\t\t2026-11-19 19:00\t2026-11-19 22:00\t安静队\t不直播 1 某某" });
check("主舞台写不直播 → 不认识", r.status === 400 && r.text.includes("主舞台的平台「不直播」不认识，只能写 斗鱼 / 虎牙 / B站 / 抖音 / Twitch"));
r = await editor.post("/events/add", [["name", "训练赛"], ["type", "训练赛"], ["stage", ""], ["start", "2026-11-18T19:00"], ["end", "2026-11-18T22:00"],
  ["teams", "安静队"], ["teams", "吵闹队"]]);

// 发布
r = await admin.get("/publish");
check("发布页按平台数人时写着不直播几个", r.text.includes("不直播 4"));
await admin.post("/publish/test");
const file = JSON.parse(env.PUBLIC.objects.get("esports_test.json").body);
const out = (name) => file.players.find((p) => p.name === name);
check("发布：不直播的编号是 none-<选手名 sha256 前 12 位>、房间号空着", JSON.stringify(out("甲二"))
  === JSON.stringify({ id: `none-${sha12("甲二")}`, name: "甲二", team: "安静队", platform: "none", room: "", liquipedia: "" }));
check("发布：开直播的照旧是平台-房间号、不直播的 Liquipedia 照样带上", out("甲一").id === "douyu-11" && out("甲三").liquipedia === "Jia3");
check("Evo 读的时候一个都不去掉", clean(file).players.length === 6);
check("Evo 读的时候：不直播还写了房间号的那一条去掉", clean({ ...file, players: [{ ...out("甲二"), room: "1" }] }).players.length === 0
  && clean({ ...file, players: [{ ...out("甲二"), room: undefined }] }).players.length === 0);
