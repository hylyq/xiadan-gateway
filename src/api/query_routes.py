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
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
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
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
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
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_today_trades(),
            task_name="get_today_trades",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/<entrust_no>/status", methods=["GET"])
def get_order_status(entrust_no: str):
    """按合同编号查询委托状态与成交回报

    合同编号 join 当日委托 × 当日成交：返回委托状态（由数量推导）、
    成交聚合（数量/金额/加权均价）与逐笔成交编号列表。
    found=false 表示当日委托中无此编号（非当日委托或编号有误）。
    流程: 当日委托查询 → 命中后当日成交查询 → 纯函数聚合（一次排队）。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        if not (entrust_no.isdigit() and 6 <= len(entrust_no) <= 24):
            from src.exceptions import ApiError, ErrorCode
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"entrust_no 格式错误: {entrust_no}",
                suggestion="合同编号为 6-24 位纯数字（不同券商/交易所长度不同）")
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_order_status(entrust_no),
            task_name="get_order_status",
            params={"entrust_no": entrust_no},
            timeout=query_timeout * 2  # 复合查询：最多两次表格复制
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/pending", methods=["GET"])
def get_today_orders():
    """获取当日委托

    返回当日所有委托记录。实测表头无独立"状态"列——委托状态通过
    「备注」（如"全部撤单"）与「撤消数量/成交数量」体现。
    流程: 树形菜单导航到"当日委托" + Ctrl+C + 剪切板解析。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: _get_position_service().get_today_orders(),
            task_name="get_today_orders",
            params={},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)
