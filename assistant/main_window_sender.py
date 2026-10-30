"""Pinned UI adapter: switch chats in the main window; never open detached windows."""
import time
from types import SimpleNamespace

from search_adapter import search_target
from conversation_guard import verify_current_chat, selected_name_matches
from server_session import ensure_interactive_desktop


def find_main_window():
    from wx4py.features.messaging.listener import _find_wechat_windows, _find_session_list
    from wx4py.core import uiautomation as uia
    from wx4py.core.win32 import bring_window_to_front
    from accessibility_recovery import recover
    import win32process
    windows = _find_wechat_windows()  # Already restricted to visible WeChat process windows.
    if not windows:
        raise RuntimeError('未枚举到可见的微信进程窗口；请显示微信主界面，并保持助手与微信运行权限一致。')
    transactions, failures, matches = {}, {}, []
    selected_pid = None
    try:
        for hwnd, _title, _native_class in windows:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                continue  # A transient popup may disappear during enumeration.
            if pid in failures:
                continue
            if pid not in transactions:
                try:
                    transactions[pid] = recover(pid)
                except RuntimeError as error:
                    failures[pid] = str(error)
                    continue
            try:
                # Native HWNDs may be Qt...QWindowIcon while UIA exposes mmui::MainWindow.
                # Prove this is the main chat surface by its session list, not either class name.
                root = uia.ControlFromHandle(hwnd)
                session_list = _find_session_list(root)
                if not session_list:
                    # Qt may expose its children only after repainting with the recovered gate.
                    bring_window_to_front(hwnd)
                    time.sleep(0.2)
                    session_list = _find_session_list(uia.ControlFromHandle(hwnd))
                if session_list:
                    matches.append((hwnd, pid))
            except Exception:
                continue
        if len(matches) > 1:
            raise RuntimeError('检测到多个带会话列表的微信主界面，请只保留一个准备运行助手的账号。')
        if not matches:
            if failures and not transactions:
                raise RuntimeError('已找到微信窗口，但读取恢复未通过：' + next(iter(failures.values())))
            classes = sorted({cls for _, _, cls in windows})
            # Only native class names and counts, never window titles or contact names.
            details = ','.join(''.join(c for c in cls[:80] if c.isascii() and (c.isalnum() or c in '_:.-'))
                               for cls in classes[:6])
            raise RuntimeError(f'已找到 {len(windows)} 个微信窗口，但主界面会话列表暂不可读。'
                               '请点击微信左侧“聊天”图标后重试；无需重新登录。窗口类型=' + details)
        hwnd, selected_pid = matches[0]
        return hwnd, selected_pid
    finally:
        for pid, transaction in transactions.items():
            transaction.finish(pid == selected_pid)


class MainWindowSender:
    def __init__(self, hwnd, pid):
        from wx4py.features.chat import ChatWindow
        import win32gui
        self.hwnd, self.pid = hwnd, pid
        self.native_class = win32gui.GetClassName(hwnd)
        self.chat = ChatWindow(self)

    @property
    def uia(self):
        from wx4py.core import uiautomation as uia
        return SimpleNamespace(root=uia.ControlFromHandle(self.hwnd))

    def validate(self):
        import win32gui
        import win32process
        if (not win32gui.IsWindow(self.hwnd) or win32gui.GetClassName(self.hwnd) != self.native_class
                or win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid):
            raise RuntimeError('微信主窗口或进程已变化，请停止后重新检查。')

    def activate(self):
        import win32gui
        from wx4py.core.win32 import bring_window_to_front
        self.validate()
        ensure_interactive_desktop()
        bring_window_to_front(self.hwnd)
        deadline = time.monotonic() + 2
        while win32gui.GetForegroundWindow() != self.hwnd and time.monotonic() < deadline:
            time.sleep(0.1)
        if win32gui.GetForegroundWindow() != self.hwnd:
            raise RuntimeError('微信主窗口无法获得前台，请重新连接服务器并点击微信主窗口后检查。')
        return True

    def check(self):
        from accessibility_recovery import recover
        from wx4py.features.messaging.listener import _find_session_list
        self.activate()
        recovery = recover(self.pid)
        verified = False
        try:
            verified = bool(_find_session_list(self.uia.root)) and bool(self.chat._get_search_edit(retries=1))
            if not verified:
                raise RuntimeError('微信主窗口搜索控件不可读，请显示主界面后重试。')
        finally:
            recovery.finish(verified)

    def _assert_selected(self, target):
        import win32gui
        self.validate()
        if win32gui.GetForegroundWindow() != self.hwnd:
            raise RuntimeError('发送前微信失去焦点，已停止发送。')
        verify_current_chat(self.uia.root, target)

    def open_target(self, target):
        self.check()
        result = search_target(self.chat, target)
        result.ctrl.Click(simulateMove=False)
        time.sleep(0.8)
        # Wait for the chat switch to settle; retry verification only, never click twice.
        for attempt in range(5):
            try:
                self._assert_selected(target)
                break
            except RuntimeError:
                if attempt == 4:
                    raise
                time.sleep(0.25)
        edit = self.uia.root.EditControl(AutomationId='chat_input_field')
        if not edit.Exists(maxSearchSeconds=1):
            raise RuntimeError('目标会话未出现明确的聊天输入框，未发送。')
        return edit

    def send(self, target, content, edit):
        from wx4py.features.chat import ChatWindow
        ensure_interactive_desktop()
        self._assert_selected(target)
        if not ChatWindow.prepare_input_for_paste(edit):
            raise RuntimeError('聊天输入框无法准备，未发送。')
        if not ChatWindow.paste_text_into_focused_input(content, log_error='写入回复失败'):
            raise RuntimeError('回复写入输入框失败，未发送。')
        # Check again immediately before Enter; do not retry a possibly sent message.
        self._assert_selected(target)
        edit.SendKeys('{Enter}')
