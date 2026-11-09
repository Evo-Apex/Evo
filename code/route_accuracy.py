"""路线的独立人工真值误差统计；只做几何计算，不读数据库和录像。"""

import math


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
    """rows 每项含 manual_x/manual_y，可含 alg_x/alg_y、visible、indoor、segment_id。

    只看可见、非室内、且有算法坐标的行；不可见/室内单独计数，不当作 0 误差。
    """
    errors = []
    for row in rows:
        if not row.get("visible", True) or row.get("indoor"):
            continue
        if row.get("alg_x") is None or row.get("alg_y") is None:
            continue
        errors.append(distance({"x": row["manual_x"], "y": row["manual_y"]},
                               {"x": row["alg_x"], "y": row["alg_y"]}))
    total = len(rows)
    visible = sum(1 for row in rows if row.get("visible", True))
    indoor = sum(1 for row in rows if row.get("indoor"))
    return {
        "total": total,
        "visible": visible,
        "indoor": indoor,
        "matched": len(errors),
        "coverage": round(len(errors) / max(1, visible), 4),
        "median": round(percentile(errors, 0.5), 4) if errors else None,
        "p90": round(percentile(errors, 0.9), 4) if errors else None,
        "p95": round(percentile(errors, 0.95), 4) if errors else None,
        "within_002": round(sum(1 for e in errors if e <= near) / max(1, len(errors)), 4),
        "over_008": sum(1 for e in errors if e > bad),
        "exact_rate": round(sum(1 for e in errors if e <= 0.005) / max(1, len(errors)), 4),
    }
