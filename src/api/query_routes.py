"""查询类路由 Blueprint

包含: 资金余额、持仓、今日成交、当日委托
"""
import time

from flask import Blueprint

from src.api.response import (
    generate_request_id, success_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.models.config import AppConfig

query_bp = Blueprint("query", __name__)


def _get_position_service():
    """延迟获取 PositionService（避免循环依赖）"""
    from src.core.ocr import OcrService
    from src.services.position_service import PositionService
    from src.services.window_service import WindowService
    return PositionService(WindowService(), OcrService.get_instance())


@query_bp.route("/account/balance", methods=["GET"])
def get_balance():
    """获取资金余额

    通过 control_id 批量读取（无需 OCR），速度快。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_balance(),
            task_name="get_balance",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/positions", methods=["GET"])
def get_position():
    """获取当前持仓

    流程: F4 + Ctrl+C + 剪切板解析，可能需要 OCR 验证码。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_position(),
            task_name="get_position",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/trades/today", methods=["GET"])
def get_today_trades():
    """获取今日成交

    流程: 树形菜单导航到"当日成交" + Ctrl+C + 剪切板解析。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_today_trades(),
            task_name="get_today_trades",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/pending", methods=["GET"])
def get_today_orders():
    """获取当日委托

    返回当日所有委托记录，含状态（待报、已报、部成、已成、已撤、部撤等）。
    流程: 树形菜单导航到"当日委托" + Ctrl+C + 剪切板解析。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_today_orders(),
            task_name="get_today_orders",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)
