"""大厅校准：用前后两次大厅读到的总分，核对中间各局的 RP、补上中间各局的段位（2026-10-18 用户决定：总分以大厅为准，是最高优先级）。

前后两次大厅读数之间的排位局，RP 加起来应该等于两次总分之差（总分 = 段位换算成的连续分数，见 ranks.score）：
- 对得上：没问题
- 只缺一局的 RP（比如没进经验值页、成盒后提前回了大厅）：用差值补上，来源记"大厅校准"。
  先按最近的参照点算（fix_rps，2026-11-13 用户同意）：中间几局读到了经验值页的段位，就等于知道了那几局结束时的总分，
  两次大厅之间分成好几小段；缺 RP 的那局只用它所在那一小段的前后总分算，别的小段里读错的 RP 不会算到它头上。
  大厅的总分不变，只是差值分得更准：2026-11-04 用户本机一局按两次大厅之差补成 -62，按前后经验值页的段位是 -38，
  差的 24 多半来自另一局对局中读的 +24（按段位算是 0），改过来以后那次回大厅显示一行校准分数 -24。
  这样也能补上两次大厅之间缺好几局 RP、但每一小段只缺一局的。以前按两次大厅之差补的，和按小段算的不一样时改过来；
  前后跨了大段（升段奖励、降段保护）、模式没读出的局不补；用户在战绩页改过 RP 的局不动。
  小段算不出时，照旧看两次大厅：整段只缺一局、没跨大段，就用差值补
- 对不上（某局的 RP 读错了、缺两局以上、跨大段的升段奖励和降段保护、没开 Evo 时打过、别人用这个号打过）：
  不标待确认，战绩页在那次回大厅的位置显示一行"校准分数"（总分之差 − 中间各局 RP 之和，见 adjustments）。
  2026-11-11 用户决定：以前对不上就把中间各局全标待确认，打完直接重新匹配的玩家两次大厅之间常隔十几局，
  一局的 RP 读错（比如右上角的 -38 赛后被免扣）整页都标黄。校准行不存进数据库，打开页面时现算：
  玩家在战绩页把某局的 RP 改对，校准行就跟着变小或消失。总览页的 RP 合计、场均 RP 不算校准行（用户决定）
- 两次大厅中间换了赛季（config/seasons.json，段位重置）：只写日志，也不显示校准行
只看同一个玩家名的局（多账号），还不知道玩家名的局也算进去。

补段位（2026-11-11 用户定的规则）：一局的段位本来只有经验值页一个来源，玩家常常不看经验值页、在总结页直接排下一局，
连打几局才回一次大厅，约一半排位局没有段位（见 docs/识别经验.md"约一半排位局没有段位"）。
- 大厅之前最近的那一局没有段位（没进经验值页）：它结束后的段位就是这次大厅读到的，直接补上（匹配等不算排位的局不补；
  那一局结束一小时以后才读到的大厅、那一局是上个赛季的都不补，见 FILL_WITHIN）
- 其余的（fill_ranks）：参照点是大厅读数（一次回大厅读到几次的，下一段从最后一次算起）和段位完整的局（经验值页读到的）。
  两个参照点之间没有段位的排位局，用各局 RP 从前往后一路加、从后往前一路减，算出每局结束后的总分，换算成段位（ranks.rank_at）
- 前后两个参照点是同一个大段：中间的局都是这个大段，大段一定补。段和段内分数：RP 都有、加起来对得上就补；
  只缺一局的 RP，用前后之差推出它（在 RP_RANGE 里才算）再补；缺两局以上，从两头各推到缺 RP 的那局为止。
  RP 对不上、算不到、算出来出了这个大段的，只补大段
- 跨了大段（可能有升段奖励、降段保护）：只在 RP 都有、加起来正好对得上时补
- 不补：两个参照点中间换了赛季、只有一头有参照点的、匹配等不算排位的局。模式没读出、又没有 RP 的局可能是排位，
  不补它，RP 当作不知道（推到它这里就断了）。大师、猎杀者不分段（分数的写法没核实）：前后一样时只补大段
- "大厅校准"补出来的 RP 不当作读到的：它是用前后之差凑出来的，可能把别的局读错的部分吸进去（见上面 -62 那局）；
  用户在战绩页改过的 RP 当作读到的
- 只补空着的项，经验值页读到的段位不改（读到了段、没读到段内分数的：算出来的段一样才补分数）。补上的在 readings 表
  记一行（来源"大厅校准"），推出来的局不当参照点
- 每次回大厅补这个账号的（calibrate）；开始记录、打开面板时补所有账号以前的局（匿名统计、小程序同步看到这一局变了会重发）
"""

