"""下单幂等检查

防止 HTTP 超时后客户端重试导致重复下单。
60 秒窗口内相同参数的下单请求会被拒绝。
"""
import time
from threading import Lock
from typing import Optional

from src.api.response import ApiError, ErrorCode
from src.models.config import AppConfig
from src.utils.logger import Logger
from src.utils.singleton import Singleton


class IdempotencyChecker(Singleton):
    """幂等检查器（单例）"""

    @classmethod
    def get_instance(cls) -> "IdempotencyChecker":
        return cls._get_instance()

    def _init(self):
        self.logger = Logger.get_instance()
        self.config = AppConfig()
        # 任务记录: {task_key: timestamp}
        self._records = {}
        self._records_lock = Lock()

    def _make_key(self, code: str, status: str, amount: Optional[str],
                  price: Optional[str], price_type: str) -> str:
        """生成任务唯一键"""
        return f"{code}_{status}_{amount or ''}_{price or ''}_{price_type}"

    def check_and_record(
        self,
        code: str,
        status: str,
        amount: Optional[str] = None,
        price: Optional[str] = None,
        price_type: str = "limit"
    ) -> None:
        """检查是否重复，如果不重复则记录

        Raises:
            ApiError: 60 秒内重复下单
        """
        window = self.config.get_idempotency_config().get("order_dedup_window_seconds", 60)
        key = self._make_key(code, status, amount, price, price_type)
        now = time.time()

        with self._records_lock:
            # 清理过期记录
            expired_keys = [k for k, t in self._records.items() if now - t > window]
            for k in expired_keys:
                del self._records[k]

            # 检查重复
            if key in self._records:
                last_time = self._records[key]
                elapsed = int(now - last_time)
                self.logger.warning(f"重复下单被拒绝: {key}, 距上次 {elapsed}s")
                raise ApiError(
                    error_code=ErrorCode.DUPLICATE_ORDER,
                    message=f"60秒内已提交相同订单（{elapsed}秒前），请勿重复下单",
                    suggestion=(
                        "请先确认上一笔订单状态: "
                        "1) 调用 GET /trades/today 查询订单是否已成交；"
                        "2) 如需撤单请调用 POST /orders/cancel-all；"
                        "3) 确认后再重新下单"
                    ),
                    details={
                        "task_key": key,
                        "elapsed_seconds": elapsed,
                        "dedup_window_seconds": window
                    }
                )

            # 记录
            self._records[key] = now
            self.logger.info(f"记录下单任务: {key}")

    def clear_record(
        self,
        code: str,
        status: str,
        amount: Optional[str] = None,
        price: Optional[str] = None,
        price_type: str = "limit"
    ) -> bool:
        """清除下单记录（下单失败时调用，允许重试）

        Returns:
            是否清除了记录
        """
        key = self._make_key(code, status, amount, price, price_type)
        with self._records_lock:
            if key in self._records:
                del self._records[key]
                self.logger.info(f"下单失败，已清除幂等记录: {key}")
                return True
            return False

    def get_status(self) -> dict:
        """获取幂等检查状态"""
        with self._records_lock:
            return {
                "record_count": len(self._records),
                "records": [
                    {"key": k, "age_seconds": int(time.time() - t)}
                    for k, t in self._records.items()
                ]
            }
