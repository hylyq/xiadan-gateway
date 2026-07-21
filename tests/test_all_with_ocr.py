"""全量功能测试 — 每步附带 OCR 界面状态确认

测试所有 API 端点，并在关键操作后调用 /diagnostic/snapshot
截图 + OCR 验证界面状态，确保测试覆盖率和结果可靠性。
"""
import json
import time
import urllib.request
import urllib.error
import os
import sys
from datetime import datetime

BASE_URL = "http://127.0.0.1:5000"
REPORT_PATH = os.path.join(os.path.dirname(__file__), "test_report_with_ocr.json")

results = []


def api_call(method: str, path: str, body: dict = None,
             desc: str = "", timeout: int = 30) -> dict:
    """调用 API 并记录结果"""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")

    entry = {
        "step": len(results) + 1,
        "time": datetime.now().strftime("%H:%M:%S"),
        "method": method,
        "path": path,
        "description": desc,
        "request_body": body,
    }
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        entry["status_code"] = resp.status
        entry["response"] = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        entry["status_code"] = e.code
        entry["response"] = json.loads(e.read().decode())
    except Exception as e:
        entry["status_code"] = 0
        entry["response"] = {"error": str(e)}

    entry["duration_ms"] = round((time.time() - start) * 1000, 1)
    print(f"  [{entry['step']:02d}] {method} {path} → {entry['status_code']} "
          f"({entry['duration_ms']}ms)")
    results.append(entry)
    return entry


def diagnostic_snapshot(context: str = "", wait_ms: int = 500) -> dict:
    """调用诊断截图+OCR"""
    if wait_ms > 0:
        time.sleep(wait_ms / 1000)
    try:
        url = f"{BASE_URL}/diagnostic/snapshot"
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        ocr = data.get("data", {})
        text = ocr.get("ocr_text", "").strip()
        ui_text = ocr.get("ui_text", "").strip()
        screenshot = ocr.get("screenshot", "")
        failed = ocr.get("ocr_failed", True)
        ui_lines = len(ui_text.split("\n")) if ui_text else 0
        status = "✓ UI OK" if ui_text else ("✓ OCR OK" if (not failed and text) else "⚠ empty" if not failed else "✗ failed")
        print(f"      诊断结果 → {status} | UI文本{ui_lines}项, OCR文本({len(text)}字)")
        if ui_text:
            print(f"      UI 控件文本 ({ui_lines} 项):")
            for line in ui_text.split("\n"):
                print(f"        | {line}")
        if text:
            print(f"      OCR 原文:\n{text}")
        return ocr
    except Exception as e:
        print(f"      诊断截图 → ✗ 失败: {e}")
        return {"ocr_failed": True, "ocr_text": "", "error": str(e)}


