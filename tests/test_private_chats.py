"""Synthetic contacts, message databases and UI controls only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from conversation_targets import ChatTarget, friend_targets, verify_target, exact_search_result
from core import GroupConversations
from main_window_sender import MainWindowSender, selected_name_matches
import test_message_source as fixtures


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,flag INTEGER,delete_flag INTEGER,verify_flag INTEGER)')
        rows = [('alice', '同名', '甲唯一备注', 1, 0, 0), ('bob', '同名', '', 1, 0, 0),
                ('bot', '助手', '', 1, 0, 0), ('stranger', '陌生人', '有备注也不是好友', 0, 0, 0),
                ('deleted', '已删除', '', 1, 1, 0), ('verified', '认证号', '', 1, 0, 8),
                ('gh_service', '公众号', '', 1, 0, 0), ('room@chatroom', '朋友群', '', 1, 0, 0),
                ('filehelper', '文件传输助手', '', 1, 0, 0), ('weixin', '系统', '', 1, 0, 0)]
        self.conn.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?)', rows)

    def test_only_verified_friend_records_enter_private_targets(self):
        targets = friend_targets(self.conn, 'bot')
        self.assertEqual(set(targets), {'private:alice', 'private:bob'})
        self.assertEqual(targets['private:alice'].name, '甲唯一备注')

    def test_duplicate_nicknames_are_rejected_but_unique_remark_works(self):
        targets = friend_targets(self.conn, 'bot')
        verify_target(self.conn, targets['private:alice'], 'bot')
        with self.assertRaisesRegex(RuntimeError, '不唯一'):
            verify_target(self.conn, targets['private:bob'], 'bot')

    def test_removed_friend_or_renamed_contact_is_rechecked_before_send(self):
        target = friend_targets(self.conn, 'bot')['private:alice']
        self.conn.execute("UPDATE contact SET flag=0 WHERE username='alice'")
        with self.assertRaisesRegex(RuntimeError, '好友关系'):
            verify_target(self.conn, target, 'bot')
        self.conn.execute("UPDATE contact SET flag=1,remark='新备注' WHERE username='alice'")
        with self.assertRaises(RuntimeError):
            verify_target(self.conn, target, 'bot')

    def test_missing_friend_fields_fail_closed(self):
        self.conn.execute('DROP TABLE contact')
        self.conn.execute('CREATE TABLE contact(username TEXT,nick_name TEXT)')
        with self.assertRaisesRegex(RuntimeError, '好友识别字段'):
            friend_targets(self.conn, 'bot')

    def test_search_requires_unique_full_name_in_correct_category(self):
        target = friend_targets(self.conn, 'bot')['private:alice']
        exact = SimpleNamespace(name=target.name)
        self.assertIs(exact_search_result({'联系人': [exact]}, target), exact)
        self.assertIs(exact_search_result({'最常使用': [exact]}, target), exact)
        for results in ({'群聊': [exact]}, {'联系人': [SimpleNamespace(name=target.name+'2')]},
                        {'联系人': [exact, exact]}, {'功能': [exact]}, {'最常使用': [exact, exact]}):
            with self.subTest(results=results), self.assertRaises(RuntimeError):
                exact_search_result(results, target)


class PrivateSourceTests(unittest.TestCase):
    table = fixtures.MessageSourceChecks.table
    insert = fixtures.MessageSourceChecks.insert
    tearDown = fixtures.MessageSourceChecks.tearDown

    def setUp(self):
        fixtures.MessageSourceChecks.setUp(self)
        self.source.group_ids['P'] = 'alice'
        self.source.targets = {'P': ChatTarget('P', 'alice', '甲', 'contact')}
        for conn in self.conns[1:]:
            conn.execute(f'CREATE TABLE {self.table("P")}(local_id INTEGER PRIMARY KEY,server_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT,compress_content BLOB)')
        self.insert(0, 'P', 1, 500, 1, '启动前的私聊')
        self.source._baseline()

    def test_private_old_self_and_unknown_senders_do_not_trigger(self):
        self.assertEqual(self.source.poll('P'), [])
        self.insert(0, 'P', 2, 501, 2, '助手自己发的')
        self.insert(0, 'P', 3, 502, 999, '方向未知')
        self.insert(0, 'P', 4, 503, 3, '不是此联系人')
        self.assertEqual(self.source.poll('P'), [])
        self.insert(1, 'P', 1, 504, 3, '你好，不需要触发词')
        event, = self.source.poll('P')
        self.assertEqual(event.sender_id, 'alice')
        self.assertEqual(event.content, '你好，不需要触发词')
        self.assertEqual(self.source.poll('P'), [])

    def test_new_private_table_after_start_is_read_without_opening_window(self):
        self.source.group_ids['NEW'] = 'bob'
        self.source.targets['NEW'] = ChatTarget('NEW', 'bob', '乙', 'contact')
        conn = self.conns[1]
        conn.execute(f'CREATE TABLE {self.table("NEW")}(local_id INTEGER PRIMARY KEY,server_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT,compress_content BLOB)')
        self.insert(0, 'NEW', 1, 600, 3, '第一次新私聊')
        event, = self.source.poll('NEW')
        self.assertEqual(event.sender_id, 'bob')


class PrivateContextTests(unittest.TestCase):
    def test_private_auto_reply_and_group_trigger_are_independent(self):
        private = GroupConversations(['private:alice'], private=True)
        group = GroupConversations(['朋友群'])
        other = GroupConversations(['private:bob'], private=True)
        self.assertIsNone(group.receive('朋友群', '普通群聊天', '@助手', 'alice'))
        job = private.receive('private:alice', '我的私事', '@助手', 'alice', '甲', direct=True)
        self.assertEqual(job.question, '我的私事')
        private.prepare_reply(job, '私聊回答')
        private.confirm_reply(job.group, '私聊回答')
        messages = private.messages(job.group, '角色', '继续', 'alice', '甲')
        self.assertIn('一对一私聊', messages[0]['content'])
        self.assertIn('私聊记录', messages[1]['content'])
        self.assertNotIn('普通群聊天', json.dumps(messages, ensure_ascii=False))
        self.assertNotIn('我的私事', json.dumps(group.messages('朋友群', '角色', '你好', 'alice'), ensure_ascii=False))
        self.assertNotIn('我的私事', json.dumps(other.messages('private:bob', '角色', '你好', 'bob'), ensure_ascii=False))
        self.assertNotIn('private:alice', json.dumps(messages, ensure_ascii=False))


class MainWindowTests(unittest.TestCase):
    def test_read_check_uses_main_window_without_api_or_sending(self):
        from account_worker import run
        source = SimpleNamespace(targets={'P': ChatTarget('P', 'alice', '甲', 'contact')}, close=Mock())
        sender = Mock()
        with patch('main_window_sender.find_main_window', return_value=(123, 456)), \
             patch('main_window_sender.MainWindowSender', return_value=sender), \
             patch('account_worker.DatabaseMessageSource', return_value=source) as factory, \
             patch('account_worker.complete') as api:
            run('check', {'groups': [], 'all_friends': True}, Mock(), Mock())
        factory.assert_called_once()
        self.assertTrue(factory.call_args.kwargs['all_friends'])
        sender.check.assert_called_once()
        sender.open_target.assert_not_called()
        sender.send.assert_not_called()
        api.assert_not_called()
        source.close.assert_called_once()

    def test_session_name_is_exact_first_line_not_substring(self):
        self.assertTrue(selected_name_matches('甲\n最近消息', '甲'))
        self.assertFalse(selected_name_matches('甲乙\n最近消息', '甲'))
        self.assertFalse(selected_name_matches('乙\n甲', '甲'))

    def test_focus_or_selection_loss_prevents_enter_and_no_retry(self):
        sender = MainWindowSender.__new__(MainWindowSender)
        sender._assert_selected = Mock(side_effect=[None, RuntimeError('会话变化')])
        edit = Mock()
        with patch('main_window_sender.ensure_interactive_desktop'), \
             patch('wx4py.features.chat.ChatWindow.prepare_input_for_paste', return_value=edit), \
             patch('wx4py.features.chat.ChatWindow.paste_text_into_focused_input', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '会话变化'):
                sender.send(SimpleNamespace(name='甲'), 'synthetic answer', edit)
        edit.SendKeys.assert_not_called()

    def test_search_failure_never_clicks_or_sends(self):
        sender = MainWindowSender.__new__(MainWindowSender)
        sender.check = Mock()
        candidate = SimpleNamespace(name='甲乙', ctrl=Mock())
        sender.chat = SimpleNamespace(search=Mock(return_value={'联系人': [candidate]}))
        with patch('main_window_sender.search_target', side_effect=RuntimeError('搜索不匹配')), \
             self.assertRaises(RuntimeError):
            sender.open_target(ChatTarget('P', 'alice', '甲', 'contact'))
        candidate.ctrl.Click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
