"""API 异常与错误码定义

从 src/api/response.py 中提取，消除循环依赖 ——
error_response 依赖 Flask（jsonify），但 ErrorCode / ApiError / TaskTimeoutError
被 core、services、task_queue 等多个模块引用，不应耦合在 api 层。
"""
from typing import Optional


# ============================================================
# 错误码定义
# ============================================================

class ErrorCode:
    # 客户端错误 (4xx)
    AUTH_REQUIRED = "AUTH_REQUIRED"                 # 未提供认证 token
    AUTH_FAILED = "AUTH_FAILED"                     # 认证 token 无效
    VALIDATION_ERROR = "VALIDATION_ERROR"           # 参数校验失败
    DUPLICATE_ORDER = "DUPLICATE_ORDER"             # 60秒内重复下单

    # 服务端错误 (5xx)
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"           # 交易窗口未找到
    CONTROL_NOT_FOUND = "CONTROL_NOT_FOUND"         # 控件未找到
    MODE_SWITCH_FAILED = "MODE_SWITCH_FAILED"       # 限价/市价切换失败
    ORDER_SUBMIT_FAILED = "ORDER_SUBMIT_FAILED"     # 订单提交失败（券商返回错误，通用）
    SERVER_CLEARING = "SERVER_CLEARING"             # 券商系统清算中
    OUTSIDE_TRADING_HOURS = "OUTSIDE_TRADING_HOURS" # 非交易时段
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"       # 券商服务器不可用（维护中）
    OCR_FAILED = "OCR_FAILED"                       # 验证码识别失败
    INTERNAL_ERROR = "INTERNAL_ERROR"               # 未知异常

    # 队列相关 (503)
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"                 # 任务排队超时
    QUEUE_FULL = "QUEUE_FULL"                       # 队列已满

    # 超时 (504)
    TASK_TIMEOUT = "TASK_TIMEOUT"                              # 任务超时，恢复成功
    TASK_TIMEOUT_RECOVERY_FAILED = "TASK_TIMEOUT_RECOVERY_FAILED"  # 任务超时，恢复也失败


# HTTP 状态码映射
HTTP_STATUS = {
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_FAILED: 401,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.DUPLICATE_ORDER: 409,
    ErrorCode.WINDOW_NOT_FOUND: 503,
    ErrorCode.CONTROL_NOT_FOUND: 500,
    ErrorCode.MODE_SWITCH_FAILED: 500,
    ErrorCode.ORDER_SUBMIT_FAILED: 500,
    ErrorCode.SERVER_CLEARING: 503,
    ErrorCode.OUTSIDE_TRADING_HOURS: 400,
    ErrorCode.SERVER_UNAVAILABLE: 503,
    ErrorCode.OCR_FAILED: 500,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.QUEUE_TIMEOUT: 503,
    ErrorCode.QUEUE_FULL: 503,
    ErrorCode.TASK_TIMEOUT: 504,
    ErrorCode.TASK_TIMEOUT_RECOVERY_FAILED: 504,
}


# ============================================================
# 异常类
# ============================================================

class ApiError(Exception):
    """API 业务异常"""

    def __init__(
        self,
        error_code: str,
        message: str,
        suggestion: Optional[str] = None,
        details: Optional[dict] = None,
        screenshot: Optional[str] = None
    ):
        self.error_code = error_code
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}
        self.screenshot = screenshot
        super().__init__(message)


class TaskTimeoutError(ApiError):
    """任务超时异常（看门狗触发）"""

    def __init__(self, task_name: str, params: dict, elapsed: float,
                 screenshot: Optional[str] = None, recovery_error: Optional[str] = None):
        if recovery_error is None:
            error_code = ErrorCode.TASK_TIMEOUT
            message = "任务执行超时，已截图存档并重置下单程序为初始状态"
            suggestion = (
                "请立即采取以下检查操作："
                "1) 调用 GET /trades/today 查询订单是否已提交；"
                "2) 调用 GET /positions 查看持仓变化；"
                "3) 必要时登录同花顺客户端手动确认"
            )
        else:
            error_code = ErrorCode.TASK_TIMEOUT_RECOVERY_FAILED
            message = f"任务执行超时，且恢复流程失败: {recovery_error}"
            suggestion = "请立即人工登录同花顺客户端检查订单状态和持仓，并手动恢复下单程序"

        details = {
            "task": task_name,
            "params": params,
            "elapsed_seconds": elapsed,
        }
        super().__init__(error_code, message, suggestion, details, screenshot)
