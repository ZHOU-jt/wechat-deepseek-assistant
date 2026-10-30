"""Database-only polling plus a single serialized main-window sender."""
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

from core import GroupConversations, complete, normalize_groups
from conversation_targets import verify_target
from delivery import DeliveryProbe
from message_source import DatabaseMessageSource, Snapshot, SnapshotBusy
from secure_storage import ContextStore


def run(mode, config, status, run_sender_thread):
    os.environ['WECHAT_LOG_FILE'] = os.devnull
    os.environ['WECHAT_SEND_AUDIT_LOG_FILE'] = os.devnull
    os.environ['WECHAT_LOG_LEVEL'] = 'CRITICAL'
    import wx4py
    if wx4py.__version__ != '0.2.1':
        raise RuntimeError('适配器版本不符，请使用 wx4py 0.2.1。')
    from wx4py.core import uiautomation as uia
    from main_window_sender import find_main_window, MainWindowSender

    groups = normalize_groups(config.get('groups', []))
    all_friends = config.get('all_friends', True) is True
    if not groups and not all_friends:
        raise RuntimeError('请填写目标群名，或开启所有好友私聊回复。')
    hwnd, pid = find_main_window()
    initial_sender = MainWindowSender(hwnd, pid)
    initial_sender.check()
    source = DatabaseMessageSource(pid, groups, status, all_friends=all_friends)
    try:
        friends = sum(target.kind == 'contact' for target in source.targets.values())
        status(f'已通过主窗口定位微信；数据库识别 {len(groups)} 个目标群、{friends} 位普通好友。无需独立窗口。')
        if mode == 'check':
            status('读取检查通过：已核对主窗口搜索控件、联系人和消息库；未调用 API、未发送消息。发送切换需实际消息验收。')
            return
        jobs = queue.Queue(maxsize=20)
        stopped, ready = threading.Event(), threading.Event()
        memories, errors = {}, []
        directory = Path(__file__).resolve().parent / 'cache' / 'conversations'

        def memory_for(target):
            if target.key not in memories:
                store = ContextStore(directory, source.self_id, {target.key: target.username})
                memories[target.key] = GroupConversations([target.key], store, private=target.kind == 'contact')
            return memories[target.key]

        def failed(error):
            errors.append(str(error))
            stopped.set()
            ready.set()
            status('主窗口发送已停止：' + str(error))

        def send_loop():
            sender = MainWindowSender(hwnd, pid)
            original = source.snapshots['contact']
            contact = Snapshot(original.path, original.key)
            try:
                while not stopped.is_set():
                    try:
                        target, memory, job = jobs.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    try:
                        try:
                            verify_target(contact.open(), target, source.self_id)
                        except SnapshotBusy:
                            status('联系人库正在更新，本条暂未回复，请稍后重新发送。')
                            continue
                        except RuntimeError as error:
                            status(f'「{target.name}」本条未回复：' + str(error))
                            continue
                        try:
                            messages = memory.messages(job.group, config['prompt'], job.question,
                                                       job.sender_id, job.sender_name, job.background)
                            status(f'「{target.name}」正在请求 DeepSeek，最长等待约45秒…')
                            answer = complete(config['key'], config['model'], messages)[:1500].strip()
                        except Exception as error:
                            status('本条 API 请求未完成：' + str(error))
                            continue
                        if stopped.is_set():
                            break
                        verify_target(contact.open(), target, source.self_id)
                        edit = sender.open_target(target)
                        # All SQLite connections used here belong to the sender thread.
                        probe_source = SimpleNamespace(self_id=source.self_id,
                            group_ids={target.key: target.username}, snapshots=dict(source.snapshots))
                        with DeliveryProbe(probe_source, target.key, answer) as probe:
                            verify_target(contact.open(), target, source.self_id)
                            memory.prepare_reply(job, answer)
                            status(f'正在通过微信主窗口向「{target.name}」发送回复…')
                            sender.send(target, answer, edit)
                            if not probe.wait(stopped.is_set):
                                raise RuntimeError('15秒内未确认新发送记录，已停止；不自动重发。')
                        try:
                            memory.confirm_reply(target.key, answer)
                        except Exception:
                            raise RuntimeError('发送记录已确认，但缓存保存失败，已停止。') from None
                        status(f'「{target.name}」的新发送记录已核对，缓存已保存。')
                    finally:
                        jobs.task_done()
                    stopped.wait(1)
            finally:
                contact.close()

        thread = threading.Thread(target=run_sender_thread,
            args=(uia, send_loop, ready.set, failed), daemon=True)
        thread.start()
        if not ready.wait(5) or errors:
            stopped.set()
            raise RuntimeError('发送线程未能启动：' + (errors[0] if errors else '启动超时'))
        status('已启动：群聊仅响应触发词；所有普通好友的新文字私聊直接回复。' if all_friends
               else '已启动：通过主窗口发送，目标群仅响应触发词。')
        status('各会话独立缓存并加密保存；启动前的旧消息不补答。图片、语音等暂不回复。')
        try:
            while not stopped.is_set():
                initial_sender.validate()  # Handle/PID check only; never moves focus while polling.
                try:
                    for event in source.poll_all():
                        if stopped.is_set():
                            break
                        target = source.targets.get(event.group)
                        if target is None:
                            continue
                        memory = memory_for(target)
                        job = memory.receive(event.group, event.content, config['prefix'],
                                             event.sender_id, event.sender_name, direct=target.kind == 'contact')
                        if job is not None:
                            try:
                                jobs.put_nowait((target, memory, job))
                                status(f'「{target.name}」新消息已排队；背景 {len(job.background)} 条。')
                            except queue.Full:
                                status('待回复队列已满，本条只存入缓存；请稍后重新提问。')
                except SnapshotBusy:
                    status('微信正在写入数据，稍后继续读取；未重置消息进度。')
                stopped.wait(1)
        finally:
            stopped.set()
        if errors:
            raise RuntimeError(errors[0])
    finally:
        source.close()
