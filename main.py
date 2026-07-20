"""xiadan-gateway 启动入口

启动顺序:
1. 单实例互斥锁检查
2. 加载配置
3. 初始化 Logger
4. 预加载 ddddocr
5. 启动窗口监控
6. 启动任务队列（自动启动 worker 线程）
7. 启动 Flask 服务（主线程）
"""
import sys

import win32api
import win32event
import winerror

from src.api.routes import create_app
from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.window_monitor import WindowMonitor
from src.services.window_service import WindowService
from src.utils.logger import Logger


# 单实例互斥锁名称
MUTEX_NAME = "Global\\xiadan-gateway_Single_Instance_Mutex"


def check_single_instance():
    """检查是否已有实例在运行"""
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    last_error = win32api.GetLastError()
    if last_error == winerror.ERROR_ALREADY_EXISTS:
        print("程序已在运行，请勿重复启动", file=sys.stderr)
        sys.exit(0)
    return mutex


def main():
    # 1. 单实例检查
    mutex = check_single_instance()

    try:
        # 2. 加载配置
        config = AppConfig()
        logger = Logger.get_instance()

        logger.info("=" * 60)
        logger.info("xiadan-gateway 启动中...")
        logger.info("=" * 60)

        # 3. 检查 xiadan.exe 路径
        trading_path = config.get_trading_app_path()
        if not trading_path:
            logger.warning(
                "未配置 xiadan.exe 路径！请在 config/app_config.json 中设置 trading_app_path"
            )
        else:
            logger.info(f"交易程序路径: {trading_path}")

        # 4. 预加载 ddddocr
        ocr_config = config.get_ocr_config()
        if ocr_config.get("warmup_on_start", True):
            logger.info("预加载 ddddocr...")
            ocr = OcrService.get_instance()
            if not ocr.warmup():
                logger.warning(
                    "ddddocr 预加载失败，查询持仓/成交时若遇到验证码将无法处理"
                )

        # 5. 启动窗口监控
        monitor_config = config.get_window_monitor_config()
        if monitor_config.get("enabled", True) and trading_path:
            monitor = WindowMonitor(check_interval=monitor_config.get("check_interval", 5))
            monitor.start(trading_path)
        else:
            logger.info("窗口监控已禁用")
            monitor = None

        # 6. 启动 Flask 服务（任务队列在第一次请求时初始化）
        app = create_app()

        # 获取服务监听配置
        host = config.get_host()
        port = config.get_port()

        # 获取任务队列配置
        queue_config = config.get_task_queue_config()
        watchdog_timeout = queue_config.get("watchdog_timeout_seconds", 30)

        # 认证状态提示
        auth_config = config.get_auth_config()
        auth_enabled = auth_config.get("enabled", False) and bool(auth_config.get("token"))

        logger.info("-" * 60)
        logger.info("xiadan-gateway 启动完成")
        logger.info(f"HTTP 服务: http://{host}:{port}")
        logger.info(f"健康检查: GET  http://{host}:{port}/health")
        logger.info(f"认证已启用: {auth_enabled}")
        logger.info(f"下单接口: POST http://{host}:{port}/orders")
        logger.info(f"持仓查询: GET  http://{host}:{port}/positions")
        logger.info(f"资金查询: GET  http://{host}:{port}/account/balance")
        logger.info(f"撤单接口: POST http://{host}:{port}/orders/cancel-all")
        logger.info(f"队列状态: GET  http://{host}:{port}/queue/status")
        logger.info(f"看门狗超时: {watchdog_timeout}s, 推荐客户端 timeout: {watchdog_timeout + 10}s")
        logger.info("-" * 60)

        # threaded=True 让不入队的快速接口（/health 等）可并行响应
        # TaskQueue 仍保证下单/撤单/查询串行执行
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    finally:
        if mutex:
            win32api.CloseHandle(mutex)


def dev():
    """开发模式（热加载）"""
    import hupper
    print("开发模式：热加载已启用")
    reloader = hupper.start_reloader("main.main")
    reloader.watch_files("**/*.py")


if __name__ == "__main__":
    if "--dev" in sys.argv:
        dev()
    else:
        main()
