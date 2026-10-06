"""RDP 重连竞态判别信号采样（③ 可行性实验）

在本会话（与 xiadan.exe 同一 RDP 会话）每秒采样 WTS 会话信号，追加写
logs/probe_session_signals.jsonl。目的：验证会话处于「断开」态时，
WTSClientName / WTSClientProtocolType 等信号能否区分——

  A. 客户端真的走了（自愈该动手）
  B. 用户正在重新连接（挂在凭据界面 / console→RDP 换轨过渡，绝不能动手）

用法：在 RDP 会话内的终端运行（不要在服务/Session 0 里跑，采到的
是别的会话），Ctrl+C 停止：

    uv run python scripts/probe_session_signals.py

采样行示例：
  {"t": "17:55:01", "state": 4, "client_name": "", "protocol": 2}
  state: 0=Active 1=Connected 4=Disconnected（win32ts.WTSConnectState）
  client_name: 客户端计算机名（services 会话实测为空串）
  protocol: 0=console 2=RDP
"""
import json
import os
import time

import win32ts

SAMPLE_INTERVAL = 1.0
OUT = os.path.join("logs", "probe_session_signals.jsonl")

CLASSES = {
    "state": win32ts.WTSConnectState,
    "client_name": win32ts.WTSClientName,
    "protocol": win32ts.WTSClientProtocolType,
}


def sample() -> dict:
    handle = win32ts.WTS_CURRENT_SERVER_HANDLE
    session = win32ts.WTS_CURRENT_SESSION
    row = {"t": time.strftime("%H:%M:%S")}
    for key, info_class in CLASSES.items():
        try:
            v = win32ts.WTSQuerySessionInformation(handle, session, info_class)
            if isinstance(v, tuple):
                v = v[0]
            row[key] = v
        except Exception as e:  # 单信号失败不中断采样
            row[key] = f"ERR:{e}"
    return row


def main() -> None:
    os.makedirs("logs", exist_ok=True)
    print(f"sampling every {SAMPLE_INTERVAL}s -> {OUT}  (Ctrl+C stop)", flush=True)
    last_signals = None
    n = 0
    with open(OUT, "a", encoding="utf-8") as f:
        while True:
            row = sample()
            signals = {k: v for k, v in row.items() if k != "t"}
            changed = signals != last_signals
            if changed or n % 10 == 0:  # 变化即记 + 每 10s 心跳一行
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(("CHANGE " if changed else "beat   ") + json.dumps(
                    row, ensure_ascii=False), flush=True)
                last_signals = signals if changed else last_signals
            n += 1
            time.sleep(SAMPLE_INTERVAL)


if __name__ == "__main__":
    main()