from bisect import bisect_left, bisect_right
from collections import namedtuple
from datetime import datetime, timedelta

import ranks
import seasons
import storage
from post_match import SOURCE_LOBBY, _split_notes, settle

RP_RANGE = (-200, 600)  # 补出来的 RP 不在这个范围里，说明中间还有别的变化（掉线惩罚、赛季重置……），不补
OLD_NOTE = "大厅校准："  # 以前对不上时写进各局备注的开头（见 clear_old_flags）
# 一局结束多久以内读到的大厅，才把段位补给那一局。打完回大厅一般只要几分钟；隔得久了中间可能换了赛季（段位重置）、
# 在别处打过：2026-11-03 第 31 赛季开始时，上一局是 10-27 没有段位的钻石局，会被补成新赛季的白银 I
FILL_WITHIN = timedelta(hours=1)
STAMP = "%Y-%m-%d %H:%M:%S"  # 数据库里时间的写法
RANK_FIELDS = ("rank_tier", "rank_division", "rank_progress")
# 补段位的参照点：time 时间；score 总分；tier 大段；match 是一局时为那一局（大厅读数为 None）
Point = namedtuple("Point", "time score tier match")


def _same_player(row_name, name):
    return name is None or row_name is None or storage.name_key(row_name) == storage.name_key(name)


def calibrate(conn, reading, log=print):
    """reading：刚记下的大厅读数（read_at、player_name、rank_tier、rank_division、rank_progress、score）。
    先核对 RP，再补这个账号没有段位的局。"""
    _check_rp(conn, reading, log)
    for _, _, line in fill_ranks(conn, reading["player_name"]):
        log(f"           {line}")


def _check_rp(conn, reading, log):
    name = reading["player_name"]
    previous = [r for r in storage.lobby_readings(conn) if r["read_at"] < reading["read_at"]
                and _same_player(r["player_name"], name)]

    def games_before():
        return [m for m in conn.execute("SELECT * FROM matches WHERE ended_at <= ? ORDER BY ended_at, match_id",
                                        (reading["read_at"],)) if _same_player(m["player_name"], name)]

    _fill_last_rank(conn, reading, games_before(), previous, log)
    for line in fix_rps(conn, name):
        log(f"           {line}")
    games = games_before()  # 上面可能补了、改了 RP
    if not previous:
        log(f"           大厅校准：这是第一次读到大厅的段位（总分 {reading['score']}），以后从这里开始对账")
        return
    prev = previous[-1]
    ranked, known, missing = _window([m for m in games if m["ended_at"] > prev["read_at"]])
    diff = reading["score"] - prev["score"]
    head = f"           大厅校准：上次大厅总分 {prev['score']}，这次 {reading['score']}（{diff:+d}）"
    if seasons.of(prev["read_at"]) != seasons.of(reading["read_at"]):
        log(f"{head}，中间换了赛季（段位重置），不核对")
        return
    if not ranked:
        if diff:
            log(f"{head}，但中间没有记下排位局（可能 Evo 没开的时候打过）：战绩页显示校准分数 {diff:+d}")
        return
    ids = "、".join(str(m["match_id"]) for m in ranked)
    rest = diff - known
    cross = prev["rank_tier"] != reading["rank_tier"]
    if not cross and len(missing) == 1 and RP_RANGE[0] <= rest <= RP_RANGE[1]:
        m = missing[0]
        storage.save_readings(conn, m["match_id"], SOURCE_LOBBY, {"rp_delta": rest}, reading["read_at"])
        settle(conn, m["match_id"], log)
        log(f"{head}，编号 {m['match_id']} 没有 RP，用差值补上 {rest:+d}")
        return
    sums = f"中间 {len(ranked)} 局（编号 {ids}）RP 加起来 {known:+d}" + \
        (f"，其中 {len(missing)} 局没读到 RP" if missing else "")
    if not rest:
        log(f"{head}，{sums}，对上了")
        return
    why = f"；跨了大段（{prev['rank_tier']} → {reading['rank_tier']}，可能有升段奖励或降段保护）" if cross else ""
    log(f"{head}，{sums}{why}，对不上：战绩页显示校准分数 {rest:+d}，不标待确认")


