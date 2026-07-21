"""下单/撤单类路由 Blueprint

包含: 下单、一键清仓、撤单、确认委托
"""
import time
from typing import Optional

from flask import Blueprint, request

from src.api.idempotency import IdempotencyChecker
from src.api.response import (
    ApiError, ErrorCode, generate_request_id,
    success_response, error_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.core.trader import Trader
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger

order_bp = Blueprint("order", __name__)


def _get_param(name: str, default: Optional[str] = None) -> Optional[str]:
    """统一参数获取：优先 JSON body，回退 query string"""
    body = request.get_json(silent=True)
    if isinstance(body, dict) and name in body:
        val = body.get(name)
        return val if val is None else str(val)
    return request.args.get(name, default)


def _get_trader() -> Trader:
    return Trader(WindowService())


def _get_position_service():
    from src.core.ocr import OcrService
    from src.services.position_service import PositionService
    return PositionService(WindowService(), OcrService.get_instance())


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
    code = _get_param("code")
    status = _get_param("status")
    amount = _get_param("amount")
    price = _get_param("price")
    price_type = (_get_param("price_type") or "limit").lower()
    confirm_str = (_get_param("confirm") or "true").lower()

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
        from src.api.response import TaskTimeoutError
        if not isinstance(e, TaskTimeoutError):
            idempotency.clear_record(code, status, amount, price, price_type)
        return error_response_from_exception(e, request_id)


@order_bp.route("/orders/sell-all", methods=["POST"])
def sell_all():
    """一键清仓（以市价卖出指定股票全部可用持仓）

    参数（JSON body 或 query string 均可）:
        code: 股票代码（必填）
        confirm: 'true'=自动确认(默认), 'false'=不确认

    示例:
        POST /orders/sell-all  {"code": "000656"}
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    idempotency = IdempotencyChecker.get_instance()
    logger = Logger.get_instance()

    code = _get_param("code")
    confirm_str = (_get_param("confirm") or "true").lower()

    if not code:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "code 参数不能为空", request_id,
            "请提供股票代码，如: POST /orders/sell-all {\"code\": \"000656\"}"
        )
    if confirm_str not in ("true", "false"):
        return error_response(
            ErrorCode.VALIDATION_ERROR, "confirm 参数错误", request_id,
            "confirm 只能是 true(自动确认) 或 false(不确认)"
        )
    confirm = (confirm_str == "true")

    _start = time.time()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
        positions = task_queue.submit(
            func=lambda: _get_position_service().get_position(),
            task_name="get_position_for_sell_all",
            params={"code": code},
            timeout=query_timeout
        )
    except Exception as e:
        return error_response_from_exception(e, request_id)

    # 按 code 定位持仓
    position = None
    for pos in positions:
        if pos.get("证券代码", "").strip() == code:
            position = pos
            break
    if position is None:
        return error_response(
            ErrorCode.VALIDATION_ERROR, f"股票 {code} 未在持仓中找到", request_id,
            f"当前持仓中无代码为 {code} 的股票，请先买入或检查股票代码是否正确"
        )

    # 提取可用余额
    available_qty = None
    for header in ("可用余额", "可用数量", "可卖数量", "卖出数量"):
        if header in position:
            val = position[header].strip()
            if val:
                available_qty = str(int(float(val)))
                break

    if available_qty is None:
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            f"无法从持仓数据中提取可用余额（可用字段名: {list(position.keys())}）",
            request_id
        )

    available_qty_int = int(available_qty)
    if available_qty_int <= 0:
        return error_response(
            ErrorCode.VALIDATION_ERROR, f"股票 {code} 无可卖数量", request_id,
            f"当前持仓 {code} 可用余额为 {available_qty} 股，无需卖出"
        )

    # 提交市价卖出委托
    logger.info(f"一键清仓: code={code}, 可用余额={available_qty_int}, confirm={confirm}")

    try:
        order_timeout = config.get_task_queue_config().get("watchdog_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: _get_trader().place_order(
                code=code, status="2",
                amount=str(available_qty_int),
                price=None, price_type="market",
                confirm=confirm
            ),
            task_name="sell_all",
            params={"code": code, "amount": available_qty_int, "price_type": "market"},
            timeout=order_timeout
        )
        result["available_qty"] = available_qty_int
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        from src.api.response import TaskTimeoutError
        if not isinstance(e, TaskTimeoutError):
            idempotency.clear_record(code, "2", str(available_qty_int), None, "market")
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
    cancel_type = _get_param("type") or "A"

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


@order_bp.route("/orders/confirm", methods=["POST"])
def confirm_order():
    """发送 Y 键确认委托

    用于 POST /orders {"confirm": "false"} 后的单独确认。
    """
    _start = time.time()
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    try:
        confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
        result = task_queue.submit(
            func=lambda: _get_trader().confirm_order(),
            task_name="confirm_order",
            params={},
            timeout=confirm_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)
