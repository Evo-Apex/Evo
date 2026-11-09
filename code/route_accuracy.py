"""路线的独立人工真值误差统计；只做几何计算，不读数据库和录像。"""

import math


def as_bool(value, default=False):
    """把 CSV / JSON 里的布尔值统一成 bool，避免字符串 ``"false"`` 被当成 True。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "visible", "indoor", "occluded"}


def distance(a, b):
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def percentile(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    at = (len(ordered) - 1) * ratio
    lo = int(at)
    hi = min(lo + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo))


def summarize(rows, near=0.02, bad=0.08):
    """汇总独立人工真值与算法结果。

    rows 每项含 manual_x/manual_y，可含 alg_x/alg_y、visible、indoor、occluded。
    误差只统计可见、非室内、未遮挡且有人工坐标的 eligible 行；没有算法坐标
    是漏检，进入覆盖率分母但不伪造成 0 误差。另报全部可见帧覆盖率，避免混淆口径。
    """
    errors = []
    eligible_rows = []
    visible_rows = []
    for row in rows:
        visible = as_bool(row.get("visible"), True)
        indoor = as_bool(row.get("indoor"))
        occluded = as_bool(row.get("occluded"))
        if visible:
            visible_rows.append(row)
        manual = row.get("manual_x") not in (None, "") and row.get("manual_y") not in (None, "")
        if not visible or indoor or occluded or not manual:
            continue
        eligible_rows.append(row)
        if row.get("alg_x") in (None, "") or row.get("alg_y") in (None, ""):
            continue
        errors.append(distance({"x": row["manual_x"], "y": row["manual_y"]},
                               {"x": row["alg_x"], "y": row["alg_y"]}))
    total = len(rows)
    visible = len(visible_rows)
    eligible = len(eligible_rows)
    indoor = sum(1 for row in rows if as_bool(row.get("indoor")))
    occluded = sum(1 for row in rows if as_bool(row.get("occluded")))
    matched_visible = sum(1 for row in visible_rows
                          if row.get("alg_x") not in (None, "") and row.get("alg_y") not in (None, ""))
    within = sum(1 for error in errors if error <= near)
    return {
        "total": total,
        "visible": visible,
        "eligible": eligible,
        "indoor": indoor,
        "occluded": occluded,
        "matched": len(errors),
        "coverage": round(len(errors) / max(1, eligible), 4),
        "eligible_coverage": round(len(errors) / max(1, eligible), 4),
        "overall_visible_coverage": round(matched_visible / max(1, visible), 4),
        "median": round(percentile(errors, 0.5), 4) if errors else None,
        "p90": round(percentile(errors, 0.9), 4) if errors else None,
        "p95": round(percentile(errors, 0.95), 4) if errors else None,
        "within_002": round(within / max(1, len(errors)), 4),
        "precision_le_002": round(within / max(1, len(errors)), 4),
        "over_008": sum(1 for e in errors if e > bad),
        "exact_rate": round(sum(1 for e in errors if e <= 0.005) / max(1, len(errors)), 4),
    }
