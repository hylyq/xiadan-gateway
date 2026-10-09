"""在交互桌面会话内启动进程（Session 0 运维工具，纯 ctypes）

背景：Claude Code 运行在 Session 0（服务会话，无桌面访问权），
网关与 xiadan.exe 活在交互会话。需要「看得见桌面」的探测脚本、
或把网关本体重新拉起时，都走本工具：

1. 从交易窗口（或指定进程）拿目标会话里的 pid，开其 token 副本
2. 用 SeDebugPrivilege 模拟同会话 winlogon（SYSTEM）补足调用方
   SeAssignPrimaryToken（CreateProcessAsUser 校验调用方特权）
3. CreateProcessAsUserW 到 WinSta0\Default——子进程落在交互桌面
4. RevertToSelf

全程纯内存 token 操作，无服务/计划任务等持久化。

用法:
    .venv/Scripts/python.exe scripts/run_in_session.py [--cwd DIR]
        [--stdout FILE] [--session-pid PID] -- <命令行...>

pywin32 的 DuplicateTokenEx 绑定对参数类型吹毛求疵（同一槽位先后
要求 int/PySECURITY_ATTRIBUTES），故 token 链路全部走 ctypes。
"""
import argparse
import ctypes
import sys
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_IMPERSONATE = 0x0004
TOKEN_ADJUST_PRIVILEGES = 0x0020
MAXIMUM_ALLOWED = 0x02000000
SecurityImpersonation = 2
TokenPrimary = 1
TokenImpersonation = 2
CREATE_NO_WINDOW = 0x08000000
SE_PRIVILEGE_ENABLED = 0x00000002


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _sa(inherit=False):
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = inherit
    return sa


def _err(msg):
    raise RuntimeError(f"{msg} failed: GetLastError={ctypes.get_last_error()}")


def enable_privileges(handle, names):
    """在指定 token（调用方自身）上启用指定特权"""
    for name in names:
        luid = LUID()
        if not _advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            continue
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not _advapi32.AdjustTokenPrivileges(
                handle, False, ctypes.byref(tp), 0, None, None):
            _err(f"AdjustTokenPrivileges({name})")
        if ctypes.get_last_error() == 1300:  # ERROR_NOT_ALL_ASSIGNED
            print(f"warn: privilege not held: {name}", file=sys.stderr)


def impersonate_system_of_session(session_pid):
    """模拟目标会话 winlogon（SYSTEM），返回是否成功（调用方负责 RevertToSelf）"""
    sid_buf = wintypes.DWORD()
    _kernel32.ProcessIdToSessionId(session_pid, ctypes.byref(sid_buf))
    import psutil
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() != "winlogon.exe":
            continue
        sid2 = wintypes.DWORD()
        _kernel32.ProcessIdToSessionId(proc.pid, ctypes.byref(sid2))
        if sid2.value != sid_buf.value:
            continue
        h = _kernel32.OpenProcess(0x1000, False, proc.pid)
        if not h:
            continue
        tok = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(
                h, TOKEN_DUPLICATE | TOKEN_QUERY, ctypes.byref(tok)):
            continue
        himp = wintypes.HANDLE()
        if not _advapi32.DuplicateTokenEx(
                tok, MAXIMUM_ALLOWED, _sa(),
                SecurityImpersonation, TokenImpersonation, ctypes.byref(himp)):
            continue
        if not _advapi32.ImpersonateLoggedOnUser(himp):
            continue
        # 复制出的 token 特权默认是「持有但未启用」态——CreateProcessAsUser
        # 的特权检查要求启用态，须在模拟 token 上显式启用
        enable_privileges(himp, ["SeAssignPrimaryTokenPrivilege",
                                 "SeIncreaseQuotaPrivilege"])
        return True
    return False


def find_session_pid(window_title=None, prefer_names=("xiadan.exe", "python.exe")):
    """交互会话里找 token 来源 pid：优先交易窗口所属进程"""
    if window_title:
        hwnd = _user32.FindWindowW(None, window_title)
        if hwnd:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                return pid.value
    import psutil
    for name in prefer_names:
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() != name:
                continue
            sid = wintypes.DWORD()
            _kernel32.ProcessIdToSessionId(proc.pid, ctypes.byref(sid))
            if sid.value:  # 非 Session 0
                return proc.pid
    raise RuntimeError("未找到可借用 token 的交互会话进程")


