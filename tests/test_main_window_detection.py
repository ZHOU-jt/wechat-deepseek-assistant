"""Window discovery regression tests with fake HWNDs/UIA; never operates live WeChat."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from main_window_sender import find_main_window, MainWindowSender


class WindowDetectionTests(unittest.TestCase):
    def detect(self, windows, main_handles, recovery=None):
        recovery = recovery or Mock(return_value=Mock())
        with patch('wx4py.features.messaging.listener._find_wechat_windows', return_value=windows), \
             patch('win32process.GetWindowThreadProcessId', side_effect=lambda hwnd: (1, hwnd * 10)), \
             patch('accessibility_recovery.recover', recovery), \
             patch('wx4py.core.win32.bring_window_to_front'), \
             patch('main_window_sender.time.sleep'), \
             patch('wx4py.core.uiautomation.ControlFromHandle', side_effect=lambda hwnd: SimpleNamespace(hwnd=hwnd)), \
             patch('wx4py.features.messaging.listener._find_session_list', side_effect=lambda root: root.hwnd in main_handles):
            return find_main_window()

    def test_qt_native_window_is_detected_by_main_surface(self):
        recovery = Mock(return_value=Mock())
        self.assertEqual(self.detect([(11, '微信', 'Qt51514QWindowIcon')], {11}, recovery), (11, 110))
        recovery.return_value.finish.assert_called_once_with(True)

    def test_detached_window_is_not_selected_even_if_it_has_main_in_class(self):
        self.assertEqual(self.detect([(11, '微信', 'QtWindowIcon'), (12, '私密群名', 'OtherMainWindow')], {11}), (11, 110))

    def test_multiple_accounts_fail_and_restore_all_recoveries(self):
        transactions = [Mock(), Mock()]
        recovery = Mock(side_effect=transactions)
        with self.assertRaisesRegex(RuntimeError, '多个'):
            self.detect([(11, '微信', 'QtWindowIcon'), (12, '微信', 'QtWindowIcon')], {11, 12}, recovery)
        for transaction in transactions:
            transaction.finish.assert_called_once_with(False)

    def test_unreadable_surface_reports_class_not_titles_and_restores(self):
        recovery = Mock(return_value=Mock())
        with self.assertRaisesRegex(RuntimeError, '会话列表暂不可读') as caught:
            self.detect([(11, 'SECRET-CONTACT-NAME', 'Qt51514QWindowIcon')], set(), recovery)
        self.assertNotIn('SECRET-CONTACT-NAME', str(caught.exception))
        self.assertIn('Qt51514QWindowIcon', str(caught.exception))
        recovery.return_value.finish.assert_called_once_with(False)

    def test_no_visible_window_does_not_attempt_memory_recovery(self):
        recovery = Mock()
        with self.assertRaisesRegex(RuntimeError, '未枚举到'):
            self.detect([], set(), recovery)
        recovery.assert_not_called()

    def test_version_or_permission_failure_is_not_reported_as_logged_out(self):
        recovery = Mock(side_effect=RuntimeError('版本不一致'))
        with self.assertRaisesRegex(RuntimeError, '读取恢复未通过.*版本不一致'):
            self.detect([(11, '微信', 'QtWindowIcon')], set(), recovery)

    def test_validation_accepts_original_qt_class_but_rejects_changed_window(self):
        sender = MainWindowSender.__new__(MainWindowSender)
        sender.hwnd, sender.pid, sender.native_class = 11, 110, 'QtWindowIcon'
        with patch('win32gui.IsWindow', return_value=True), \
             patch('win32process.GetWindowThreadProcessId', return_value=(1, 110)), \
             patch('win32gui.GetClassName', return_value='QtWindowIcon') as get_class:
            sender.validate()
            get_class.return_value = 'ChangedWindow'
            with self.assertRaisesRegex(RuntimeError, '已变化'):
                sender.validate()


if __name__ == '__main__':
    unittest.main()
