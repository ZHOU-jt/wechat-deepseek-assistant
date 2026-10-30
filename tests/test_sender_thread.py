"""Exercise the real wx4py queue loop with a fake destination; never sends UI input."""
from pathlib import Path
import sys
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from worker import run_sender_thread
from wx4py.features.messaging.listener import WeChatGroupListener


class SenderTests(unittest.TestCase):
    def test_api_answer_is_drained_by_sender_on_its_own_thread(self):
        calls = []
        fake_uia = SimpleNamespace(
            InitializeUIAutomationInCurrentThread=lambda: calls.append(("init", threading.get_ident())),
            UninitializeUIAutomationInCurrentThread=lambda: calls.append(("cleanup", threading.get_ident())))
        sink = SimpleNamespace(_stop_event=threading.Event(), _reply_queue=queue.Queue())
        def reply(group, content):
            calls.append(((group, content), threading.get_ident()))
            sink._stop_event.set()
        sink.reply = reply
        sink._reply_queue.put(SimpleNamespace(group="test", content="fake API answer"))
        ready, failure = threading.Event(), Mock()
        thread = threading.Thread(target=run_sender_thread, args=(
            fake_uia, lambda: WeChatGroupListener._send_loop(sink), ready.set, failure))
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(ready.is_set())
        self.assertEqual([item[0] for item in calls], ["init", ("test", "fake API answer"), "cleanup"])
        self.assertTrue(all(item[1] == thread.ident for item in calls))
        self.assertEqual(sink._reply_queue.unfinished_tasks, 0)
        failure.assert_not_called()

    def test_initialization_failure_is_reported_before_ready(self):
        init = Mock(side_effect=RuntimeError("init failed"))
        fake_uia = SimpleNamespace(InitializeUIAutomationInCurrentThread=init,
                                  UninitializeUIAutomationInCurrentThread=Mock())
        run, ready, error = Mock(), Mock(), Mock()
        run_sender_thread(fake_uia, run, ready, error)
        run.assert_not_called()
        ready.assert_not_called()
        error.assert_called_once()
        fake_uia.UninitializeUIAutomationInCurrentThread.assert_not_called()

    def test_runtime_failure_is_reported_and_com_is_released(self):
        fake_uia = SimpleNamespace(InitializeUIAutomationInCurrentThread=Mock(),
                                  UninitializeUIAutomationInCurrentThread=Mock())
        error = Mock()
        run_sender_thread(fake_uia, Mock(side_effect=RuntimeError("loop failed")), Mock(), error)
        error.assert_called_once()
        fake_uia.UninitializeUIAutomationInCurrentThread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
