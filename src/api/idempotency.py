"""下单幂等检查（强制客户端幂等键）

API 契约：POST /orders 必须携带 Idempotency-Key 请求头（≤128 字符）。

- **key 生命周期**：每个逻辑订单一个 key；HTTP 超时重试必须复用同一 key
  （窗口内同 key 拒绝 = 重试保护）；确要新单（含同参数多单）请新 key
  （不同 key 同参数立即放行——显式意图优于服务端猜测）
- 服务端不校验"随机性"（无法校验），契约是**唯一性**；uuid4 是最省力的
  达成方式，"策略ID+自增序号"同样合法
- 去重窗口语义 = 同 key 重试拦截窗（order_dedup_window_seconds，默认 60s，
  与推荐客户端超时 40s 校准）

历史注记：2026-09-29 前为"可选 key + 参数指纹兜底"双通道，混合使用存在
穿透缺口（keyed 下单后 keyless 重试不被指纹拦截），key 必填后该缺口在
构造上消失（无 keyless 路径）。
"""
import time
from threading import Lock

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
    - ORDER_STATE_UNKNOWN：下单点击提交后的非业务异常（会话断开杀桌面等），
      订单可能已提交 → 保留（同 key 重试被 DUPLICATE_ORDER 拦截，逼调用
      方先查单核实，确认未提交后用新 key 重试）
    - 其余失败（业务报错/参数校验/队列满/会话健康门拒绝 SESSION_UNAVAILABLE）：
      任务确定未执行 → 清除以便重试（SESSION_UNAVAILABLE 在任务开始前
      毫秒级拒绝、未触碰客户端，恢复后同 key 重试即正常执行）
    """
    if isinstance(e, TaskTimeoutError):
        return True
    return getattr(e, "error_code", None) in (
        ErrorCode.TASK_TIMEOUT,
        ErrorCode.TASK_TIMEOUT_RECOVERY_FAILED,
        ErrorCode.QUEUE_TIMEOUT,
        ErrorCode.ORDER_STATE_UNKNOWN,
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

    def check_and_record(self, idem_key: str) -> None:
        """检查是否重复，如果不重复则记录

        Args:
            idem_key: 客户端幂等键（Idempotency-Key 请求头，必填）。
                同 key 窗口内重复 → DUPLICATE_ORDER；不同 key 一律放行。

        Raises:
            ApiError: key 缺失（VALIDATION_ERROR）或窗口内同 key 重复
                （DUPLICATE_ORDER）
        """
        key = self._normalize_key(idem_key)
        now = time.time()
        window = self.config.get_idempotency_config().get("order_dedup_window_seconds", 60)

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
                    message=f"同一 Idempotency-Key 在 {window}秒内已提交过"
                            f"（{elapsed}秒前）",
                    suggestion=(
                        "同 key = 同一笔逻辑订单。若这是一次超时重试，请先确认"
                        "上笔状态: 1) GET /orders/{entrust_no}/status 查询委托"
                        "状态与成交；2) 如需撤单调用 POST /orders/cancel-all。"
                        "若确要新下一笔（含同参数多单），请新生成一个 key"
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

    def clear_record(self, idem_key: str) -> bool:
        """清除下单记录（下单失败时调用，允许同 key 重试）

        Args:
            idem_key: 与 check_and_record 相同的客户端幂等键

        Returns:
            是否清除了记录
        """
        key = self._normalize_key(idem_key)
        with self._records_lock:
            if key in self._records:
                del self._records[key]
                self.logger.info(f"下单失败，已清除幂等记录: {key}")
                return True
            return False

    @staticmethod
    def _normalize_key(idem_key) -> str:
        """幂等键规范化：非空校验 + 统一命名空间前缀

        空/纯空白 key 直接 VALIDATION_ERROR（路由层已前置校验，此处兜底）。
        """
        k = str(idem_key or "").strip()
        if not k:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "缺少 Idempotency-Key（幂等键必填）",
                suggestion=(
                    "为每笔逻辑订单生成一个唯一键（如 uuid4），"
                    "HTTP 超时重试必须复用同一 key；确要新下一笔请新 key"
                ),
            )
        return f"client:{k}"

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
