import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
import accessibility_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        dll = Path(folder) / "Weixin.dll"
        dll.write_bytes(b"audited fixture")
        self.stack.enter_context(patch.object(recovery, "DLL_SHA256", hashlib.sha256(dll.read_bytes()).hexdigest()))
        self.kernel = self.stack.enter_context(patch.object(recovery, "k32"))
        self.kernel.OpenProcess.return_value = 42
        self.stack.enter_context(patch.object(recovery.win32process, "EnumProcessModulesEx", return_value=[1000]))
        self.stack.enter_context(patch.object(recovery.win32process, "GetModuleFileNameEx",
                                             side_effect=lambda h, b: str(dll) if b else r"C:\Weixin.exe"))
        self.write = self.stack.enter_context(patch.object(recovery, "_write"))
        self.read = self.stack.enter_context(patch.object(recovery, "_read",
                                            side_effect=[recovery.CODE_BYTES, b"\x00"]))

    def test_changed_dll_refuses_write(self):
        with patch.object(recovery, "DLL_SHA256", "wrong"):
            with self.assertRaisesRegex(RuntimeError, "程序文件已变化"):
                recovery.recover(123)
        self.write.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(42)

    def test_failed_control_check_restores_original(self):
        transaction = recovery.recover(123)
        transaction.finish(False)
        self.assertEqual([c.args[2] for c in self.write.call_args_list], [1, 0])
        self.kernel.CloseHandle.assert_called_once_with(42)

    def test_already_active_does_not_write_or_disable(self):
        self.read.side_effect = [recovery.CODE_BYTES, b"\x01"]
        transaction = recovery.recover(123)
        transaction.finish(False)
        self.write.assert_not_called()

    def test_unexpected_original_refuses_write(self):
        self.read.side_effect = [recovery.CODE_BYTES, b"\x02"]
        with self.assertRaisesRegex(RuntimeError, "状态值异常"):
            recovery.recover(123)
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
