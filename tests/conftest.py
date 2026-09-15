"""测试全局配置

模块导入期即重定向日志文件：pytest 收集 tests/ 下测试模块前先加载本
文件，此后任何单例（Logger/AppConfig/TaskQueue）在测试中首次初始化时，
日志已指向临时目录——生产 logs/app.log 不被 pytest 运行污染（此前实测：
配置加载/虚假连续失败告警混入生产日志，掩盖真实信号）。
"""
import os
import tempfile

_TEST_LOG_DIR = tempfile.mkdtemp(prefix="xiadan-gateway-test-logs-")
os.environ.setdefault(
    "XIADAN_LOG_FILE", os.path.join(_TEST_LOG_DIR, "app.log"))
