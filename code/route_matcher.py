"""行进路线的 OpenCV 地图配准；只由记录程序的后台线程按需导入，面板和托盘不加载 OpenCV。"""

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

    def _locate_with_indexes(self, query_points, query_desc, indexes):
        """在一组参照特征里匹配。局部匹配失败时，locate 会再回退到全图。"""
        indexes = np.arange(len(self.keypoints)) if indexes is None else np.asarray(indexes)
        ref_desc = self.descriptors[indexes]
        pairs = cv2.BFMatcher(self.norm).knnMatch(query_desc, ref_desc, k=2)
        good = [a for a, b in pairs if a.distance < self.cfg["ratio"] * b.distance]
        if len(good) < self.cfg["min_matches"]:
            return None
        src = np.float32([query_points[m.queryIdx].pt for m in good])
        dst = np.float32([self.keypoints[int(indexes[m.trainIdx])].pt for m in good])
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                     ransacReprojThreshold=self.cfg["ransac_px"])
        if matrix is None or inliers is None:
            return None
        inliers = inliers.reshape(-1).astype(bool)
        count = int(inliers.sum())
        ratio = count / len(good)
        if count < self.cfg["min_inliers"] or ratio < self.cfg["min_inlier_ratio"]:
            return None
        size = float(self.cfg["sample_size"])
        center = np.array([size / 2, size / 2, 1.0], dtype=np.float64)
        x, y = matrix @ center
        x, y = x / self.image.shape[1], y / self.image.shape[0]
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        predicted = cv2.transform(src[inliers][None, :, :], matrix)[0]
        error = float(np.mean(np.linalg.norm(predicted - dst[inliers], axis=1)))
        confidence = min(1.0, count / (self.cfg["min_inliers"] * 2)) * ratio / (1 + error / 5)
        return {"x": float(x), "y": float(y), "confidence": round(confidence, 4), "matches": count}

    def locate(self, crop, previous=None):
        self.last_scope = "none"
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
                if result is not None:
                    self.last_scope = "local"
                    return result
        self.last_scope = "full"
        return self._locate_with_indexes(query_points, query_desc, None)
