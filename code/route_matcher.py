"""行进路线的 OpenCV 地图配准；只由记录程序的后台线程按需导入，面板和托盘不加载 OpenCV。"""

import math

import cv2
import numpy as np


def configure_worker():
    cv2.setNumThreads(1)  # 特征提取不占满多个核心；宁可晚一点出结果，也不给游戏抢 CPU


def _gray(image, size):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)


class MapMatcher:
    """一张预置地图的特征。第一次全图找，后续优先只用上个坐标附近的特征。"""

    def __init__(self, path, cfg):
        raw = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if raw is None:
            raise ValueError(f"地图图片读不出来：{path}")
        side = int(cfg["reference_size"])
        self.image = cv2.resize(raw, (side, side), interpolation=cv2.INTER_AREA)
        self.cfg = cfg
        self.last_scope = None  # 上一次 locate 用的是局部特征还是全图；只给离线回放统计用
        self.last_metrics = {}  # 离线诊断，也记录被拒绝的原因
        self.locate_calls = 0
        self.detector, self.norm = self._detector()
        self.keypoints, self.descriptors = self.detector.detectAndCompute(self.image, None)
        if self.descriptors is None or len(self.keypoints) < cfg["min_matches"]:
            raise ValueError(f"地图特征太少：{path}")

    @staticmethod
    def _detector():
        if hasattr(cv2, "SIFT_create"):
            return cv2.SIFT_create(nfeatures=5000, contrastThreshold=0.025), cv2.NORM_L2
        return cv2.ORB_create(nfeatures=5000, fastThreshold=8), cv2.NORM_HAMMING

    def _query(self, crop):
        gray = _gray(crop, int(self.cfg["sample_size"]))
        mask = np.zeros_like(gray)
        center = gray.shape[0] // 2
        radius = round(gray.shape[0] * self.cfg["mask_radius"])
        cv2.circle(mask, (center, center), radius, 255, -1)
        # 中间的玩家箭头和准星永远不属于地图；遮掉以后更不容易把图标当地形。
        cv2.circle(mask, (center, center), round(gray.shape[0] * self.cfg["center_mask"]), 0, -1)
        return self.detector.detectAndCompute(gray, mask)

    def _fit(self, src, dst, good_matches):
        """拟合一个候选，并用几何范围拒绝只靠一小团图标得到的假匹配。"""
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                     ransacReprojThreshold=self.cfg["ransac_px"])
        if matrix is None or inliers is None:
            return None, None
        inliers = inliers.reshape(-1).astype(bool)
        count = int(inliers.sum())
        ratio = count / max(1, int(good_matches))
        if count < self.cfg["min_inliers"] or ratio < self.cfg["min_inlier_ratio"]:
            return None, inliers
        size = float(self.cfg["sample_size"])
        center = np.array([size / 2, size / 2, 1.0], dtype=np.float64)
        x, y = matrix @ center
        x, y = x / self.image.shape[1], y / self.image.shape[0]
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None, inliers
        predicted = cv2.transform(src[inliers][None, :, :], matrix)[0]
        error = float(np.mean(np.linalg.norm(predicted - dst[inliers], axis=1)))
        confidence = min(1.0, count / (self.cfg["min_inliers"] * 2)) * ratio / (1 + error / 5)
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        angle = float(math.degrees(math.atan2(matrix[1, 0], matrix[0, 0])))
        query_inliers = src[inliers]
        width = float(np.ptp(query_inliers[:, 0])) if count > 1 else 0.0
        height = float(np.ptp(query_inliers[:, 1])) if count > 1 else 0.0
        spread = width * height / max(1.0, size * size)
        quadrants = len({(point[0] >= size / 2, point[1] >= size / 2) for point in query_inliers})
        metrics = {
            # ``matches`` 是旧版离线报告字段，保留兼容；good_matches 名字更明确。
            "matches": int(good_matches),
            "good_matches": int(good_matches),
            "inliers": count,
            "inlier_ratio": round(ratio, 4),
            "reprojection_error": round(error, 3),
            "scale": round(scale, 4),
            "angle": round(angle, 2),
            "query_spread": round(spread, 4),
            "query_quadrants": quadrants,
            "confidence": round(confidence, 4),
        }
        if not (float(self.cfg.get("min_scale", 0.75)) <= scale <= float(self.cfg.get("max_scale", 1.65))):
            return {**metrics, "reject_reason": "scale"}, inliers
        if spread < float(self.cfg.get("min_query_spread", 0.012)):
            return {**metrics, "reject_reason": "clustered_inliers"}, inliers
        if quadrants < int(self.cfg.get("min_query_quadrants", 2)):
            return {**metrics, "reject_reason": "single_quadrant"}, inliers
        return {"x": float(x), "y": float(y), **metrics}, inliers

    def _locate_with_indexes(self, query_points, query_desc, indexes):
        """在一组参照特征里匹配，并拒绝两个相距很远且强度接近的空间假设。"""
        indexes = np.arange(len(self.keypoints)) if indexes is None else np.asarray(indexes)
        ref_desc = self.descriptors[indexes]
        pairs = cv2.BFMatcher(self.norm).knnMatch(query_desc, ref_desc, k=2)
        good = [a for a, b in pairs if a.distance < self.cfg["ratio"] * b.distance]
        if len(good) < self.cfg["min_matches"]:
            self.last_metrics = {"reject_reason": "few_matches", "good_matches": len(good)}
            return None
        src = np.float32([query_points[m.queryIdx].pt for m in good])
        dst = np.float32([self.keypoints[int(indexes[m.trainIdx])].pt for m in good])
        result, inliers = self._fit(src, dst, len(good))
        if result is None:
            self.last_metrics = {"reject_reason": "affine_failed", "good_matches": len(good)}
            return None
        if "reject_reason" in result:
            self.last_metrics = result
            return None

        # 第一组内点之外如果还能形成一个相距很远、强度接近的解，这一帧没有唯一答案。
        remaining = ~inliers
        second = None
        if int(remaining.sum()) >= max(int(self.cfg["min_matches"]), int(self.cfg["min_inliers"])):
            second, _second_inliers = self._fit(src[remaining], dst[remaining], int(remaining.sum()))
            if second is not None and "reject_reason" not in second:
                distance = math.hypot(result["x"] - second["x"], result["y"] - second["y"])
                ratio = second["confidence"] / max(0.0001, result["confidence"])
                result["second_confidence"] = second["confidence"]
                result["second_distance"] = round(distance, 4)
                if (distance >= float(self.cfg.get("ambiguity_distance", 0.08))
                        and ratio >= float(self.cfg.get("ambiguity_ratio", 0.75))):
                    self.last_metrics = {**result, "reject_reason": "ambiguous_hypotheses"}
                    return None
        self.last_metrics = dict(result)
        return result

    def _choose_local_full(self, local, full):
        """合并局部和全图结果；冲突且优势不足时宁可漏掉，不强行选择。"""
        if local is None:
            return full, "full" if full is not None else "none"
        if full is None:
            return local, "local"
        distance = math.hypot(local["x"] - full["x"], local["y"] - full["y"])
        if distance <= float(self.cfg.get("local_full_agree_radius", 0.035)):
            return (full, "full") if full["confidence"] > local["confidence"] else (local, "local")
        margin = abs(float(local["confidence"]) - float(full["confidence"]))
        ratio = min(local["confidence"], full["confidence"]) / max(0.0001, max(local["confidence"], full["confidence"]))
        if (margin < float(self.cfg.get("local_full_confidence_margin", 0.18))
                or ratio >= float(self.cfg.get("local_full_ambiguity_ratio", 0.65))):
            self.last_metrics = {
                "reject_reason": "local_full_conflict",
                "local_x": round(local["x"], 5), "local_y": round(local["y"], 5),
                "full_x": round(full["x"], 5), "full_y": round(full["y"], 5),
                "local_confidence": local["confidence"], "full_confidence": full["confidence"],
                "distance": round(distance, 4),
            }
            return None, "ambiguous"
        return (full, "full") if full["confidence"] > local["confidence"] else (local, "local")

    def locate(self, crop, previous=None):
        self.last_scope = "none"
        self.last_metrics = {}
        self.locate_calls += 1
        query_points, query_desc = self._query(crop)
        if query_desc is None or len(query_points) < self.cfg["min_matches"]:
            return None
        if previous is not None:
            radius = self.image.shape[0] * self.cfg["local_radius"]
            cx, cy = previous[0] * self.image.shape[1], previous[1] * self.image.shape[0]
            local = [i for i, kp in enumerate(self.keypoints)
                     if (kp.pt[0] - cx) ** 2 + (kp.pt[1] - cy) ** 2 <= radius ** 2]
            if len(local) >= self.cfg["min_matches"] * 2:
                result = self._locate_with_indexes(query_points, query_desc, local)
                low = result is not None and result["confidence"] < self.cfg.get("local_min_confidence", 0.35)
                periodic = self.locate_calls % max(1, int(self.cfg.get("full_check_every", 6))) == 0
                if low or result is None or periodic:
                    full = self._locate_with_indexes(query_points, query_desc, None)
                    chosen, scope = self._choose_local_full(result, full)
                    self.last_scope = scope
                    if chosen is not None:
                        self.last_metrics = dict(chosen)
                    return chosen
                self.last_scope = "local"
                self.last_metrics = dict(result)
                return result
        self.last_scope = "full"
        result = self._locate_with_indexes(query_points, query_desc, None)
        if result is not None:
            self.last_metrics = dict(result)
        return result
