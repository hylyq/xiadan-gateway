"""统一响应封装

所有 API 返回统一格式:
    成功: {"status": "success", "request_id": "...", "timestamp": "...", "data": {...}}
    失败: {"status": "error", "request_id": "...", "timestamp": "...",
           "error_code": "...", "message": "...", "suggestion": "...", "details": {...}}
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from flask import jsonify

from src.exceptions import ErrorCode, ApiError, HTTP_STATUS  # noqa: F401 — 重新导出供外部使用


def generate_request_id() -> str:
    """生成唯一请求 ID（时间戳 + 完整 UUID hex，避免并发冲突）"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:12]
    return f"req_{timestamp}_{short_uuid}"


def success_response(data: Any, request_id: Optional[str] = None,
                     duration_ms: Optional[float] = None) -> tuple:
    """构建成功响应

    Args:
        data: 响应数据
        request_id: 请求 ID，不传则自动生成
        duration_ms: 请求处理耗时（毫秒），不传则不返回

    Returns:
        (flask Response, http_status)
    """
    body = {
        "status": "success",
        "request_id": request_id or generate_request_id(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": data,
    }
    if duration_ms is not None:
        body["duration_ms"] = round(duration_ms, 1)
    return jsonify(body), 200


def error_response(
    error_code: str,
    message: str,
    request_id: Optional[str] = None,
    suggestion: Optional[str] = None,
    details: Optional[dict] = None,
    screenshot: Optional[str] = None
) -> tuple:
    """构建错误响应

    Returns:
        (flask Response, http_status)
    """
    response = {
        "status": "error",
        "request_id": request_id or generate_request_id(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error_code": error_code,
        "message": message,
    }
    if suggestion:
        response["suggestion"] = suggestion
    if details:
        response["details"] = details
    if screenshot:
        response["screenshot"] = screenshot

    http_status = HTTP_STATUS.get(error_code, 500)
    # 统一返回 HTTP 200，通过 JSON 中的 status 字段区分成功/失败。
    # 避免 PowerShell 的 Invoke-RestMethod 等客户端因 HTTP 错误状态码抛异常。
    return jsonify(response), 200


def error_response_from_exception(e: Exception, request_id: Optional[str] = None) -> tuple:
    """从异常构建错误响应"""
    if isinstance(e, ApiError):
        return error_response(
            e.error_code, e.message, request_id,
            e.suggestion, e.details, e.screenshot
        )

    # 未知异常
    return error_response(
        ErrorCode.INTERNAL_ERROR,
        f"内部错误: {str(e)}",
        request_id
    )