def _window(between):
    """两次大厅之间的局里，算排位的局（模式是排位，或者有 RP）、读到的 RP 之和、没读到 RP 的局。"""
    ranked = [m for m in between if m["mode"] == "ranked" or m["rp_delta"] is not None]
    known = sum(m["rp_delta"] for m in ranked if m["rp_delta"] is not None)
    return ranked, known, [m for m in ranked if m["rp_delta"] is None]


def adjustments(lobby, games):
    """战绩页的"校准分数"行：前后两次大厅的总分之差，减去中间各局读到的 RP 之和，不是 0 的每次回大厅一行。
    lobby：大厅读数（storage.lobby_readings，所有账号）；games：对局（storage.all_matches，所有账号），都按时间从早到晚。
    规则和 calibrate 一样（同一个玩家名，还不知道玩家名的局也算；换了赛季的那一次不算）。
    返回 [{read_at, player_name, before, after（前后两次大厅读数）, games（中间的排位局数）, missing（其中没读到 RP 的）,
    known（读到的 RP 之和）, delta（校准分数）}]，按时间从早到晚。"""
    ends = [m["ended_at"] or "" for m in games]
    out = []
    for i, now in enumerate(lobby):
        if now["score"] is None:
            continue
        prev = next((lobby[j] for j in range(i - 1, -1, -1)
                     if lobby[j]["score"] is not None and lobby[j]["read_at"] < now["read_at"]
                     and _same_player(lobby[j]["player_name"], now["player_name"])), None)
        if prev is None or seasons.of(prev["read_at"]) != seasons.of(now["read_at"]):
            continue
        between = games[bisect_right(ends, prev["read_at"]):bisect_right(ends, now["read_at"])]
        ranked, known, missing = _window([m for m in between if _same_player(m["player_name"], now["player_name"])])
        delta = now["score"] - prev["score"] - known
        if delta:
            out.append({"read_at": now["read_at"], "player_name": now["player_name"], "before": prev, "after": now,
                        "games": len(ranked), "missing": len(missing), "known": known, "delta": delta})
    return out


def clear_old_flags(conn):
    """以前大厅校准对不上时，把中间各局标了待确认、备注开头写"大厅校准："（2026-11-11 起改成显示校准行，见文件开头）。
    还没确认的局去掉这条备注；没有别的问题了就不再待确认。确认过的局不动（备注留着当初为什么待确认）。
    每次打开面板、开始记录时调用一次，没有这样的局时什么都不做。返回改了几局。"""
    rows = conn.execute("SELECT match_id, review_note FROM matches WHERE needs_review = 1 AND review_note LIKE ?",
                        (f"%{OLD_NOTE}%",)).fetchall()
    for row in rows:
        problems, info = _split_notes(row["review_note"])
        problems = [p for p in problems if not p.startswith(OLD_NOTE)]
        storage.update_match(conn, row["match_id"], {"review_note": "；".join(problems + info) or None,
                                                     "needs_review": int(bool(problems))})
    return len(rows)


