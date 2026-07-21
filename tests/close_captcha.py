"""关闭残留的验证码弹窗"""
from pywinauto import Desktop

desktop = Desktop(backend="uia")
dialogs = desktop.windows(title="网上股票交易系统5.0")
if dialogs:
    w = dialogs[0]
    found = False
    for ctrl in w.descendants():
        try:
            cid = ctrl.control_id()
            text = ctrl.window_text() or ""
            if cid == 2 or text == "取消":
                print("找到取消按钮: cid=%s text=%s" % (cid, text))
                ctrl.click()
                print("已点击取消")
                found = True
                break
        except:
            pass
    if not found:
        print("未找到取消按钮，尝试 ESC")
        import win32api, win32con, time
        win32api.keybd_event(0x1B, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(0x1B, 0, win32con.KEYEVENTF_KEYUP, 0)
else:
    print("未找到交易窗口")