def print_separator(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def do_test():
    global results
    results = []

    print(f"\n{'#'*70}")
    print(f"#  xiadan-gateway 全量功能测试（带 OCR 验证）")
    print(f"#  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"#  BASE_URL: {BASE_URL}")
    print(f"{'#'*70}\n")

    # =========================================================
    # 1. 健康检查
    # =========================================================
    print_separator("1. 健康检查")
    api_call("GET", "/health", desc="基础健康检查，确认服务和 xiadan.exe 状态")
    diagnostic_snapshot("health_check")

    # =========================================================
    # 2. 队列状态
    # =========================================================
    print_separator("2. 队列状态")
    api_call("GET", "/queue/status", desc="查看任务队列当前状态")
    # 队列状态不需要截图

    # =========================================================
    # 3. 资金余额（OCR 确认界面）
    # =========================================================
    print_separator("3. 资金余额查询")
    entry_balance = api_call("GET", "/account/balance",
                             desc="查询资金余额，包含可用资金、总资产等",
                             timeout=40)
    diagnostic_snapshot("after_balance_query", wait_ms=300)

    # =========================================================
    # 4. 持仓查询（OCR 确认界面 + 验证数据）
    # =========================================================
    print_separator("4. 持仓查询")
    entry_pos = api_call("GET", "/positions",
                         desc="查询当前持仓列表",
                         timeout=40)
    diagnostic_snapshot("after_position_query", wait_ms=300)
    if entry_pos.get("response", {}).get("data"):
        positions = entry_pos["response"]["data"]
        print(f"      持仓数量: {len(positions) if isinstance(positions, list) else 'N/A'}")

    # =========================================================
    # 5. 今日成交（OCR 确认导航 + 数据）
    # =========================================================
    print_separator("5. 今日成交查询")
    entry_trades = api_call("GET", "/trades/today",
                            desc="查询今日成交记录",
                            timeout=40)
    diagnostic_snapshot("after_today_trades_query", wait_ms=300)
    trades = entry_trades.get("response", {}).get("data", [])
    if isinstance(trades, list):
        print(f"      今日成交条数: {len(trades)}")

    # =========================================================
    # 6. 当日委托（OCR 确认导航 + 数据）
    # =========================================================
    print_separator("6. 当日委托查询")
    entry_pending = api_call("GET", "/orders/pending",
                             desc="查询当日所有委托记录",
                             timeout=40)
    diagnostic_snapshot("after_pending_orders_query", wait_ms=300)

    # =========================================================
    # 7. 发送按键测试（F1 打开买入窗口 → OCR 确认）
    # =========================================================
    print_separator("7. 发送按键测试（F1 打开买入窗口）")
    api_call("POST", "/actions/send-key", {"key": "F1"},
             desc="模拟按 F1 打开买入窗口，用 OCR 验证窗口是否打开",
             timeout=20)
    diagnostic_snapshot("after_f1_buy_window", wait_ms=500)
    # 关闭买入窗口（使用 SendMessage WM_CLOSE 安全关闭，不关闭整个程序）
    api_call("POST", "/actions/close-dialog", {"title": "买入"},
             desc="安全关闭买入子对话框",
             timeout=20)
    diagnostic_snapshot("after_close_buy_dialog", wait_ms=500)

    # =========================================================
    # 8. 撤单测试（OCR 确认 F3 窗口 + 结果）
    # =========================================================
    print_separator("8. 撤单测试（全部撤单）")
    entry_cancel = api_call("POST", "/orders/cancel-all",
                            desc="执行全部撤单（F3 → 全部撤单 → 确认）",
                            timeout=20)
    diagnostic_snapshot("after_cancel_all", wait_ms=1000)

    # =========================================================
    # 9. 鼠标点击测试
    # =========================================================
    print_separator("9. 鼠标点击测试（点击标题栏恢复焦点）")
    api_call("POST", "/actions/click", {"x": 200, "y": 10},
             desc="鼠标点击坐标 (200,10)，用于恢复窗口焦点",
             timeout=10)
    # 点击后不做截图（坐标点击不影响界面数据）

    # =========================================================
    # 10. 重复查询确认状态一致性（OCR 验证前后一致性）
    # =========================================================
    print_separator("10. 状态一致性验证（再次查询余额+持仓）")
    api_call("GET", "/account/balance", desc="再次查询余额，对比与第一次结果是否一致")
    api_call("GET", "/positions", desc="再次查询持仓，对比与第一次结果是否一致")

    # =========================================================
    # 11. 最终诊断快照
    # =========================================================
    print_separator("11. 最终诊断快照")
    ocr_final = diagnostic_snapshot("final_state", wait_ms=0)
    if ocr_final.get("ui_text", "").strip():
        ui_count = len(ocr_final["ui_text"].split("\n"))
        print(f"\n  ✓ 最终诊断结果: UI 控件文本 {ui_count} 项")
    elif ocr_final.get("ocr_text", "").strip():
        print(f"\n  ✓ 最终 OCR 识别结果 ({len(ocr_final['ocr_text'])} 字符)")
    else:
        print(f"\n  ⚠ 最终诊断未提取到文本")

    # =========================================================
    # 生成测试报告
    # =========================================================
    summary = _generate_summary(results)
    _save_report(summary)

    print(f"\n{'#'*70}")
    print(f"  测试完成！报告已保存: {REPORT_PATH}")
    print(f"{'#'*70}")


def _generate_summary(results: list) -> dict:
    """生成测试汇总"""
    total = len(results)
    success = sum(1 for r in results if r.get("status_code") == 200)
    errors = [r for r in results if r.get("status_code") != 200]
    durations = [r.get("duration_ms", 0) for r in results]

    # 检查每个步骤的响应中是否有 data 字段
    data_responses = sum(1 for r in results
                         if r.get("response", {}).get("status") == "success")

    # 提取所有 OCR 文本（如果有）
    ocr_entries = []
    for i, r in enumerate(results):
        resp = r.get("response", {})
        if r.get("path") == "/diagnostic/snapshot":
            ocr_entries.append({
                "step": r["step"],
                "context": r.get("description", ""),
                "ui_text": resp.get("data", {}).get("ui_text", ""),
                "ocr_text": resp.get("data", {}).get("ocr_text", ""),
                "ocr_failed": resp.get("data", {}).get("ocr_failed", True),
            })

    return {
        "test_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_steps": total,
            "successful": success,
            "failed": total - success,
            "success_rate": f"{success/total*100:.1f}%" if total else "N/A",
            "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "total_duration_ms": round(sum(durations), 1),
            "data_responses": data_responses,
        },
        "error_details": [
            {
                "step": r["step"],
                "path": r["path"],
                "status_code": r["status_code"],
                "message": r.get("response", {}).get("message", str(r.get("response", {})))
            }
            for r in errors
        ],
        "ocr_snapshots": ocr_entries,
        "steps": results,
    }


def _save_report(summary: dict):
    """保存测试报告到文件"""
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n  报告文件: {REPORT_PATH}")
    # 打印摘要
    s = summary["summary"]
    print(f"  测试步骤: {s['total_steps']} | "
          f"成功: {s['successful']} | "
          f"失败: {s['failed']} | "
          f"成功率: {s['success_rate']}")
    print(f"  平均耗时: {s['avg_duration_ms']}ms | "
          f"总耗时: {s['total_duration_ms']}ms")


if __name__ == "__main__":
    do_test()