def _fill_last_rank(conn, reading, games, previous, log):
    """大厅之前最近的那一局没有段位：它结束后的段位就是这次读到的（中间没有别的局）。"""
    if not games:
        return
    last = games[-1]
    if last["rank_tier"] is not None or last["mode"] in ("pubs", "other"):
        return  # 已经有段位；或者是匹配等不算排位的局：段位不会变，不补
    if previous and previous[-1]["read_at"] >= last["ended_at"]:
        return  # 这一局之后已经回过大厅了，那次才是它结束后的段位
    if seasons.of(last["ended_at"]) != seasons.of(reading["read_at"]):
        log(f"           编号 {last['match_id']} 没有段位：是上个赛季的局，不补这个赛季读到的段位")
        return
    if datetime.strptime(reading["read_at"], STAMP) - datetime.strptime(last["ended_at"], STAMP) > FILL_WITHIN:
        log(f"           编号 {last['match_id']} 没有段位：结束一小时以后才回到大厅（中间可能换了赛季），不补")
        return
    fields = {k: reading[k] for k in RANK_FIELDS}
    storage.update_match(conn, last["match_id"], fields)
    storage.save_readings(conn, last["match_id"], SOURCE_LOBBY, fields, reading["read_at"])
    log(f"           编号 {last['match_id']} 没有段位：补上这次大厅读到的 "
        f"{ranks.rank_name(reading['rank_tier'], reading['rank_division'])} {reading['rank_progress']}")


# ---------- 按最近的参照点补 RP、补段位（规则见文件开头） ----------

def fix_rps(conn, name=None):
    """每一小段（两个参照点之间）只缺一局 RP 时，用前后的总分推出它：补上没有 RP 的局，以前按两次大厅之差补的不一样时改过来。
    name：只看这个玩家名的（回大厅时）；None = 所有账号（补以前的局）。返回日志，每改一局一行。"""
    out = []
    for a, gap, b in _all_segments(conn, name):
        got = _missing_rp(a, gap, b)
        if got is None:
            continue
        m, rp = got
        row = storage.get_match(conn, m["match_id"])  # 读最新的（没有玩家名的局几个账号都会看到）
        edited = _edited(row)
        if row["rp_delta"] == rp or "rp_delta" in edited:
            continue  # 已经是这个数；用户改过的不动
        if row["rp_delta"] is not None and row["rp_source"] != SOURCE_LOBBY:
            continue  # 经验值页、对局中读到的不改
        storage.save_readings(conn, row["match_id"], SOURCE_LOBBY, {"rp_delta": rp}, b.time)
        if edited:  # 用户改过别的项：只改 RP，不按各来源重新定（settle 会把用户改的数换回读到的）
            storage.update_match(conn, row["match_id"], {"rp_delta": rp, "rp_source": SOURCE_LOBBY})
        else:
            settle(conn, row["match_id"], log=lambda _: None)
        if row["rp_delta"] is None:
            out.append(f"编号 {row['match_id']} 没有 RP：按前后的段位和各局 RP 补上 {rp:+d}")
        else:
            out.append(f"编号 {row['match_id']} 的 RP 按前后的段位改成 {rp:+d}（以前按两次大厅之差补的是 "
                       f"{row['rp_delta']:+d}，差的部分在这一段以外的局上）")
    return out


def _missing_rp(a, gap, b):
    """a、b 之间（同一个赛季、同一个分段的大段）只缺一局的 RP：(那一局, 前后总分之差减去其他各局 RP)；
    缺的不是一局、跨了大段、缺的那局可能不是排位（模式没读出）、算出来不在 RP_RANGE 里都返回 None。"""
    if seasons.of(a.time) != seasons.of(b.time) or a.tier != b.tier or not ranks.has_divisions(a.tier):
        return None
    chain = gap + ([b.match] if b.match is not None else [])  # b 是一局时，它的 RP 也在 a、b 之间
    rps = [_rp(m) for m in chain]
    missing = [m for m, rp in zip(chain, rps) if rp is None]
    if len(missing) != 1 or not _ranked(missing[0]):
        return None
    rp = b.score - a.score - sum(r for r in rps if r is not None)
    return (missing[0], rp) if RP_RANGE[0] <= rp <= RP_RANGE[1] else None


