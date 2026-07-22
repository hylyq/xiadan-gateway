"""Flask 应用工厂 + 系统路由

系统路由（不入队）: /health, /queue/status, /admin/reload-config
业务路由通过 Blueprint 注册:
- query_bp: 查询类（持仓/资金/成交/委托）
- order_bp: 下单/撤单/清仓/确认
- action_bp: 手动操作/诊断
"""
import hmac

from flask import Flask, request

from src.api.response import (
    ErrorCode, generate_request_id,
    success_response, error_response
)
from src.api.task_queue import TaskQueue
from src.models.config import AppConfig
from src.utils.logger import Logger


def create_app() -> Flask:
    """创建 Flask 应用"""
    from flask_cors import CORS

    config = AppConfig()
    logger = Logger.get_instance()
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
            if request.path in PUBLIC_ENDPOINTS:
                return None
            # 支持 Header: Authorization: Bearer <token> 或 X-API-Key: <token>
            token = None
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            if not token:
                token = request.headers.get("X-API-Key")
            # 也支持 query 参数 ?token=xxx（便于浏览器/curl 快速测试）
            if not token:
                token = request.args.get("token")

            if not token:
                return error_response(
                    ErrorCode.AUTH_REQUIRED, "缺少认证 token", generate_request_id(),
                    "请在请求头携带 Authorization: Bearer <token> 或 X-API-Key: <token>"
                )
            # 使用常量时间比较防止时序攻击
            if not hmac.compare_digest(token, expected_token):
                return error_response(
                    ErrorCode.AUTH_FAILED, "认证 token 无效", generate_request_id()
                )

    # 注册系统路由
    _register_system_routes(app)

    # 注册业务 Blueprint
    from src.api.query_routes import query_bp
    from src.api.order_routes import order_bp
    from src.api.action_routes import action_bp

    app.register_blueprint(query_bp)
    app.register_blueprint(order_bp)
    app.register_blueprint(action_bp)

    logger.info("所有路由注册完成")
    return app


def _register_system_routes(app: Flask) -> None:
    """注册系统级路由（健康检查、队列状态、配置重载）"""

    config = AppConfig()
    task_queue = TaskQueue.get_instance()

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
        trading_paths = config.get_trading_app_paths()
        if trading_paths:
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
            "trading_app_paths": trading_paths,
            "queue_status": task_queue.get_status(),
            "config": {
                "watchdog_timeout_seconds": watchdog_timeout,
                "recovery_time_seconds": recovery_time,
                "recommended_client_timeout_seconds": watchdog_timeout + recovery_time + 5
            }
        })

    @app.route("/queue/status", methods=["GET"])
    def queue_status():
        """获取任务队列状态"""
        return success_response(task_queue.get_status())

    @app.route("/admin/reload-config", methods=["POST"])
    def reload_config():
        """热重载配置文件

        重新读取 config/app_config.json，无需重启服务。
        注意: trading_app_paths 等路径变更需重启服务才能完全生效。
        """
        result = config.reload()
        return success_response(result, generate_request_id())
