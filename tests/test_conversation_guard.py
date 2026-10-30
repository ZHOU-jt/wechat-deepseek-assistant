"""Synthetic active-chat checks; no real contact names or messages."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from conversation_guard import verify_current_chat, _title_candidates
from main_window_sender import MainWindowSender


def node(name='', kind='PaneControl', auto='', cls='', children=(), offscreen=False):
    result = SimpleNamespace(Name=name, ControlTypeName=kind, AutomationId=auto, ClassName=cls,
                             IsOffscreen=offscreen, GetChildren=lambda: list(children), _parent=None)
    result.GetParentControl = lambda: result._parent
    for child in children:
        child._parent = result
    return result


def title(name='目标群', **kwargs):
    return node(name, kind='TextControl', auto='content_view.title.current_chat_name_label', **kwargs)


TARGET = SimpleNamespace(name='目标群')


class ConversationGuardTests(unittest.TestCase):
    def setUp(self):
        def candidates(root):
            queue, matches = [root], []
            while queue:
                item = queue.pop(0)
                if (item.ControlTypeName == 'TextControl' and not item.IsOffscreen
                        and item.AutomationId.split('.')[-1] == 'current_chat_name_label'):
                    matches.append(item)
                queue.extend(item.GetChildren())
            return matches
        self.candidates = patch('conversation_guard._title_candidates', side_effect=candidates).start()
        self.addCleanup(patch.stopall)
        patch('wx4py.core.uiautomation.ControlsAreSame', side_effect=lambda a, b: a is b).start()

    def test_dedicated_title_works_without_selection_pattern(self):
        with patch('wx4py.features.messaging.listener._find_session_list') as sessions:
            verify_current_chat(node(children=[node(children=[title()])]), TARGET)
        sessions.assert_not_called()

    def test_wrong_or_duplicate_titles_cannot_fall_back_to_selected_row(self):
        for titles in ([title('其他群')], [title(), title()]):
            with self.subTest(titles=titles), \
                 patch('wx4py.features.messaging.listener._find_session_list') as sessions, \
                 self.assertRaisesRegex(RuntimeError, '标题核对失败'):
                verify_current_chat(node(children=titles), TARGET)
            sessions.assert_not_called()

    def test_title_requires_full_name_not_substring_or_preview(self):
        for name in ('目标群2', '甲\n目标群', '目标群\n备注'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                verify_current_chat(node(children=[title(name)]), TARGET)

    def test_search_messages_other_windows_and_hidden_titles_are_excluded(self):
        impostors = [node(kind='ListControl', children=[title()]),
                     node(cls='mmui::SearchContentPopover', children=[title()]),
                     node(kind='WindowControl', children=[title()]),
                     title(offscreen=True), node('目标群', kind='TextControl')]
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=None), \
             self.assertRaisesRegex(RuntimeError, '标题控件=0'):
            verify_current_chat(node(children=impostors), TARGET)

    def test_container_selection_when_row_pattern_absent(self):
        selected = node('目标群\n最新消息', kind='ListItemControl')
        container = SimpleNamespace(GetSelectionPattern=lambda: SimpleNamespace(GetSelection=lambda: [selected]))
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=container):
            verify_current_chat(node(), TARGET)

    def test_legacy_selected_state_and_non_listitem_session_row(self):
        selected = node('目标群', kind='CustomControl', cls='mmui::ChatSessionCell')
        selected.GetPattern = lambda _: None
        selected.GetLegacyIAccessiblePattern = lambda: SimpleNamespace(State=2)
        container = SimpleNamespace(GetSelectionPattern=lambda: None)
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=container), \
             patch('wx4py.core.uiautomation.WalkControl', return_value=[(selected, 4)]):
            verify_current_chat(node(), TARGET)

    def test_focused_is_not_selected_and_diagnostic_has_no_private_text(self):
        selected = node('隐私姓名\n隐私消息', kind='DataItemControl')
        selected.GetPattern = lambda _: None
        selected.GetLegacyIAccessiblePattern = lambda: SimpleNamespace(State=4)
        container = SimpleNamespace(GetSelectionPattern=lambda: None)
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=container), \
             patch('wx4py.core.uiautomation.WalkControl', return_value=[(selected, 1)]), \
             self.assertRaises(RuntimeError) as error:
            verify_current_chat(node(), TARGET)
        self.assertIn('已选=0', str(error.exception))
        self.assertNotIn('隐私', str(error.exception))

    def test_multiple_selected_rows_remain_rejected(self):
        selected = [node('目标群'), node('其他群')]
        container = SimpleNamespace(GetSelectionPattern=lambda: SimpleNamespace(GetSelection=lambda: selected))
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=container), \
             self.assertRaisesRegex(RuntimeError, '已选=2'):
            verify_current_chat(node(), TARGET)

    def test_unsupported_or_false_item_selection_does_not_pass(self):
        for state in (None, False):
            selected = node('目标群', kind='ListItemControl')
            selected.GetPattern = lambda _: SimpleNamespace(IsSelected=state)
            selected.GetLegacyIAccessiblePattern = lambda: None
            container = SimpleNamespace(GetSelectionPattern=lambda: None)
            with patch('wx4py.features.messaging.listener._find_session_list', return_value=container), \
                 patch('wx4py.core.uiautomation.WalkControl', return_value=[(selected, 1)]), \
                 self.assertRaises(RuntimeError):
                verify_current_chat(node(), TARGET)

    def test_chat_switch_settles_without_reclick(self):
        sender = MainWindowSender.__new__(MainWindowSender)
        sender.check = Mock()
        sender.chat = Mock()
        sender._assert_selected = Mock(side_effect=[RuntimeError('旧标题'), None])
        result, edit, root = Mock(), Mock(), Mock()
        root.EditControl.return_value = edit
        edit.Exists.return_value = True
        with patch('main_window_sender.search_target', return_value=result), \
             patch('main_window_sender.time.sleep'), \
             patch.object(MainWindowSender, 'uia', new_callable=PropertyMock,
                          return_value=SimpleNamespace(root=root)):
            self.assertIs(sender.open_target(TARGET), edit)
        result.ctrl.Click.assert_called_once()
        edit.SendKeys.assert_not_called()

    def test_changed_title_before_enter_prevents_send(self):
        sender = MainWindowSender.__new__(MainWindowSender)
        roots = iter([node(children=[title()]), node(children=[title('别的群')])])
        sender._assert_selected = lambda target: verify_current_chat(next(roots), target)
        edit = Mock()
        with patch('main_window_sender.ensure_interactive_desktop'), \
             patch('wx4py.features.chat.ChatWindow.prepare_input_for_paste', return_value=True), \
             patch('wx4py.features.chat.ChatWindow.paste_text_into_focused_input', return_value=True), \
             self.assertRaisesRegex(RuntimeError, '标题核对失败'):
            sender.send(TARGET, '合成测试回复', edit)
        edit.SendKeys.assert_not_called()

    def test_title_more_than_24_layout_levels_deep_is_verified(self):
        root = title()
        for _ in range(40):
            root = node(children=[root])
        verify_current_chat(root, TARGET)

    def test_large_irrelevant_layout_does_not_abort_title_verification(self):
        root = node(children=[node(children=[node() for _ in range(700)]), title()])
        verify_current_chat(root, TARGET)

    def test_missing_title_in_deep_layout_still_tries_container_selection(self):
        root = node()
        for _ in range(40):
            root = node(children=[root])
        container = SimpleNamespace(GetSelectionPattern=lambda: SimpleNamespace(
            GetSelection=lambda: [node('目标群', kind='ListItemControl')]))
        with patch('wx4py.features.messaging.listener._find_session_list', return_value=container):
            verify_current_chat(root, TARGET)


class NativeTitleQueryTests(unittest.TestCase):
    def test_native_exact_id_query_does_not_enumerate_layout_children(self):
        client, root = Mock(), Mock()
        element = SimpleNamespace(CurrentAutomationId='current_chat_name_label')
        elements = SimpleNamespace(Length=1, GetElement=lambda index: element)
        root.Element.FindAll.return_value = elements
        with patch('wx4py.core.uiautomation._AutomationClient.instance',
                   return_value=SimpleNamespace(IUIAutomation=client)), \
             patch('wx4py.core.uiautomation.Control.CreateControlFromElement', return_value='title'):
            self.assertEqual(_title_candidates(root), ['title'])
        root.GetChildren.assert_not_called()
        root.Element.FindAll.assert_called_once()
        self.assertEqual(root.Element.FindAll.call_args.args[0], 4)

    def test_prefixed_id_fallback_filters_ids_without_reading_message_names(self):
        client, root = Mock(), Mock()
        # Text payloads have no Name attribute: the query must only inspect automation IDs.
        items = [SimpleNamespace(CurrentAutomationId='message_text') for _ in range(700)]
        items.append(SimpleNamespace(CurrentAutomationId='MainView.content.current_chat_name_label'))
        root.Element.FindAll.side_effect = [SimpleNamespace(Length=0),
            SimpleNamespace(Length=len(items), GetElement=lambda index: items[index])]
        with patch('wx4py.core.uiautomation._AutomationClient.instance',
                   return_value=SimpleNamespace(IUIAutomation=client)), \
             patch('wx4py.core.uiautomation.Control.CreateControlFromElement', return_value='title') as convert:
            self.assertEqual(_title_candidates(root), ['title'])
        convert.assert_called_once_with(items[-1])
        root.GetChildren.assert_not_called()


if __name__ == '__main__':
    unittest.main()