def fill_ranks(conn, name=None):
    """用前后两个参照点和各局 RP 补中间各局的段位。name：只补这个玩家名的（回大厅时）；None = 所有账号（补以前的局）。
    返回补了的局：[(编号, 补全了没有（False = 只补了大段）, 日志)]，按时间从早到晚。"""
    out = []
    for before, gap, after in _all_segments(conn, name):
        out += _fill_segment(conn, before, gap, after)
    return out


def _all_segments(conn, name=None):
    """这个玩家名（None = 每个账号各算各的）的 [(前一个参照点, 中间的局, 后一个参照点)]，见 _segments。"""
    lobby = [r for r in storage.lobby_readings(conn) if r["score"] is not None]
    games = [m for m in conn.execute("SELECT * FROM matches WHERE ended_at IS NOT NULL ORDER BY ended_at, match_id")
             if m["mode"] not in ("pubs", "other")]
    inferred = {r[0] for r in conn.execute("SELECT DISTINCT match_id FROM readings WHERE source = ? AND field IN (?, ?, ?)",
                                           (SOURCE_LOBBY, *RANK_FIELDS))}
    if name is not None:
        names = [name]
    else:
        names = sorted({storage.name_key(r["player_name"]) for r in (*lobby, *games) if r["player_name"]}) or [None]
    out = []
    for who in names:
        events = sorted([(m["ended_at"], 0, m) for m in games if _same_player(m["player_name"], who)]
                        + [(r["read_at"], 1, r) for r in lobby if _same_player(r["player_name"], who)],
                        key=lambda e: e[:2])  # 同一秒的局算在大厅之前（和 calibrate 一样）
        out += _segments(events, inferred)
    return out


def _score(row):
    """一局段位完整时的总分，否则 None。"""
    return ranks.score(row["rank_tier"], row["rank_division"], row["rank_progress"])


def _edited(row):
    """用户在战绩页改过哪几项。"""
    return {f for f in (row["edited"] or "").split(",") if f}


def _ranked(row):
    """算排位的局：模式是排位，或者有 RP（模式没读出、又没有 RP 的可能是匹配）。"""
    return row["mode"] == "ranked" or row["rp_delta"] is not None


def _rp(row):
    """推段位、推 RP 时用的 RP：大厅校准补出来的不算（见文件开头），用户在战绩页改过的算；没有为 None。"""
    if row["rp_delta"] is None or (row["rp_source"] == SOURCE_LOBBY and "rp_delta" not in _edited(row)):
        return None
    return row["rp_delta"]


def _segments(events, inferred):
    """按时间排好的 [(时间, 0 局 / 1 大厅读数, 那一行)] → [(前一个参照点, 中间的局, 后一个参照点)]，只要中间有局的。
    inferred：段位是推出来的局（不当参照点）。"""
    out, prev, gap = [], None, []
    for _, kind, row in events:
        if kind == 1:
            point = Point(row["read_at"], row["score"], row["rank_tier"], None)
            if prev is not None and prev.match is None and not gap:
                prev = point  # 同一次回大厅读到好几次：下一段从最后一次算起
                continue
        elif row["match_id"] not in inferred and _score(row) is not None:
            point = Point(row["ended_at"], _score(row), row["rank_tier"], row)
        else:
            gap.append(row)
            continue
        if prev is not None and gap:
            out.append((prev, gap, point))
        prev, gap = point, []
    return out


def _fill_segment(conn, a, gap, b):
    """补参照点 a、b 之间没有段位的排位局。"""
    todo = [m for m in gap if _ranked(m) and _score(m) is None]
    if not todo or seasons.of(a.time) != seasons.of(b.time):
        return []
    guesses = _guess(a, gap, b)
    out = []
    for m in todo:
        rank, why = guesses[m["match_id"]]
        done = _apply(conn, m, rank, why, b.time)
        if done:
            out.append(done)
    return out


