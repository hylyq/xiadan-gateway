"""下单幂等检查

防止 HTTP 超时后客户端重试导致重复下单。
60 秒窗口内相同参数的下单请求会被拒绝。
"""
import time
from threading import Lock
from typing import Optional

from src.exceptions import ApiError, ErrorCode, TaskTimeoutError
from src.models.config import AppConfig
from src.utils.logger import Logger
from src.utils.singleton import Singleton


def should_keep_record_on_error(e: Exception) -> bool:
    """异常发生后，幂等记录是否应保留（下单可能仍会执行时保留）

    防重复下单的关键判定：
    - TaskTimeoutError（看门狗超时）：任务可能仍在执行 → 保留
    - QUEUE_TIMEOUT：submit 等待超时返回错误，但任务仍在队列中、
      稍后仍会被执行——此时清除记录并让客户端重试，两单都会成交 → 保留
    - 其余失败（业务报错/参数校验/队列满）：任务确定未执行 → 清除以便重试
    """
    if isinstance(e, TaskTimeoutError):
        return True
    return getattr(e, "error_code", None) in (
        ErrorCode.TASK_TIMEOUT,
        ErrorCode.TASK_TIMEOUT_RECOVERY_FAILED,
        ErrorCode.QUEUE_TIMEOUT,
    )


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

    def _resolve_key(self, idem_key: Optional[str], code: str, status: str,
                     amount: Optional[str], price: Optional[str],
                     price_type: str) -> str:
        """解析幂等键：客户端 Idempotency-Key 优先，缺省回退参数指纹

        客户端键加 "client:" 前缀隔离命名空间，避免与参数指纹撞键；
        同一策略重试传同一 key 即可去重，不同策略同参数互不干扰
        （参数指纹模式下的互撞问题）。
        """
        if idem_key:
            return f"client:{idem_key}"
        return self._make_key(code, status, amount, price, price_type)

    def check_and_record(
        self,
        code: str,
        status: str,
        amount: Optional[str] = None,
        price: Optional[str] = None,
        price_type: str = "limit",
        idem_key: Optional[str] = None
    ) -> None:
        """检查是否重复，如果不重复则记录

        Args:
            idem_key: 客户端幂等键（Idempotency-Key 请求头）。提供时以它
                为去重依据（60s 窗口内同 key 拒绝），参数指纹退居其次；
                缺省时按参数指纹去重（向后兼容）。

        Raises:
            ApiError: 60 秒内重复下单
        """
        window = self.config.get_idempotency_config().get("order_dedup_window_seconds", 60)
        key = self._resolve_key(idem_key, code, status, amount, price, price_type)
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
                    message=f"{window}秒内已提交相同订单（{elapsed}秒前），请勿重复下单",
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
        price_type: str = "limit",
        idem_key: Optional[str] = None
    ) -> bool:
        """清除下单记录（下单失败时调用，允许重试）

        Args:
            idem_key: 与 check_and_record 相同的客户端幂等键，确保清除的
                是同一把键（客户端键模式下参数指纹里没有记录）

        Returns:
            是否清除了记录
        """
        key = self._resolve_key(idem_key, code, status, amount, price, price_type)
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
