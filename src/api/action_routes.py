"""手动操作/诊断路由 Blueprint

包含: 发送按键、鼠标点击、关闭对话框、诊断截图、诊断历史
"""
import time
from typing import Optional

import pyautogui
from flask import Blueprint, request

from src.api.helpers import get_param
from src.api.response import (
    generate_request_id,
    success_response, error_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.exceptions import ErrorCode
from src.models.config import AppConfig
from src.services.window_service import WindowService

action_bp = Blueprint("action", __name__)


@action_bp.route("/actions/send-key", methods=["POST"])
def send_key():
    """手动发送按键

    参数（JSON body 或 query string 均可）:
        key: 按键，如 F1, F2, Y, {CTRL+C}

    示例:
        POST /actions/send-key  {"key": "F1"}
        POST /actions/send-key  {"key": "{CTRL+C}"}
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    window_service = WindowService()

    key = get_param("key")
    if not key:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "key 参数不能为空", request_id,
            "示例: POST /actions/send-key {\"key\": \"F1\"}"
        )

    _start = time.time()
    try:
        confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
        result = task_queue.submit(
            func=lambda: window_service.send_key(key) or {"key": key, "sent": True},
            task_name="send_key",
            params={"key": key},
            timeout=confirm_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@action_bp.route("/actions/click", methods=["POST"])
def click():
    """鼠标点击坐标

    参数（JSON body 或 query string 均可）:
        x: 横坐标
        y: 纵坐标

    示例:
        POST /actions/click  {"x": 100, "y": 200}
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()

    x = get_param("x")
    y = get_param("y")

    if x is None or y is None:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "x 和 y 参数不能为空", request_id,
            "示例: POST /actions/click {\"x\": 100, \"y\": 200}"
        )

    try:
        x_int = int(x)
        y_int = int(y)
    except ValueError:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "x 和 y 必须是整数", request_id
        )

    _start = time.time()
    try:
        confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
        result = task_queue.submit(
            func=lambda: (pyautogui.click(x_int, y_int), {"x": x_int, "y": y_int})[1],
            task_name="click",
            params={"x": x_int, "y": y_int},
            timeout=confirm_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@action_bp.route("/actions/close-dialog", methods=["POST"])
def close_dialog():
    """安全关闭子对话框（如买入/卖出窗口）

    使用 SendMessage WM_CLOSE 仅关闭嵌入的子对话框，
    绝不关闭整个券商程序。

    参数（JSON body 或 query string 均可）:
        title: 对话框标题，如 "买入"（可选，留空自动尝试关闭活动子窗口）

    示例:
        POST /actions/close-dialog {"title": "买入"}
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    window_service = WindowService()

    title = get_param("title") or ""

    _start = time.time()
    try:
        confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
        result = task_queue.submit(
            func=lambda: window_service.close_child_dialog(title),
            task_name="close_dialog",
            params={"title": title},
            timeout=confirm_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


# ------------------------------------------------------------
# 调试诊断（开发测试用）
# ------------------------------------------------------------

@action_bp.route("/diagnostic/snapshot", methods=["GET"])
def diagnostic_snapshot():
    """截图 + OCR 全文本识别（调试用）

    返回当前交易窗口的截图和 OCR 识别结果，
    用于开发测试时验证每一步操作的界面状态。
    不入队，立即返回。
    """
    from src.utils.diagnostic import DiagnosticUtil
    info = DiagnosticUtil().snapshot("api_diagnostic")
    return success_response({
        "screenshot": info.get("screenshot"),
        "ui_text": info.get("ui_text", ""),
        "ocr_text": info.get("ocr_text", ""),
        "ocr_failed": info.get("ocr_failed", True),
    })


@action_bp.route("/diagnostic/history", methods=["GET"])
def diagnostic_history():
    """诊断历史记录（开发调试用）

    返回最近 N 个任务执行后的界面状态快照。

    Query 参数:
        n: 返回条数（默认 5，最大 20）

    示例:
        GET /diagnostic/history          # 最近 5 条
        GET /diagnostic/history?n=10     # 最近 10 条
    """
    task_queue = TaskQueue.get_instance()
    n_str = request.args.get("n", "5")
    try:
        n = max(1, min(20, int(n_str)))
    except (ValueError, TypeError):
        n = 5
    history = task_queue.get_diagnostic_history(n)
    return success_response({
        "total_returned": len(history),
        "max_available": 20,
        "entries": history,
    })


@action_bp.route("/ocr/quality", methods=["GET"])
def ocr_quality_report():
    """查看 OCR 质检报告

    返回轻量引擎 vs ddddocr 的对比统计:
      - 累计识别次数
      - 准确率
      - 模板覆盖情况
      - 连续正确次数
    """
    from src.core.ocr import OcrService
    ocr = OcrService.get_instance()
    return success_response(ocr.quality_report)
