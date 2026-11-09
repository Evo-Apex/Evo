"""4:3 数据页离线复现：只给开发时排查用，不在正常运行时保存截图。

一张用户主动提供的截图 → 按指定/自动检测的游戏比例换算 ROI、判断数据页、读玩家名和赛季数字，
把每一步的中间结果和失败步骤打出来。真实 4:3 截图还没拿到时，这个工具只用于合成图和用户回传的图。
"""

from __future__ import annotations

from pathlib import Path

import layout

ASPECTS = {"16:9": 16 / 9, "16:10": 16 / 10, "4:3": 4 / 3}
ROI_NAMES = (
    ("detect", "数据页检测", ("detect", "screens", "season_stats", "roi")),
    ("name", "玩家名", ("name", "roi")),
    ("label", "赛季标签", ("label", "roi")),
    ("blocks", "顶部一块", ("blocks", "top")),
    ("damage", "伤害两块", ("blocks", "damage")),
    ("kills", "击杀两块", ("blocks", "kills")),
    ("rows", "下面六行", ("blocks", "rows")),
)


def resolve_aspect(choice, frame, detected):
    """返回 (比例, 来源)。显式选择优先；auto 才用自动检测/截图比例，绝不把 1920×1080 当 16:9 盖掉 4:3。"""
    if choice in ASPECTS:
        return ASPECTS[choice], f"显式选择 {choice}"
    if detected:
        return detected, "自动检测（视频设置）"
    h, w = frame.shape[:2]
    return w / h, "截图比例（没有视频设置可读）"


def _get(cfg, path):
    value = cfg
    for key in path:
        value = value[key]
    return value


def roi_boxes(cfg, shape, aspect):
    """按这个游戏比例，把配置里的基准坐标换成截图上的像素框 {名字: (左, 上, 右, 下)}。"""
    base = cfg["base_resolution"]
    out = {}
    for key, label, path in ROI_NAMES:
        spec = _get(cfg, path)
        anchor = spec.get("anchor") if isinstance(spec, dict) else None
        out[key] = (label, layout.Mapping(shape, base, anchor, aspect).box(spec))
    return out


def inspect(engine, frame, cfg, aspect, annotate=None):
    """跑一遍识别，返回可直接打印的文本行；失败时也把失败步骤写出来。"""
    from season_stats import SeasonPage

    h, w = frame.shape[:2]
    page = SeasonPage(engine, cfg)
    name = next((key for key, value in ASPECTS.items() if abs(value - aspect) < 1e-4), f"{aspect:.4f}")
    lines = [f"输入尺寸：{w}×{h}",
             f"最终采用的游戏比例：{aspect:.4f}（{name}）"]
    boxes = roi_boxes(cfg, frame.shape, aspect)
    for label, box in boxes.values():
        lines.append(f"  {label}：{box}")
    score = page.detector.similarity(frame, "season_stats")
    lines.append(f"数据页外观分：{'没有参照图' if score is None else f'{score:.3f}'}")
    if not page.on_page(frame):
        lines.append("数据页识别：不是（停在第一步；看上面的外观分和 ROI）")
        lines.append("失败步骤：数据页检测")
        _annotate(frame, boxes, annotate)
        return lines
    lines.append("数据页识别：是")
    name = page.name(frame)
    lines.append(f"玩家名：{name or '（没读出来）'}")
    season, kind, season_name, raw = page.label(frame, {})
    lines.append(f"赛季标签：{season if season is not None else '（没读出来）'} / {kind or '-'} / {season_name or '-'}")
    lines.append(f"赛季标签原文：{raw or '（空）'}")
    reading = page.read(frame, "season", {})
    lines.append(f"数字：{reading['values'] or '（没读出来）'}")
    lines.append(f"约数：{reading['approx'] or '（无）'}")
    lines.append(f"被说明框挡住：{'是' if reading['obscured'] else '否'}")
    if not reading["values"]:
        lines.append("失败步骤：数字 OCR")
    elif season is None:
        lines.append("失败步骤：赛季标签")
    elif not name:
        lines.append("失败步骤：玩家名")
    else:
        lines.append("失败步骤：没走到（这一张能读）")
    _annotate(frame, boxes, annotate)
    return lines


def _annotate(frame, boxes, path):
    if not path:
        return
    from PIL import Image, ImageDraw, ImageFont

    h, w = frame.shape[:2]
    scale = min(1.0, 900 / max(w, h))
    canvas = Image.fromarray(frame[:, :, ::-1]).resize((round(w * scale), round(h * scale)),
                                                       Image.LANCZOS)
    draw = ImageDraw.Draw(canvas)
    try:
        label_font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 14)
    except OSError:
        label_font = None
    # 玩家名/ID 那一块先涂黑，再画框；标注图不保留玩家 ID。
    name_box = boxes.get("name")
    if name_box:
        x0, y0, x1, y1 = name_box[1]
        draw.rectangle((round(x0 * scale), round(y0 * scale), round(x1 * scale), round(y1 * scale)),
                       fill=(0, 0, 0))
    for label, (x0, y0, x1, y1) in boxes.values():
        rect = tuple(round(v * scale) for v in (x0, y0, x1, y1))
        draw.rectangle(rect, outline=(255, 92, 92), width=2)
        draw.text((rect[0] + 3, max(0, rect[1] - 16)), label, fill=(255, 210, 120), font=label_font)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
