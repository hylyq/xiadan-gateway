"""下单/撤单类路由 Blueprint

包含: 下单、一键清仓、撤单、确认委托
"""
import time
from typing import Optional

from flask import Blueprint, request

from src.api.helpers import get_param
from src.api.idempotency import IdempotencyChecker
from src.api.response import (
    generate_request_id,
    success_response, error_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.core.trader import Trader
from src.exceptions import ApiError, ErrorCode, TaskTimeoutError
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger

order_bp = Blueprint("order", __name__)


def _get_trader() -> Trader:
    return Trader(WindowService())


@order_bp.route("/orders", methods=["POST"])
def xiadan():
    """下单

    参数（JSON body 或 query string 均可）:
        code: 股票代码（必填）
        status: '1'=买入, '2'=卖出（必填）
        amount: 委托数量（可选）
        price: 委托价格（仅限价模式，可选）
        price_type: 'limit'=限价(默认), 'market'=市价
        confirm: 'true'=自动确认(默认), 'false'=不确认

    示例:
        POST /orders  {"code": "601991", "status": "1", "amount": "100", "price_type": "market"}
        POST /orders  {"code": "600000", "status": "1", "amount": "100", "price": "10.5", "price_type": "limit"}
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    idempotency = IdempotencyChecker.get_instance()

    # 参数提取
    code = get_param("code")
    status = get_param("status")
    amount = get_param("amount")
    price = get_param("price")
    price_type = (get_param("price_type") or "limit").lower()
    confirm_str = (get_param("confirm") or "true").lower()

    # 参数校验
    if not code:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "code 参数不能为空", request_id,
            "请提供股票代码，如: POST /orders {\"code\": \"601991\"}"
        )
    if not status:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "status 参数不能为空", request_id,
            "status=1 买入, status=2 卖出"
        )
    if status not in ("1", "2"):
        return error_response(
            ErrorCode.VALIDATION_ERROR, "status 参数错误", request_id,
            "status 只能是 1(买入) 或 2(卖出)"
        )
    if price_type not in ("limit", "market"):
        return error_response(
            ErrorCode.VALIDATION_ERROR, "price_type 参数错误", request_id,
            "price_type 只能是 limit(限价) 或 market(市价)"
        )
    if price_type == "market" and price:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "市价模式下不能指定 price 参数", request_id,
            "市价模式下由系统自动以最优价格成交，无需指定价格"
        )
    if price_type == "limit" and price is not None:
        try:
            price_float = float(price)
            if round(price_float, 2) != price_float:
                return error_response(
                    ErrorCode.VALIDATION_ERROR, "价格格式错误", request_id,
                    f"A 股价格最多 2 位小数，传入价格 '{price}' 有 {len(price.split('.')[1]) if '.' in price else 0} 位小数"
                )
        except ValueError:
            return error_response(
                ErrorCode.VALIDATION_ERROR, "价格格式无效", request_id,
                f"价格 '{price}' 不是有效的数字格式"
            )
    if confirm_str not in ("true", "false"):
        return error_response(
            ErrorCode.VALIDATION_ERROR, "confirm 参数错误", request_id,
            "confirm 只能是 true(自动确认) 或 false(不确认)"
        )

    confirm = (confirm_str == "true")

    # 幂等检查
    try:
        idempotency.check_and_record(code, status, amount, price, price_type)
    except ApiError as e:
        return error_response(
            e.error_code, e.message, request_id,
            e.suggestion, e.details
        )

    # 提交任务到队列
    _start = time.time()
    try:
        order_timeout = config.get_task_queue_config().get("watchdog_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: _get_trader().place_order(
                code=code, status=status, amount=amount,
                price=price, price_type=price_type, confirm=confirm
            ),
            task_name="place_order",
            params={
                "code": code, "status": status, "amount": amount,
                "price": price, "price_type": price_type, "confirm": confirm
            },
            timeout=order_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        if not isinstance(e, TaskTimeoutError):
            idempotency.clear_record(code, status, amount, price, price_type)
        return error_response_from_exception(e, request_id)


@order_bp.route("/orders/cancel-all", methods=["POST"])
def cancel_all_orders():
    """撤单

    参数（JSON body 或 query string 均可）:
        type: 撤单类型
            - 'A' 或不传: 全部撤单
            - 'X': 撤买
            - 'C': 撤卖

    示例:
        POST /orders/cancel-all                  # 全部撤单
        POST /orders/cancel-all  {"type": "X"}   # 撤买
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    cancel_type = get_param("type") or "A"

    _start = time.time()
    try:
        from src.services.trading_service import TradingService
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        result = task_queue.submit(
            func=lambda: TradingService(WindowService()).cancel_all_orders(cancel_type),
            task_name="cancel_all_orders",
            params={"type": cancel_type},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


