"""xiadan-gateway 启动入口

启动顺序:
1. 单实例互斥锁检查
2. 加载配置
3. 初始化 Logger
4. 预加载 ddddocr
5. 启动窗口监控
6. 启动 waitress 生产服务器（主线程）
7. 优雅关闭（Ctrl+C 时停止监控、等待队列排空）
"""
import signal
import sys
import threading

import win32api
import win32event
import winerror

from src.api.routes import create_app
from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.window_monitor import WindowMonitor
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

    # 优雅关闭事件
    shutdown_event = threading.Event()
    monitor = None

    try:
        # 2. 加载配置
        config = AppConfig()
        logger = Logger.get_instance()

        logger.info("=" * 60)
        logger.info("xiadan-gateway 启动中...")
        logger.info("=" * 60)

        # 3. 检查 xiadan.exe 路径
        trading_paths = config.get_trading_app_paths()
        if not trading_paths:
            logger.warning(
                "未配置 xiadan.exe 路径！请在 config/app_config.json 中设置 trading_app_paths"
            )
        else:
            logger.info(f"交易程序路径: {trading_paths}")

        # 4. 预热 OCR 引擎（轻量模板匹配；ddddocr 可选）
        ocr_config = config.get_ocr_config()
        if ocr_config.get("warmup_on_start", True):
            ddddocr_enabled = ocr_config.get("ddddocr_enabled", False)
            logger.info("预热 OCR 引擎...")
            ocr = OcrService.get_instance()
            ocr.configure(ddddocr_enabled=ddddocr_enabled)
            if not ocr.warmup():
                logger.warning(
                    "OCR 引擎预热失败，查询持仓/成交时若遇到验证码将无法处理"
                )
            else:
                coverage = len(ocr.lightweight_coverage)
                logger.info(f"OCR 引擎就绪，覆盖 {coverage}/10 个数字")

        # 5. 启动窗口监控
        monitor_config = config.get_window_monitor_config()
        if monitor_config.get("enabled", True) and trading_paths:
            monitor = WindowMonitor(check_interval=monitor_config.get("check_interval", 2))
            monitor.start(trading_paths)
        else:
            logger.info("窗口监控已禁用")

        # 5.5 清理过期截图
        from src.utils.screenshot import ScreenshotUtil
        logging_cfg = config.get_logging_config()
        screenshot_util = ScreenshotUtil(
            logging_cfg.get("screenshot_dir", "logs/screenshots")
        )
        screenshot_util.cleanup_old_screenshots()

        # 6. 创建 Flask 应用
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
        logger.info(f"服务器: waitress (生产级 WSGI)")
        logger.info("-" * 60)

        # 7. 注册优雅关闭信号处理
        def _graceful_shutdown(signum, frame):
            logger.info("\n收到关闭信号，正在优雅停机...")
            shutdown_event.set()
            if monitor:
                monitor.stop()
            logger.info("窗口监控已停止，服务已关闭")
            sys.exit(0)

        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)

        # 8. 启动 waitress 生产服务器
        from waitress import serve
        serve(app, host=host, port=port, threads=4,
              channel_timeout=watchdog_timeout + 15)

    finally:
        if monitor:
            monitor.stop()
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
