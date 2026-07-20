"""Flask 路由定义

所有 API 端点定义在此文件。
通过 TaskQueue 提交任务，保证顺序执行。
"""
import time
from typing import Optional

import pyautogui
from flask import Flask, request

from src.api.idempotency import IdempotencyChecker
from src.api.response import (
    ApiError, ErrorCode, generate_request_id,
    success_response, error_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.core.trader import Trader
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.trading_service import TradingService
from src.services.window_monitor import WindowMonitor
from src.services.window_service import WindowService
from src.utils.logger import Logger


def create_routes(app: Flask) -> None:
    """注册所有路由"""

    # 获取共享服务实例
    config = AppConfig()
    logger = Logger.get_instance()
    window_service = WindowService()
    task_queue = TaskQueue.get_instance()
    idempotency = IdempotencyChecker.get_instance()

    # 这些会在第一次请求时延迟初始化（因为依赖 OCR）
    _trader = None
    _position_service = None
    _trading_service = None

    def get_trader() -> Trader:
        nonlocal _trader
        if _trader is None:
            _trader = Trader(window_service)
        return _trader

    def get_trading_service() -> TradingService:
        nonlocal _trading_service
        if _trading_service is None:
            _trading_service = TradingService(window_service)
        return _trading_service

    def get_position_service() -> PositionService:
        nonlocal _position_service
        if _position_service is None:
            # 延迟导入避免循环依赖
            from src.core.ocr import OcrService
            _position_service = PositionService(window_service, OcrService.get_instance())
        return _position_service

    def _get_param(name: str, default: Optional[str] = None) -> Optional[str]:
        """统一参数获取：优先 JSON body，回退 query string

        便于 POST 接口既支持 application/json 调用，也支持 curl 简单测试。
        """
        # JSON body
        body = request.get_json(silent=True)
        if isinstance(body, dict) and name in body:
            val = body.get(name)
            return val if val is None else str(val)
        # query string
        return request.args.get(name, default)

    # ------------------------------------------------------------
    # 健康检查（不入队）
    # ------------------------------------------------------------

    @app.route("/health", methods=["GET"])
    def health_check():
        """健康检查

        返回服务状态和推荐配置（包括调用方推荐的 timeout）
        """
        queue_config = config.get_task_queue_config()
        watchdog_timeout = queue_config.get("watchdog_timeout_seconds", 30)
        recovery_time = 5  # 恢复流程预估耗时

        # 检查 xiadan.exe 是否在运行
        xiadan_running = False
        trading_path = config.get_trading_app_path()
        if trading_path:
            try:
                import psutil
                for proc in psutil.process_iter(["name"]):
                    if proc.info["name"] and proc.info["name"].lower() == "xiadan.exe":
                        xiadan_running = True
                        break
            except Exception:
                pass

        return success_response({
            "service": "xiadan-gateway",
            "version": "1.0.0",
            "xiadan_running": xiadan_running,
            "trading_app_path": trading_path,
            "queue_status": task_queue.get_status(),
            "config": {
                "watchdog_timeout_seconds": watchdog_timeout,
                "recovery_time_seconds": recovery_time,
                "recommended_client_timeout_seconds": watchdog_timeout + recovery_time + 5
            }
        })

    # ------------------------------------------------------------
    # 队列状态（不入队）
    # ------------------------------------------------------------

    @app.route("/queue/status", methods=["GET"])
    def queue_status():
        """获取任务队列状态"""
        return success_response(task_queue.get_status())

    # ------------------------------------------------------------
    # 资金余额
    # ------------------------------------------------------------

    @app.route("/account/balance", methods=["GET"])
    def get_balance():
        """获取资金余额

        通过 control_id 批量读取（无需 OCR），速度快。
        """
        request_id = generate_request_id()
        try:
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            result = task_queue.submit(
                func=lambda: get_position_service().get_balance(),
                task_name="get_balance",
                params={},
                timeout=query_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 持仓查询
    # ------------------------------------------------------------

    @app.route("/positions", methods=["GET"])
    def get_position():
        """获取当前持仓

        流程: F4 + Ctrl+C + 剪切板解析，可能需要 OCR 验证码。
        """
        request_id = generate_request_id()
        try:
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            result = task_queue.submit(
                func=lambda: get_position_service().get_position(),
                task_name="get_position",
                params={},
                timeout=query_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 今日成交
    # ------------------------------------------------------------

    @app.route("/trades/today", methods=["GET"])
    def get_today_trades():
        """获取今日成交

        流程: 树形菜单导航到"当日成交" + Ctrl+C + 剪切板解析。
        """
        request_id = generate_request_id()
        try:
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            result = task_queue.submit(
                func=lambda: get_position_service().get_today_trades(),
                task_name="get_today_trades",
                params={},
                timeout=query_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 当日委托
    # ------------------------------------------------------------

    @app.route("/orders/pending", methods=["GET"])
    def get_today_orders():
        """获取当日委托

        返回当日所有委托记录，含状态（待报、已报、部成、已成、已撤、部撤等）。
        流程: 树形菜单导航到"当日委托" + Ctrl+C + 剪切板解析。
        """
        request_id = generate_request_id()
        try:
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            result = task_queue.submit(
                func=lambda: get_position_service().get_today_orders(),
                task_name="get_today_orders",
                params={},
                timeout=query_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 一键清仓
    # ------------------------------------------------------------

    @app.route("/orders/sell-all", methods=["POST"])
    def sell_all():
        """一键清仓（以市价卖出指定股票全部可用持仓）

        参数（JSON body 或 query string 均可）:
            code: 股票代码（必填）
            confirm: 'true'=自动确认(默认), 'false'=不确认

        流程:
            1. 查询当前持仓
            2. 按 code 匹配到对应股票
            3. 提取可用余额
            4. 按市价提交卖出委托

        示例:
            POST /orders/sell-all  {"code": "000656"}
        """
        request_id = generate_request_id()
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

        try:
            # 1. 查询持仓
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            positions = task_queue.submit(
                func=lambda: get_position_service().get_position(),
                task_name="get_position_for_sell_all",
                params={"code": code},
                timeout=query_timeout
            )
        except Exception as e:
            return error_response_from_exception(e, request_id)

        # 2. 按 code 定位持仓
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

        # 3. 提取可用余额
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

        # 4. 提交市价卖出委托
        logger.info(
            f"一键清仓: code={code}, 可用余额={available_qty_int}, confirm={confirm}"
        )

        try:
            order_timeout = config.get_task_queue_config().get("watchdog_timeout_seconds", 30)
            result = task_queue.submit(
                func=lambda: get_trader().place_order(
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
            return success_response(result, request_id)
        except Exception as e:
            # 下单失败清除幂等记录
            from src.api.response import TaskTimeoutError
            if not isinstance(e, TaskTimeoutError):
                idempotency.clear_record(code, "2", str(available_qty_int), None, "market")
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------

    @app.route("/orders", methods=["POST"])
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
                # 检查是否超过 2 位小数
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
        try:
            order_timeout = config.get_task_queue_config().get("watchdog_timeout_seconds", 30)
            result = task_queue.submit(
                func=lambda: get_trader().place_order(
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
            return success_response(result, request_id)
        except Exception as e:
            # 下单失败时清除幂等记录，允许客户端重试
            # 注意：TaskTimeoutError 不清除记录，因为超时可能已部分提交订单，
            # 客户端应通过 /today_trades 确认状态后再决定是否重试
            from src.api.response import TaskTimeoutError
            if not isinstance(e, TaskTimeoutError):
                idempotency.clear_record(code, status, amount, price, price_type)
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------

    @app.route("/orders/cancel-all", methods=["POST"])
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
        cancel_type = _get_param("type") or "A"

        try:
            query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 15)
            result = task_queue.submit(
                func=lambda: get_trading_service().cancel_all_orders(cancel_type),
                task_name="cancel_all_orders",
                params={"type": cancel_type},
                timeout=query_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 单独确认委托（用于 confirm=false 的后续确认）
    # ------------------------------------------------------------

    @app.route("/orders/confirm", methods=["POST"])
    def confirm_order():
        """发送 Y 键确认委托

        用于 POST /orders {"confirm": "false"} 后的单独确认。
        """
        request_id = generate_request_id()
        try:
            confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
            result = task_queue.submit(
                func=lambda: get_trader().confirm_order(),
                task_name="confirm_order",
                params={},
                timeout=confirm_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    # ------------------------------------------------------------
    # 手动操作接口
    # ------------------------------------------------------------

    @app.route("/actions/send-key", methods=["POST"])
    def send_key():
        """手动发送按键

        参数（JSON body 或 query string 均可）:
            key: 按键，如 F1, F2, Y, {CTRL+C}

        示例:
            POST /actions/send-key  {"key": "F1"}
            POST /actions/send-key  {"key": "{CTRL+C}"}
        """
        request_id = generate_request_id()
        key = _get_param("key")
        if not key:
            return error_response(
                ErrorCode.VALIDATION_ERROR, "key 参数不能为空", request_id,
                "示例: POST /actions/send-key {\"key\": \"F1\"}"
            )

        try:
            confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
            result = task_queue.submit(
                func=lambda: window_service.send_key(key) or {"key": key, "sent": True},
                task_name="send_key",
                params={"key": key},
                timeout=confirm_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    @app.route("/actions/click", methods=["POST"])
    def click():
        """鼠标点击坐标

        参数（JSON body 或 query string 均可）:
            x: 横坐标
            y: 纵坐标

        示例:
            POST /actions/click  {"x": 100, "y": 200}
        """
        request_id = generate_request_id()
        x = _get_param("x")
        y = _get_param("y")

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

        try:
            confirm_timeout = config.get_task_queue_config().get("confirm_timeout_seconds", 10)
            result = task_queue.submit(
                func=lambda: (pyautogui.click(x_int, y_int), {"x": x_int, "y": y_int})[1],
                task_name="click",
                params={"x": x_int, "y": y_int},
                timeout=confirm_timeout
            )
            return success_response(result, request_id)
        except Exception as e:
            return error_response_from_exception(e, request_id)

    logger.info("所有路由注册完成")


# ============================================================
# Flask 应用工厂
# ============================================================

def create_app() -> Flask:
    """创建 Flask 应用"""
    from flask_cors import CORS

    config = AppConfig()
    auth_config = config.get_auth_config()
    auth_enabled = auth_config.get("enabled", False)
    expected_token = auth_config.get("token", "")
    # /health 始终公开，便于监控探活
    PUBLIC_ENDPOINTS = {"/health"}

    app = Flask(__name__)
    CORS(app)
    app.config["JSON_AS_ASCII"] = False

    # 认证中间件
    if auth_enabled and expected_token:
        @app.before_request
        def _check_auth():
            from flask import request as _req
            if _req.path in PUBLIC_ENDPOINTS:
                return None
            # 支持 Header: Authorization: Bearer <token> 或 X-API-Key: <token>
            token = None
            auth_header = _req.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            if not token:
                token = _req.headers.get("X-API-Key")
            # 也支持 query 参数 ?token=xxx（便于浏览器/curl 快速测试）
            if not token:
                token = _req.args.get("token")

            if not token:
                return error_response(
                    ErrorCode.AUTH_REQUIRED, "缺少认证 token", generate_request_id(),
                    "请在请求头携带 Authorization: Bearer <token> 或 X-API-Key: <token>"
                )
            if token != expected_token:
                return error_response(
                    ErrorCode.AUTH_FAILED, "认证 token 无效", generate_request_id()
                )

    create_routes(app)
    return app
