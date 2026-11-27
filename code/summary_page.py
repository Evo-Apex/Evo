"""识别赛后总结页：从截图里读出名次、小队总击杀、击杀/助攻/击倒、伤害、生存时间、急救、重生。

识别规则（区域坐标、格式、合理范围）写在 config/summary.json 里，不写死在代码中。

单独试：python code/summary_page.py <截图路径>
例如：  python code/summary_page.py captures/samples/vlcsnap-2026-10-16-18h26m12s173.png
加上 --save-crops，会把裁出来的小块存到 captures/debug_crops/，可以看看 OCR 实际看到了什么。
"""

import argparse
import re
import time
from pathlib import Path

import cv2
import numpy as np

import game_lang
import modes
from ocr import SMALL_CROP_PARAMS, create_engine, crop, load_config, load_image
from paths import CAPTURES_DIR, data_relative
from process_info import memory_mb

CONFIG_NAME = "summary.json"
CROPS_DIR = CAPTURES_DIR / "debug_crops"
KEY_FIELDS = ("placement", "kda", "damage")  # 这几项有任何问题，整局标记"待确认"

STATUS_TEXT = {
    "ok": "正常",
    "low_score": "置信度低",
    "not_found": "没读到",
    "parse_fail": "格式不对",
    "out_of_range": "超出合理范围",
}


def clean(text, replace):
    """修正常见误读（比如字母 O 读成数字 0），去掉空格和千位逗号。"""
    for wrong, right in replace.items():
        text = text.replace(wrong, right)
    return text.replace(" ", "").replace(",", "")


def parse(expect, text):
    """把文字转成数据。格式不对时返回 None。"""
    if expect == "kda":
        # 必须正好是"数字/数字/数字"，少一个斜杠都不算（不去猜）
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{1,2})", text)
        return tuple(int(g) for g in m.groups()) if m else None
    if expect == "duration":
        m = re.search(r"(\d{1,2}):(\d{2})", text)
        return f"{m[1]}:{m[2]}" if m else None
    m = re.search(r"\d+", text)  # integer：取出其中的数字，比如"第6"取出 6
    return int(m[0]) if m else None


def in_range(value, bounds):
    if bounds is None:
        return True
    values = value if isinstance(value, tuple) else (value,)
    return all(bounds[0] <= v <= bounds[1] for v in values)


def same_piece(a, b, rule):
    """两小块是不是一样（数字停住了）：差别超过 rule["level"] 的点，占比不到 rule["share"]。"""
    return a.shape == b.shape and float(np.mean(cv2.absdiff(a, b) > rule["level"])) <= rule["share"]


def read_field(engine, img, name, spec, cfg, save_crops, memo=None):
    """识别一个数据项，返回结果字典。
    memo：同一次停在总结页时传同一个字典。这一小块和上次读的一样（见配置里的 unchanged），就沿用上次的结果，
    不再识别（结果里多一项 "reused"）；不一样才读，读完记进 memo。"""
    piece = crop(img, spec["roi"], cfg, spec.get("anchor"))
    if save_crops:
        cv2.imencode(".png", piece)[1].tofile(CROPS_DIR / f"{name}.png")
    before = memo.get(name) if memo is not None else None
    if before is not None and "unchanged" in cfg and same_piece(before[0], piece, cfg["unchanged"]):
        return {**before[1], "ms": 0.0, "reused": True}
    out = _read_piece(engine, piece, name, spec, cfg)
    if memo is not None:
        memo[name] = (piece, out)
    return out


def _read_piece(engine, piece, name, spec, cfg):
    start = time.perf_counter()
    result = engine(piece)
    ms = (time.perf_counter() - start) * 1000
    lines = list(zip(result.txts, result.scores)) if result.txts else []

    out = {"name": name, "label": spec["label"], "value": None, "raw": "", "score": None, "ms": ms}
    if not lines:
        out["status"] = "not_found"
        return out

    # 小块里可能读到不止一行（比如边上带进来半个标签），挑格式对得上、置信度最高的那行
    candidates = []
    for txt, score in lines:
        value = parse(spec["expect"], clean(txt, cfg["replace"]))
        if value is not None:
            candidates.append((score, txt, value))

    if not candidates:
        best_txt, best_score = max(lines, key=lambda t: t[1])
        out.update(raw=best_txt, score=best_score, status="parse_fail")
        return out

    score, txt, value = max(candidates)
    out.update(value=value, raw=txt, score=score)
    if not in_range(value, spec.get("range")):
        out["status"] = "out_of_range"
    elif score < cfg["min_score"]:
        out["status"] = "low_score"
    else:
        out["status"] = "ok"
    return out


