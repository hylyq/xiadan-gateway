"""下单横幅截获：合同编号（委托号）回传

快速交易模式下点击下单按钮后，客户端在窗口底部状态栏右段绘制一条
黄色横幅（自绘覆盖层，无窗口文本、无 UIA Name，存活约 1-2 秒）：
「您的买入委托已成功提交，合同编号：6246860043。」

截获方式（feat/entrust-no 探索结论，2026-09-09 模拟盘验证）：
1. 提交前启动后台线程，对窗口右下角固定区域（宽 45% × 底部 32px）
   以 ~12fps 连拍——只读屏幕像素，不碰 UIA（无 COM 线程问题）
2. numpy 黄色掩码定位横幅条带（黄底红字，与状态栏背景区分度极高）
3. BannerDigitOCR 模板匹配提取 8 位以上合同编号（~35ms，零外部依赖，
   10 个数字已经真实横幅样本逐位验证，见 scripts/test_template_ocr.py）

注意: 横幅号即客户端提交时显示的合同编号；极端情况（券商服务器
维护窗口）下与最终落表号可能不一致，关键操作前应以当日委托查询复核。
"""
import threading
import time

import numpy as np
import win32gui
from PIL import ImageGrab

from src.core.banner_ocr import BannerDigitOCR
from src.utils.logger import Logger

ENTRUST_LEN_MIN = 8

# 横幅黄色掩码阈值（黄底红字，采样自模拟盘实测截图）
_YELLOW_R, _YELLOW_G, _YELLOW_B = 200, 180, 120


class EntrustNoCapture:
    """下单横幅后台截获器（单次下单生命周期，用完即弃）"""

    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or Logger.get_instance()
        self._ocr = BannerDigitOCR()
        self._thread = None
        self._result = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get_order_config().get("capture_entrust_no", False))

    def start(self, window) -> None:
        """提交前调用：启动后台截获线程"""
        if not self.enabled or window is None:
            return
        self._result = None
        self._thread = threading.Thread(
            target=self._run, args=(window.handle,), daemon=True)
        self._thread.start()

    def wait_result(self, timeout: float):
        """主流程收尾时调用：阻塞等待截获结果，返回委托号 str 或 None"""
        if self._thread is None:
            return None
        self._thread.join(timeout=timeout)
        return self._result

    # ------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------

    def _run(self, hwnd) -> None:
        timeout = float(self.config.get_order_config()
                        .get("entrust_no_timeout_seconds", 3.0))
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            try:
                digits = self._grab_banner_digits(hwnd)
                if digits:
                    self._result = digits
                    self.logger.info(
                        f"横幅截获委托号: {digits}"
                        f"（耗时 {time.perf_counter() - t0:.2f}s）")
                    return
            except Exception as e:
                self.logger.warning(f"横幅截获异常: {e}")
            time.sleep(0.08)
        self.logger.info("横幅截获超时，未获得委托号（窗口最小化或横幅未出现）")

    def _grab_banner_digits(self, hwnd):
        """截取右下角区域 → 黄色掩码定位条带 → 模板匹配数字，无横幅返回 None"""
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        box = (l + int((r - l) * 0.55), b - 34, r - 4, b - 2)
        img = ImageGrab.grab(bbox=box)
        arr = np.asarray(img)
        mask = ((arr[:, :, 0] > _YELLOW_R)
                & (arr[:, :, 1] > _YELLOW_G)
                & (arr[:, :, 2] < _YELLOW_B))
        if int(mask.sum()) < 300:
            return None
        rows = np.where(mask.sum(axis=1) > 30)[0]
        cols = np.where(mask.sum(axis=0) > 5)[0]
        band = np.asarray(img.crop((int(cols.min()), int(rows.min()),
                                    int(cols.max()) + 1, int(rows.max()) + 1)))
        digits, _conf = self._ocr.read_digits(band)
        return digits if len(digits) >= ENTRUST_LEN_MIN else None
