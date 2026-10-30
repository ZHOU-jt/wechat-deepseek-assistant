"""Synthetic Qt/UIA layouts: no running WeChat and no messages sent."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from conversation_targets import ChatTarget, exact_search_result, search_display_name
from search_adapter import parse_search_rows, read_popup, search_target


def node(name='', cls='', auto='', kind='PaneControl', children=()):
    return SimpleNamespace(Name=name, ClassName=cls, AutomationId=auto, ControlTypeName=kind,
                           GetChildren=lambda: list(children), Click=Mock())


def heading(name):
    return node(name, 'mmui::XTableCell')


def row(name, **kwargs):
    return node(name, cls='mmui::SearchContentCellView', **kwargs)


TARGET = ChatTarget('group', 'synthetic@chatroom', '示例群', 'group')


class SearchAdapterTests(unittest.TestCase):
    def test_frequent_group_with_history_and_web_results(self):
        direct = row('示例群', auto='search_item_0')
        results = parse_search_rows([
            heading('最常使用'), direct, heading('聊天记录'),
            row('甲\n我是群聊“示例群”的成员'), row('乙\n示例群'),
            heading('搜索网络结果'), node('示例群', cls='mmui::XTableCell')])
        self.assertIs(exact_search_result(results, TARGET).ctrl, direct)
        self.assertEqual(len(results['聊天记录']), 2)

    def test_nested_headers_cells_without_automation_ids_and_child_name(self):
        direct = node('', cls='mmui::XTableCell', children=[node('示例群', kind='TextControl')])
        results = parse_search_rows([node(children=[node('最常使用', kind='TextControl'), direct])])
        self.assertIs(exact_search_result(results, TARGET).ctrl, direct)

    def test_highlight_and_secondary_line_are_presentation_only(self):
        direct = row('<font color="#00ff00">示例</font>群\n群聊')
        results = parse_search_rows([heading('群聊'), direct])
        self.assertIs(exact_search_result(results, TARGET).ctrl, direct)
        self.assertEqual(search_display_name('A & B'), 'A & B')
        self.assertEqual(search_display_name('<未知>群'), '<未知>群')

    def test_history_children_never_become_candidates(self):
        results = parse_search_rows([
            heading('最常使用'), row('另一个群'), heading('聊天记录'),
            row('示例群\n历史记录', children=[heading('最常使用'), row('示例群')]),
            heading('搜索网络结果'), row('示例群')])
        with self.assertRaisesRegex(RuntimeError, '完整匹配=0'):
            exact_search_result(results, TARGET)

    def test_unknown_leaf_header_stops_category_inheritance(self):
        results = parse_search_rows([heading('最常使用'),
            node('其他搜索分类', kind='TextControl'), row('示例群')])
        with self.assertRaises(RuntimeError):
            exact_search_result(results, TARGET)

    def test_duplicate_and_substring_results_remain_rejected(self):
        for rows in ([row('示例群'), row('示例群')], [row('示例群2')], [row('甲\n示例群')]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                exact_search_result(parse_search_rows([heading('最常使用'), *rows]), TARGET)

    def test_result_named_like_a_section_does_not_change_category(self):
        direct = row('群聊', auto='search_item_0')
        results = parse_search_rows([heading('最常使用'), direct])
        target = ChatTarget('g', 'other@chatroom', '群聊', 'group')
        self.assertIs(exact_search_result(results, target).ctrl, direct)

    def test_list_can_be_nested_in_popup_layout(self):
        direct = row('示例群')
        popup = node(children=[node(children=[node(kind='ListControl', children=[heading('最常使用'), direct])])])
        results, stage = read_popup(SimpleNamespace(_get_search_popup=lambda: popup))
        self.assertEqual(stage, '搜索列表已读取')
        self.assertIs(exact_search_result(results, TARGET).ctrl, direct)

    def test_multiple_lists_are_not_guessed(self):
        popup = node(children=[node(kind='ListControl'), node(kind='ListControl')])
        results, stage = read_popup(SimpleNamespace(_get_search_popup=lambda: popup))
        self.assertEqual(results, {})
        self.assertEqual(stage, '搜索列表数量=2')

    def test_delayed_results_retry_read_only(self):
        chat = SimpleNamespace(_input_search=Mock(return_value=True))
        direct = SimpleNamespace(name='示例群', ctrl=Mock())
        with patch('search_adapter.read_popup', side_effect=[({}, '搜索面板未读到'),
                ({'最常使用': [direct]}, '搜索列表已读取')]), patch('search_adapter.time.sleep'):
            self.assertIs(search_target(chat, TARGET), direct)
        chat._input_search.assert_called_once_with('示例群')
        direct.ctrl.Click.assert_not_called()

    def test_failure_reports_counts_without_contact_or_message_content(self):
        chat = SimpleNamespace(_input_search=Mock(return_value=True))
        results = {'最常使用': [SimpleNamespace(name='敏感备注')],
                   '聊天记录': [SimpleNamespace(name='私密内容')]}
        with patch('search_adapter.read_popup', return_value=(results, '搜索列表已读取')), \
             patch('search_adapter.time.sleep'), self.assertRaises(RuntimeError) as error:
            search_target(chat, TARGET)
        text = str(error.exception)
        self.assertIn('最常使用=1', text)
        self.assertIn('聊天记录=1', text)
        self.assertIn('读取阶段：搜索列表已读取', text)
        self.assertNotIn('敏感备注', text)
        self.assertNotIn('私密内容', text)

    def test_failed_input_never_reads_results(self):
        chat = SimpleNamespace(_input_search=Mock(return_value=False))
        with patch('search_adapter.read_popup') as read, self.assertRaises(RuntimeError):
            search_target(chat, TARGET)
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