def read_summary(engine, img, cfg, save_crops=False, memo=None):
    """memo：同一次停在总结页时传同一个字典，没变的项不再识别（见 read_field）。"""
    results = {name: read_field(engine, img, name, spec, cfg, save_crops, memo) for name, spec in cfg["fields"].items()}
    return results, summary_problems(results)


def summary_problems(results):
    """按各项的读数写出问题（有问题的局标记"待确认"）。没读全就切走、后来补全时，合并以后再算一遍（见 live_collect）。"""
    # 常识检查：自己的击杀数不可能超过小队总击杀
    problems = []
    kda, squad = results["kda"], results["squad_kills"]
    if kda["status"] == "ok" and squad["status"] == "ok" and kda["value"][0] > squad["value"]:
        problems.append(f"击杀数 {kda['value'][0]} 大于小队总击杀 {squad['value']}")

    for name in KEY_FIELDS:
        r = results[name]
        if r["status"] != "ok":
            problems.append(f"{r['label']}：{STATUS_TEXT[r['status']]}（OCR 原文：{r['raw'] or '无'}）")
    return problems


def is_champion(engine, img, cfg, memo=None):
    """总结页顶部的大字是不是"捍卫者"（拿冠军；被淘汰时是"全军覆没"）。配置里可以写几种（别的语言还没核实时）。
    memo：和 read_field 的一样，这一块没变就沿用上次的结果。"""
    banner = cfg["banner"]
    names = [banner["champion"]] if isinstance(banner["champion"], str) else banner["champion"]
    piece = crop(img, banner["roi"], cfg, banner.get("anchor"))
    before = memo.get("banner") if memo is not None else None
    if before is not None and "unchanged" in cfg and same_piece(before[0], piece, cfg["unchanged"]):
        return before[1]
    result = engine(piece)
    found = any(name in text for text in result.txts or () for name in names)
    if memo is not None:
        memo["banner"] = (piece, found)
    return found


def detect_mode(engine, img, cfg, memo=None):
    """总结页最后两行是不是某个轮换玩法自己的标签（config/summary.json 里每个玩法一段，写法在语言配置里）：
    两行都对得上才算，返回那一份的模式值（如 "wildcard"）；都不是返回 None。
    每个玩法配了几种写法就认几种（配置里是列表，引擎偶尔读成简体也算）；只对上一行不算（别把普通大逃杀的
    总结页认成轮换玩法）。没配标签的玩法（还没实拍核对）直接跳过，不跑识别。
    memo：和 read_field 的一样，没变就不重读。"""
    for key, mode in modes.summary_modes().items():
        rule = cfg.get(key)
        if not rule:
            continue
        rows = [(field, names) for field, names in rule.get("labels", {}).items() if names]
        if not rows:
            continue
        found = []
        for field, names in rows:
            roi = rule["roi"][field]
            piece = crop(img, roi, cfg, "center")
            memo_key = f"{key}_{field}"
            before = memo.get(memo_key) if memo is not None else None
            if before is not None and "unchanged" in cfg and same_piece(before[0], piece, cfg["unchanged"]):
                found.append(before[1])
                continue
            result = engine(piece)
            text = "".join(result.txts or ())
            names = names if isinstance(names, list) else [names]
            hit = any(name in text for name in names)
            if memo is not None:
                memo[memo_key] = (piece, hit)
            found.append(hit)
        if all(found):
            return mode
    return None


def skip_fields(cfg, mode=None):
    """这个玩法的总结页上不写进普通字段的项（config/summary.json 的 <summary>.skip_fields）。
    mode=None 或者这个模式没有配置时返回空（普通的局照常写）。"""
    key = modes.summary_key(mode) if mode else None
    return tuple(((cfg.get(key) or {}).get("skip_fields")) or ())


def to_seconds(duration):
    """"16:03" → 963 秒。"""
    if duration is None:
        return None
    minutes, seconds = duration.split(":")
    return int(minutes) * 60 + int(seconds)