def _guess(a, gap, b):
    """中间每一局结束后的段位：{编号: ((段位, 段, 段内进度) / (段位, None, None) 只知道大段 / None 不补, 为什么)}。"""
    same = a.tier == b.tier
    if not (ranks.has_divisions(a.tier) and ranks.has_divisions(b.tier)):
        return {m["match_id"]: ((a.tier, None, None) if same else None, "大师、猎杀者不分段") for m in gap}
    chain = gap + ([b.match] if b.match is not None else [])  # b 是一局时，它的 RP 也在 a、b 之间
    rps = [_rp(m) for m in chain]
    last = len(chain) - 1
    ahead = [None] * len(chain)  # 每局结束后的总分：从 a 往后一路加
    total = a.score
    for i, rp in enumerate(rps):
        if rp is None:
            break
        total += rp
        ahead[i] = total
    behind = [None] * len(chain)  # 从 b 往前一路减
    behind[last] = b.score
    for i in range(last, 0, -1):
        if rps[i] is None:
            break
        behind[i - 1] = behind[i] - rps[i]
    missing = [i for i, rp in enumerate(rps) if rp is None]
    out = {}
    for i, m in enumerate(gap):
        total, why = None, None
        if not missing:
            diff = b.score - a.score - sum(rps)
            if diff:
                why = f"RP 对不上，差 {diff:+d}"
            else:
                total, why = ahead[i], "前后的段位和各局 RP 对得上"
        elif not same:
            why = f"跨了大段，缺 {len(missing)} 局 RP，没法核对"
        elif len(missing) == 1:
            j = missing[0]
            derived = behind[j] - (ahead[j - 1] if j else a.score)
            if RP_RANGE[0] <= derived <= RP_RANGE[1]:
                total = ahead[i] if ahead[i] is not None else behind[i]
                why = f"缺一局 RP，按前后之差是 {derived:+d}"
            else:
                why = f"缺一局 RP，按前后之差是 {derived:+d}，不合理"
        else:
            total = ahead[i] if ahead[i] is not None else behind[i]
            side = "从前面推" if ahead[i] is not None else "从后面推"
            why = f"缺 {len(missing)} 局 RP，" + (side if total is not None else "算不到")
        rank = ranks.rank_at(total) if total is not None else None
        if rank is not None and (not same or rank[0] == a.tier):
            out[m["match_id"]] = (rank, why)
        elif same:
            out[m["match_id"]] = ((a.tier, None, None), why if total is None else f"{why}，但算出来不在{a.tier}里")
        else:
            out[m["match_id"]] = (None, why)
    return out


def _apply(conn, m, rank, why, when):
    """把推出来的段位补进这一局的空项。返回 (编号, 补全了没有, 日志)；什么都没补返回 None。"""
    if rank is None:
        return None
    have = [m[k] for k in RANK_FIELDS]
    if any(old is not None and new is not None and old != new for old, new in zip(have, rank)):
        return None  # 经验值页读到的段和推出来的不一样：不改
    fields = {k: new for k, old, new in zip(RANK_FIELDS, have, rank) if old is None and new is not None}
    if not fields:
        return None
    storage.update_match(conn, m["match_id"], fields)
    storage.save_readings(conn, m["match_id"], SOURCE_LOBBY, fields, when)
    tier, division, progress = (fields.get(k, m[k]) for k in RANK_FIELDS)
    lack = "没有段位" if m["rank_tier"] is None else "没有段内分数"
    if progress is None:
        return m["match_id"], False, f"编号 {m['match_id']} {lack}：只补上大段 {tier}（{why}）"
    return m["match_id"], True, f"编号 {m['match_id']} {lack}：补上 {ranks.rank_name(tier, division)} {progress}（{why}）"
