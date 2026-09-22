"""下单/撤单类路由 Blueprint

包含: 下单、一键清仓、撤单、确认委托
"""
import time
from typing import Optional

from flask import Blueprint, request

from src.api.helpers import get_param
from src.api.idempotency import IdempotencyChecker, should_keep_record_on_error
from src.api.response import (
    generate_request_id,
    success_response, error_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.constants import CANCEL_TYPE_MAP
from src.core.trader import Trader
from src.exceptions import ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.window_service import WindowService
from src.utils.logger import Logger

order_bp = Blueprint("order", __name__)


def _get_trader() -> Trader:
    return Trader(WindowService())


def _maybe_verify_entrust_no(config: AppConfig, task_queue: TaskQueue, result: dict) -> dict:
    """entrust_no 可选自动对账（order.verify_entrust_no 门控）

    横幅截获的委托号与券商最终落表号在极端场景（服务器维护窗口）可能
    不一致（2026-09 模拟盘实测，README 有契约说明）。开启本选项后，
    下单成功且拿到委托号时自动追加一笔当日委托查询核对——链式入队，
    与其他任务同队列串行，不破坏 worker 单线程假设。

    对账结果附加到 entrust_no_verified 字段：
    - True  = 横幅号在当日委托落表号中命中
    - False = 未命中（横幅号可能不准，应以查询落表为准）
    - None  = 对账查询失败（网络/窗口/验证码等），下单结果语义不变

    对账绝不改变下单成败判定，只附加信息；开启后接口总耗时增加一次
    查询（~6s，队列繁忙时更久），调用方 timeout 需相应放大。
    """
    order_cfg = config.get_order_config()
    if not (order_cfg.get("verify_entrust_no")
            and result.get("confirmed") and result.get("entrust_no")):
        return result

    entrust_no = str(result["entrust_no"])
    logger = Logger.get_instance()
    try:
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        orders = task_queue.submit(
            func=lambda: PositionService(WindowService()).get_today_orders(),
            task_name="get_today_orders",
            params={"verify_entrust_no": entrust_no},
            timeout=query_timeout,
        )
        booked = {
            str(row[key]).strip()
            for row in (orders or []) if isinstance(row, dict)
            for key in ("合同编号", "委托编号") if row.get(key)
        }
        result["entrust_no_verified"] = entrust_no in booked
        if not result["entrust_no_verified"]:
            logger.warning(
                f"entrust_no 对账未命中: 横幅号 {entrust_no} 不在当日委托"
                f"落表号中（以 GET /orders/pending 查询为准）"
            )
        else:
            logger.info(f"entrust_no 对账命中: {entrust_no}")
    except Exception as e:
        result["entrust_no_verified"] = None
        logger.warning(f"entrust_no 对账查询失败（不影响下单结果）: {e}")
    return result


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

    幂等:
        可选请求头 Idempotency-Key（≤128 字符）——提供时按它去重（60s 窗口），
        HTTP 超时重试带同一 key 即不会被 DUPLICATE_ORDER 误拦也不会重复下单；
        缺省按 参数指纹 去重（60s 内相同 code+status+amount+price+price_type 拒绝）。
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

    # 客户端幂等键（可选）：Idempotency-Key 请求头优先于参数指纹去重。
    # HTTP 超时重试时携带同一 key 即可安全重试，且不同策略同参数不再互撞。
    idem_key = (request.headers.get("Idempotency-Key") or "").strip() or None
    if idem_key and len(idem_key) > 128:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "Idempotency-Key 过长", request_id,
            "Idempotency-Key 请求头最长 128 字符"
        )

    # 幂等检查
    try:
        idempotency.check_and_record(code, status, amount, price, price_type,
                                     idem_key=idem_key)
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
        result = _maybe_verify_entrust_no(config, task_queue, result)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        # 任务可能仍在执行/排队（看门狗超时、队列超时）时保留幂等记录，
        # 防止客户端立即重试导致重复下单
        if not should_keep_record_on_error(e):
            idempotency.clear_record(code, status, amount, price, price_type,
                                     idem_key=idem_key)
        return error_response_from_exception(e, request_id)


@order_bp.route("/orders/cancel-all", methods=["POST"])
def cancel_all_orders():
    """撤单

    参数（JSON body 或 query string 均可）:
        type: 撤单类型
            - 'A' 或不传: 全部撤单
            - 'X': 撤买
            - 'C': 撤卖
            - 'L': 撤最后（撤销最近一笔委托）

    示例:
        POST /orders/cancel-all                  # 全部撤单
        POST /orders/cancel-all  {"type": "X"}   # 撤买
    """
    request_id = generate_request_id()
    config = AppConfig()
    task_queue = TaskQueue.get_instance()
    cancel_type = (get_param("type") or "A").upper()

    # 参数校验前置到路由层（与 /orders 对齐）：非法类型不再入队后
    # 在服务层报错，队列资源不被无效任务占用
    if cancel_type not in CANCEL_TYPE_MAP:
        return error_response(
            ErrorCode.VALIDATION_ERROR, f"无效的撤单类型: {cancel_type}", request_id,
            "type 只能是 A(全部)/X(撤买)/C(撤卖)/L(撤最后)，或不传默认全部撤单"
        )

    _start = time.time()
    try:
        from src.services.trading_service import TradingService
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = task_queue.submit(
            func=lambda: TradingService(WindowService()).cancel_all_orders(cancel_type),
            task_name="cancel_all_orders",
            params={"type": cancel_type},
            timeout=query_timeout
        )
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