def summary_to_match(results, problems, ended_at, evidence_path=None, mode=None, skip=()):
    """把总结页的识别结果整理成数据库里"一局"的格式（键和 storage.COLUMNS 的列名一致）。
    mode：这一局是哪种轮换玩法（见 detect_mode，或开局横幅认出、见 modes.is_rotational），模式写这个值；
    skip 里的项（这个玩法不写进普通字段的，如外卡的最后两行）留空、不写进去。"""
    v = {name: r["value"] for name, r in results.items()}
    kills, assists, knockdowns = v["kda"] if v["kda"] else (None, None, None)
    match = {
        "ended_at": ended_at,
        "placement": v["placement"],
        "squad_kills": v["squad_kills"],
        "kills": kills,
        "assists": assists,
        "knockdowns": knockdowns,
        "damage": v["damage"],
        "survival_time": to_seconds(v["survival_time"]),
        "revives": v["revives"],
        "respawns": v["respawns"],
        "needs_review": int(bool(problems)),
        "review_note": "；".join(problems) or None,
        "evidence_path": data_relative(evidence_path) if evidence_path else None,
    }
    if mode:
        match["mode"] = mode
        for name in skip:
            match[name] = None
    return match


READING_FIELDS = ("placement", "squad_kills", "kills", "assists", "knockdowns", "damage", "survival_time", "revives", "respawns")


def summary_readings(results, skip=()):
    """总结页读到的各项，整理成 (值, 置信度) 两个字典，键是 storage.COLUMNS 的列名（存进各来源读数表 readings 用）。
    击杀/助攻/击倒是一个小块里读出来的，三项用同一个置信度。skip：外卡不写进普通字段的项（值写空）。"""
    match = summary_to_match(results, [], None)
    values = {k: match[k] for k in READING_FIELDS}
    for name in skip:
        values[name] = None
    scores = {}
    for name, r in results.items():
        for key in (("kills", "assists", "knockdowns") if name == "kda" else (name,)):
            scores[key] = r["score"]
    return values, scores


def show(value):
    if value is None:
        return "-"
    if isinstance(value, tuple):
        return "/".join(str(v) for v in value)
    return str(value)


def main():
    parser = argparse.ArgumentParser(description="从赛后总结页截图里读出本局数据")
    parser.add_argument("image", type=Path, help="截图路径")
    parser.add_argument("--save-crops", action="store_true", help="把裁出来的小块存到 captures/debug_crops/")
    parser.add_argument("--lang", choices=game_lang.languages(), default=game_lang.DEFAULT,
                        help="游戏语言（默认简体中文 zh-CN）")
    args = parser.parse_args()

    cfg = load_config(CONFIG_NAME, args.lang)
    img = load_image(args.image)
    if args.save_crops:
        CROPS_DIR.mkdir(parents=True, exist_ok=True)

    mem_start, _ = memory_mb()
    engine = create_engine(SMALL_CROP_PARAMS)

    # 跑两遍：第一遍包含加载模型的时间，第二遍才是实际识别耗时
    start = time.perf_counter()
    read_summary(engine, img, cfg)
    first_s = time.perf_counter() - start
    start = time.perf_counter()
    results, problems = read_summary(engine, img, cfg, args.save_crops)
    second_s = time.perf_counter() - start
    mem_now, mem_peak = memory_mb()

    print(f"图片: {args.image}  尺寸: {img.shape[1]}x{img.shape[0]}\n")
    print(f"{'数据':<10}{'结果':<12}{'OCR 原文':<14}{'置信度':>6}  {'耗时':>7}  状态")
    for r in results.values():
        score = f"{r['score']:.3f}" if r["score"] is not None else "-"
        print(f"{r['label']:<10}{show(r['value']):<12}{r['raw']:<14}{score:>6}  {r['ms']:>5.0f}ms  {STATUS_TEXT[r['status']]}")

    print()
    if problems:
        print("⚠ 需要人工确认：")
        for p in problems:
            print(f"  - {p}")
    else:
        k, a, d = results["kda"]["value"]
        print(f"✅ 第 {results['placement']['value']} 名，{k} 杀 / {a} 助攻 / {d} 击倒，伤害 {results['damage']['value']}")

    print("\n===== 性能 =====")
    print(f"第一遍（含加载模型）: {first_s:.2f} 秒")
    print(f"第二遍（识别 {len(results)} 个小块）: {second_s:.2f} 秒")
    print(f"内存: 启动时 {mem_start:.0f} MB，现在 {mem_now:.0f} MB，峰值 {mem_peak:.0f} MB")
    if args.save_crops:
        print(f"\n裁出的小块已存到: {CROPS_DIR.resolve()}")


if __name__ == "__main__":
    main()
