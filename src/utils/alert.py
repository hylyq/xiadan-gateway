"""告警 webhook 外推

无人值守场景下，连续任务失败、下单弹窗行为漂移（客户端快速交易设置
被重置）、任务看门狗超时等告警只写日志时无人看见——本模块把告警
POST 到配置的 webhook：企业微信群机器人、钉钉自定义机器人（text 格式）
或自建 receiver（generic 格式）均可接。

设计:
- 未配置 alerts.webhook_url 即整体禁用（默认关闭，向后兼容）
- 每次告警实时读配置，POST /admin/reload-config 热更新即时生效
- 后台 daemon 线程发送：绝不阻塞 worker/告警调用方，绝不抛异常
"""
import json
import threading
import time
import urllib.request

from src.models.config import AppConfig
from src.utils.logger import Logger


def send_alert(alert_type: str, title: str, message: str,
               details: dict = None, level: str = "warning") -> None:
    """发送告警到配置的 webhook（非阻塞，绝不抛异常）

    Args:
        alert_type: 告警类型标识
            （consecutive_failures / order_dialog_drift / task_timeout）
        title: 简短标题
        message: 正文
        details: 结构化附加信息（进 generic payload / text 末行）
        level: 级别（warning / error / info）
    """
    try:
        cfg = AppConfig().get_alerts_config()
        url = (cfg.get("webhook_url") or "").strip()
        if not url:
            return  # 未配置即禁用
        timeout = float(cfg.get("timeout_seconds", 5) or 5)
        payload = _build_payload(
            cfg.get("format", "generic"),
            alert_type, title, message, details or {}, level)
        if payload is None:
            return
        threading.Thread(
            target=_deliver, args=(url, timeout, payload),
            daemon=True, name=f"alert-{alert_type}").start()
    except Exception as e:
        # 告警是旁路功能，任何异常都不能影响交易主流程
        try:
            Logger.get_instance().warning(f"告警发送调度失败: {e}")
        except Exception:
            pass


def _build_payload(fmt: str, alert_type: str, title: str, message: str,
                   details: dict, level: str) -> dict:
    """按配置构造 webhook payload；未知格式返回 None（不发送）"""
    lines = [f"[xiadan-gateway] {title}", message]
    if details:
        lines.append(json.dumps(details, ensure_ascii=False))
    if fmt == "text":
        # 企业微信群机器人 / 钉钉自定义机器人的文本消息格式（两者同构）
        return {"msgtype": "text", "text": {"content": "\n".join(lines)}}
    if fmt == "feishu":
        # 飞书 / Lark 自定义机器人的文本消息格式（msg_type + content.text，
        # 与企业微信的 msgtype/text 结构不同，不能混用）
        return {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    if fmt == "generic":
        # 完整结构化 JSON（自建 receiver / 其他集成）
        return {
            "service": "xiadan-gateway",
            "alert_type": alert_type,
            "level": level,
            "title": title,
            "message": message,
            "details": details,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return None


def _deliver(url: str, timeout: float, payload: dict) -> None:
    """同步 POST（在后台 daemon 线程中执行），结果只写日志"""
    log = Logger.get_instance()
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(500).decode("utf-8", errors="replace")
        if not 200 <= status < 300:
            log.warning(f"告警 webhook 返回非 2xx: {status}")
            return
        # 机器人平台（飞书/企业微信/钉钉）业务错误也返回 HTTP 200，
        # 必须检查响应体里的业务码，否则签名失效/关键词不匹配会被误报成功
        biz_problem = None
        try:
            body_json = json.loads(body)
            for key in ("code", "errcode"):
                if key in body_json and body_json[key] != 0:
                    biz_problem = f"{key}={body_json[key]} {body_json.get('msg', '')}"
                    break
        except (ValueError, AttributeError):
            pass
        if biz_problem:
            log.warning(f"告警 webhook 业务错误: {biz_problem}｜响应: {body[:200]}")
        else:
            # generic 取 alert_type；text/feishu 取正文首行（[xiadan-gateway] 标题）
            display = payload.get("alert_type")
            if not display:
                text = payload.get("text", {}).get("content") \
                    or payload.get("content", {}).get("text") or ""
                display = text.splitlines()[0] if text else "(unknown)"
            log.info(f"告警已推送: {display} → {url}")
    except Exception as e:
        log.warning(f"告警 webhook 发送失败: {e}")
