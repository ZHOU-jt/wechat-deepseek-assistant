"""User-started wx4py adapter, pinned to 0.2.1. Never run on import."""
import json
import os
import sys
import time
import queue
import threading
from dataclasses import replace
from pathlib import Path
from core import complete, normalize_groups, GroupConversations
from message_source import DatabaseMessageSource, SnapshotBusy
from delivery import DeliveryProbe
from server_session import ensure_interactive_desktop
from secure_storage import ContextStore

def status(text):
    print(json.dumps({"status": text}, ensure_ascii=False), flush=True)

def run_sender_thread(uia, send_loop, on_ready, on_error):
    """Initialize/uninitialize COM on the sender thread using supported APIs."""
    initialized = False
    try:
        uia.InitializeUIAutomationInCurrentThread()
        initialized = True
        on_ready()
        send_loop()
    except Exception as error:
        on_error(error)
    finally:
        if initialized:
            uia.UninitializeUIAutomationInCurrentThread()

def main(mode, config):
    if mode == "api":
        reply = complete(config["key"], config["model"], [{"role":"user","content":"请只回复：连接成功"}])
        status("DeepSeek API 已连通：" + reply[:120])
        return
    if config.get('main_window', True):
        from account_worker import run
        return run(mode, config, status, run_sender_thread)
    groups = normalize_groups(config.get("groups", []))
    if not groups:
        raise RuntimeError("请在新版设置窗口填写目标群名，然后检查读取。")
    os.environ["WECHAT_LOG_FILE"] = os.devnull
    os.environ["WECHAT_SEND_AUDIT_LOG_FILE"] = os.devnull
    os.environ["WECHAT_LOG_LEVEL"] = "CRITICAL"
    os.environ["WECHAT_ALLOWED_GROUPS"] = ",".join(groups)
    import wx4py
    if wx4py.__version__ != "0.2.1":
        raise RuntimeError("适配器版本不符，请使用 wx4py 0.2.1")
    # Avoid wx4py's implicit registry edits, auto-restart, and login clicks.
    import wx4py.core.window as window_module
    window_module.check_and_fix_registry = lambda: "unchanged"
    window_module.ensure_screen_reader_flag = lambda: False
    window_module.WeChatWindow._try_click_login_button = lambda *args: False
    import win32gui
    import win32process
    from accessibility_recovery import recover
    from wx4py.core import uiautomation as uia
    from wx4py.core.win32 import get_window_title, bring_window_to_front
    from wx4py.features.messaging.listener import (
        WeChatGroupListener, _find_wechat_windows, _find_message_list, _read_visible_items)

    class ConfiguredGroupListener(WeChatGroupListener):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.recoveries = []
            self.target_pids = {}
            self.next_health_checks = {}
            self.message_source = None

        def _ensure_subwindow(self, group, chat_already_open=False):
            matches = [hwnd for hwnd, title, cls in _find_wechat_windows() if title == group]
            if len(matches) != 1:
                raise RuntimeError(f"请将「{group}」打开为独立聊天窗口，且只保留一个同名窗口。")
            hwnd = matches[0]
            # Only raise the target chat; connecting the main window hides it.
            bring_window_to_front(hwnd)
            deadline = time.monotonic() + 8
            if win32gui.GetForegroundWindow() != hwnd:
                status(f"请在 8 秒内点击「{group}」独立窗口，使它位于前台。")
            while win32gui.GetForegroundWindow() != hwnd:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"「{group}」未获得前台焦点，请将群窗口置前后重试。")
                time.sleep(0.1)
            time.sleep(0.5)
            status(f"已定位并置前「{group}」独立窗口。")
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            self.target_pids[group] = pid
            recovery = recover(pid)
            self.recoveries.append(recovery)
            status("已为当前微信会话恢复读取状态（0→1），正在验证控件。" if recovery.changed
                   else "当前微信会话读取状态已开启，正在验证控件。")
            return hwnd

        def _check_health(self, session):
            if not win32gui.IsWindow(session.hwnd) or get_window_title(session.hwnd) != session.group:
                raise RuntimeError(f"「{session.group}」窗口已关闭或变化，请重新检查后启动。")
            _, pid = win32process.GetWindowThreadProcessId(session.hwnd)
            if pid != self.target_pids[session.group]:
                raise RuntimeError("微信进程已变化，请重新检查后启动。")
            root = uia.ControlFromHandle(session.hwnd)
            msg_list = _find_message_list(root)
            if msg_list:
                session.root, session.msg_list = root, msg_list
                return
            status(f"「{session.group}」监听中断：微信控件不可读，正在尝试恢复。")
            transaction = recover(pid)
            recovered = False
            try:
                root = uia.ControlFromHandle(session.hwnd)
                msg_list = _find_message_list(root)
                if not msg_list or not self._find_chat_input(root):
                    raise RuntimeError("读取恢复后仍找不到消息列表或输入框，请停止并重新检查。")
                # UI reconstruction may change runtime IDs. Rebaseline instead of
                # replaying old messages and accidentally sending them to the API.
                session.root, session.msg_list = root, msg_list
                session.seen.update(item.key for item in _read_visible_items(msg_list))
                recovered = True
            finally:
                transaction.finish(recovered)
            status("发送窗口读取已恢复；消息继续按数据库固定编号判断。")

        def _poll_session(self, session):
            if time.monotonic() >= self.next_health_checks.get(session.group, 0):
                self._check_health(session)
                self.next_health_checks[session.group] = time.monotonic() + 3
            # UI RuntimeIds are rebuilt by scrolling; never use them as message IDs.
            if self.message_source is None:
                raise RuntimeError("消息数据库尚未准备好，未开始监听。")
            try:
                messages = self.message_source.poll(session.group)
            except SnapshotBusy:
                session.next_scan_at = time.time() + 1
                if time.monotonic() >= getattr(self, '_next_busy_notice', 0):
                    status("微信正在写入消息，正在等待一致的读取结果；没有重置消息进度。")
                    self._next_busy_notice = time.monotonic() + 10
                return
            for event in messages:
                self.on_message(event)
            session.next_scan_at = time.time() + 1

        def _send_loop(self):
            def failed(error):
                self._sender_error = str(error)
                status("发送线程失败，已停止监听：" + str(error))
                self._stop_event.set()
                self._sender_ready.set()
            run_sender_thread(uia, super()._send_loop, self._sender_ready.set, failed)

        def _start_sender(self):
            self._sender_ready = threading.Event()
            self._sender_error = None
            super()._start_sender()
            if not self._sender_ready.wait(timeout=5):
                self._stop_event.set()
                raise RuntimeError("发送线程启动超时，未开始监听。")
            self._assert_sender_ready()
            status("发送线程已就绪。已启动：仅响应所选群中以“" + config["prefix"]
                   + " ”或“" + config["prefix"] + "：”开头的新文字。")

        def _assert_sender_ready(self):
            if getattr(self, "_sender_error", None):
                raise RuntimeError("发送线程不可用：" + self._sender_error)
            if self._sender_thread is None or not self._sender_thread.is_alive():
                raise RuntimeError("发送线程未运行，请停止并重新启动。")

        def _send_in_subwindow(self, session, content):
            # Build controls in the sender's COM thread instead of sharing them.
            local_session = replace(session, root=uia.ControlFromHandle(session.hwnd))
            return super()._send_in_subwindow(local_session, content)

        def reply(self, group, content):
            delivery_verified = False
            try:
                if self._stop_event.is_set():
                    return False
                if group not in groups or group not in self.sessions or get_window_title(self.sessions[group].hwnd) != group:
                    raise RuntimeError("目标窗口已变化，已阻止发送")
                _, pid = win32process.GetWindowThreadProcessId(self.sessions[group].hwnd)
                if pid != self.target_pids[group]:
                    raise RuntimeError("微信进程已变化，已阻止发送。")
                ensure_interactive_desktop()
                status(f"正在向「{group}」发送回复…")
                with DeliveryProbe(self.message_source, group, content) as probe:
                    ensure_interactive_desktop()
                    sent = super().reply(group, content)
                    if not sent:
                        raise RuntimeError("微信输入或发送动作失败。")
                    status("发送动作已执行，正在核对微信中的新发送记录…")
                    if not probe.wait(self._stop_event.is_set):
                        raise RuntimeError("15 秒内未确认这条回复的新发送记录。可能未发出或仍在等待网络；已停止监听，不自动重发，避免重复。")
                delivery_verified = True
                conversations.confirm_reply(group, content)
            except Exception as error:
                self._sender_error = str(error)
                self._stop_event.set()
                status(("发送记录已确认，但缓存保存失败，已停止监听：" if delivery_verified
                        else "发送未确认，已停止监听：") + str(error))
                raise
            status(f"已在「{group}」核对到助手的新发送记录；接收端是否收到请以群内为准。")
            return True

    # An explicit independently opened group window is mandatory. No search fallback.
    # The listener only needs the outgoing registry when reply_on_at is false.
    # Do not connect(): that activates the unrelated main WeChat window.
    client = wx4py.WeChatClient()
    jobs = queue.Queue(maxsize=3)
    conversations = None
    def on_message(event):
        if event.group not in groups:
            return
        job = conversations.receive(event.group, event.content, config["prefix"], event.sender_id, event.sender_name)
        if job is None:
            status(f"「{event.group}」新文字已加入本群缓存；未匹配触发词，不请求 API、不回复。")
            return
        try:
            jobs.put_nowait(job)
            status(f"「{event.group}」收到触发消息，带入本群背景 {len(job.background)} 条；"
                   + ("同时使用该成员的个人上下文。" if event.sender_id else "未识别发言人，不使用个人历史。"))
        except queue.Full:
            status("待回复队列已满，本条已跳过。")

    listener = ConfiguredGroupListener(client, groups, on_message, auto_reply=False, tail_size=20)
    # This reads the baseline only: old messages are not sent to the API or answered.
    verified = False
    try:
        listener._open_sessions()
        for group, session in listener.sessions.items():
            if not listener._find_chat_input(session.root):
                raise RuntimeError(f"「{group}」已找到消息列表，但没有找到输入框。")
        verified = True
    except RuntimeError as error:
        if mode == "check":
            matches = [hwnd for hwnd, title, cls in _find_wechat_windows() if title in groups]
            if len(matches) == 1:
                hwnd = matches[0]
                root = uia.ControlFromHandle(hwnd)
                classes = []
                for control, depth in uia.WalkControl(root, includeTop=True, maxDepth=8):
                    classes.append(control.ClassName or control.ControlTypeName)
                    if len(classes) >= 80:
                        break
                status("诊断：目标群" + ("仍在前台" if win32gui.GetForegroundWindow() == hwnd else "已失去前台焦点")
                       + "；控件数=" + str(len(classes)) + ("（至少）" if len(classes) == 80 else "")
                       + "；控件类型=" + ", ".join(dict.fromkeys(classes))[:400])
        raise error
    finally:
        for recovery in reversed(listener.recoveries):
            recovery.finish(verified)
            if not verified and recovery.changed:
                status("控件检查未通过，本次内存修改已恢复原值。")
    pids = set(listener.target_pids.values())
    if len(pids) != 1:
        raise RuntimeError("目标群不属于同一个微信进程，请分别运行助手。")
    listener.message_source = DatabaseMessageSource(pids.pop(), groups, status)
    status("消息读取已切换为固定消息编号；滚动聊天窗口不会重新触发旧问题。")
    if mode == "check":
        for _ in range(3):
            time.sleep(1)
            for session in listener.sessions.values():
                listener._check_health(session)
        for group in groups:
            total, identified, people = listener.message_source.recent_identity_check(group)
            status(f"「{group}」发言人检查：最近抽查 {total} 条，识别 {identified} 条、{people} 位成员。")
        listener.message_source.close()
        status(f"读取检查通过：已核对 {len(groups)} 个群的发送窗口、固定消息编号和发言人；未调用 API、未发送消息。")
        client.disconnect()
        return

    source = listener.message_source
    try:
        conversations = GroupConversations(groups, ContextStore(
            Path(__file__).resolve().parent / 'cache' / 'conversations', source.self_id, source.group_ids))
    except Exception:
        source.close()
        raise
    status("缓存已就绪：每群最近40条文字、每位成员最近5轮对话；加密保存，重启恢复，不补答旧消息。")

    def generate():
        while True:
            job = jobs.get()
            group = job.group
            try:
                prompt = config["prompt"]
                listener._assert_sender_ready()
                if listener._stop_event.is_set():
                    continue
                messages = conversations.messages(group, prompt, job.question, job.sender_id, job.sender_name,
                                                  shared_context=job.background)
                status(f"「{group}」正在请求 DeepSeek，最长等待约 45 秒…")
                answer = complete(config["key"], config["model"], messages)
                listener._assert_sender_ready()
                if listener._stop_event.is_set():
                    continue
                answer = answer[:1500].strip()
                conversations.prepare_reply(job, answer)
                listener.enqueue_reply(group, answer)
                status(f"「{group}」DeepSeek 已返回回答，已加入微信发送队列。")
            except Exception as error:
                status(str(error))
            finally:
                jobs.task_done()
            time.sleep(3)
    threading.Thread(target=generate, daemon=True).start()
    status("正在启动发送线程…")
    try:
        listener.start(block=True)
    finally:
        listener._stop_event.set()
        listener.message_source.close()
    if getattr(listener, "_sender_error", None):
        raise RuntimeError("发送线程已退出：" + listener._sender_error)

if __name__ == "__main__":
    try:
        settings = json.loads(sys.stdin.readline())
        main(sys.argv[1], settings)
    except Exception as error:
        status("未完成：" + str(error))
        sys.exit(1)