def _launch(cmdline, cwd=None, stdout_path=None, session_pid=None):
    hproc = _kernel32.OpenProcess(0x1000, False, session_pid)
    if not hproc:
        _err("OpenProcess")
    htok = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
            hproc, TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
            | TOKEN_IMPERSONATE, ctypes.byref(htok)):
        _err("OpenProcessToken")
    hprimary = wintypes.HANDLE()
    if not _advapi32.DuplicateTokenEx(
            htok, MAXIMUM_ALLOWED, _sa(),
            SecurityImpersonation, TokenPrimary, ctypes.byref(hprimary)):
        _err("DuplicateTokenEx")

    hself = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
            ctypes.c_void_p(-1),  # GetCurrentProcess() 伪句柄
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(hself)):
        _err("OpenProcessToken(self)")
    enable_privileges(hself, ["SeDebugPrivilege"])

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "WinSta0\\Default"
    flags = CREATE_NO_WINDOW
    h_out = None
    if stdout_path:
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x1
        OPEN_ALWAYS = 4
        h_out = _kernel32.CreateFileW(
            stdout_path, GENERIC_WRITE, FILE_SHARE_READ,
            ctypes.byref(_sa(inherit=True)), OPEN_ALWAYS, 0, None)
        si.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        si.hStdInput = h_out
        si.hStdOutput = h_out
        si.hStdError = h_out

    sa_none = _sa()
    sa_inherit = _sa(inherit=True)
    pi = PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(cmdline)

    impersonated = impersonate_system_of_session(session_pid)
    try:
        ok = _advapi32.CreateProcessAsUserW(
            hprimary, None, cmd_buf,
            ctypes.byref(sa_none), ctypes.byref(sa_none),
            True, flags, None, cwd, ctypes.byref(si), ctypes.byref(pi))
    finally:
        if impersonated:
            _advapi32.RevertToSelf()
    if ok:
        if h_out:
            _kernel32.CloseHandle(h_out)
        print(f"launched pid={pi.dwProcessId} (AsUser+system-impersonation, "
              f"token from pid={session_pid})")
        return pi.dwProcessId

    err = ctypes.get_last_error()
    if err != 1314:  # 非 PRIVILEGE_NOT_HELD 才直接报错
        _err("CreateProcessAsUserW")

    # 回退: CreateProcessWithTokenW 只需 SeImpersonatePrivilege，但落点
    # 是调用方会话（实测 Session 0），且不支持句柄继承——仅尽力而为，
    # stdout 重定向改由 cmd /c 内部完成
    _advapi32.CreateProcessWithTokenW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPWSTR,
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
    _advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL
    hself2 = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
            ctypes.c_void_p(-1), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(hself2)):
        _err("OpenProcessToken(self)")
    enable_privileges(hself2, ["SeImpersonatePrivilege"])

    if stdout_path:
        cmdline = ("C:\\Windows\\System32\\cmd.exe /c "
                   f"{cmdline} > \"{stdout_path}\" 2>&1")
        si.dwFlags = 0
        si.hStdInput = si.hStdOutput = si.hStdError = None
        cwd = cwd or "C:\\"
    cmd_buf = ctypes.create_unicode_buffer(cmdline)
    LOGON_WITH_PROFILE = 0x1
    if not _advapi32.CreateProcessWithTokenW(
            hprimary, LOGON_WITH_PROFILE, None, cmd_buf,
            flags, None, cwd, ctypes.byref(si), ctypes.byref(pi)):
        _err("CreateProcessWithTokenW")
    print(f"launched pid={pi.dwProcessId} (WithToken fallback, "
          f"token from pid={session_pid})")
    return pi.dwProcessId


def launch(cmdline, cwd=None, stdout_path=None, session_pid=None,
           window_title=None):
    session_pid = session_pid or find_session_pid(window_title=window_title)
    return _launch(cmdline, cwd=cwd, stdout_path=stdout_path,
                   session_pid=session_pid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--stdout", default=None)
    ap.add_argument("--session-pid", type=int, default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("用法: run_in_session.py [--cwd DIR] [--stdout FILE] -- <命令行...>",
              file=sys.stderr)
        return 2
    cmdline = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    launch(cmdline, cwd=args.cwd, stdout_path=args.stdout,
           session_pid=args.session_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
