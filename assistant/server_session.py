"""Check desktop availability; explicit --disconnect transfers OWN RDP session.

No auto-login, password handling, registry edits, or changes to lock policies.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys


def current_session():
    import win32ts
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    sid = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
        raise RuntimeError('无法核对当前 Windows 会话。')
    state = win32ts.WTSQuerySessionInformation(None, sid.value, win32ts.WTSConnectState)
    protocol = win32ts.WTSQuerySessionInformation(None, sid.value, win32ts.WTSClientProtocolType)
    return sid.value, state, protocol


def ensure_interactive_desktop():
    _, state, _ = current_session()
    if state != 0:  # WTSActive, including a session transferred to console
        raise RuntimeError('当前 Windows 桌面会话已断开，无法可靠发送。请重新连接，并使用 4-disconnect-keep-desktop.cmd 退出远程桌面。')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    handle = user32.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS only
    if not handle:
        raise RuntimeError('当前输入桌面不可访问，可能已锁屏；未尝试发送。')
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        ok = user32.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
        if not ok or name.value.lower() != 'default':
            raise RuntimeError('当前不是正常输入桌面，可能处于锁屏或权限提示界面；未尝试发送。')
    finally:
        user32.CloseDesktop(handle)


def disconnect_command(session_id, state, protocol, is_server, is_admin, system_root):
    if not is_server:
        raise RuntimeError('此工具只用于 Windows Server，请在云服务器内运行，不要在本地电脑运行。')
    if not is_admin:
        raise RuntimeError('请关闭此窗口，右键 4-disconnect-keep-desktop.cmd，选择“以管理员身份运行”。')
    if session_id <= 0 or state != 0 or protocol != 2:
        raise RuntimeError('当前不是活动的 RDP 桌面会话，未执行切换；若已经在控制台桌面，无需重复切换。')
    return [str(Path(system_root)/'System32'/'tscon.exe'), str(session_id), '/dest:console']


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:] != ['--disconnect']:
        raise RuntimeError('此工具由 4-disconnect-keep-desktop.cmd 调用。')
    sid, state, protocol = current_session()
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    shell32.IsUserAnAdmin.restype = wintypes.BOOL
    command = disconnect_command(sid, state, protocol,
        sys.getwindowsversion().product_type in (2,3),
        bool(shell32.IsUserAnAdmin()), os.environ['SystemRoot'])
    print('将把当前远程桌面会话转交服务器控制台，然后断开本次远程连接。')
    print('微信和助手会继续运行，控制台桌面保持可操作；不会修改自动登录或锁屏策略。')
    print('请先确认助手已启动；断开后等待 30 秒，再从另一个账号发送新问题测试。')
    input('准备好后按 Enter 执行；不执行请关闭此窗口。')
    completed = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=20)
    if completed.returncode:
        detail = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(f'桌面交接失败，返回码 {completed.returncode}。\n{detail}')
    print('Windows 已接受桌面交接请求。请从另一微信账号验证回复。')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('未执行或未完成：', str(error))
        sys.exit(1)
