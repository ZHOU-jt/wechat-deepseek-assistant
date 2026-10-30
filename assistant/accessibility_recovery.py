"""Recover the audited 4.1.15.56 Qt gate; never reuse offsets on another DLL.

Based on the method in fanyuantaier/wechatauto-replica tools/wake_uia.py.
This module has no UI operations, network access, or database/key extraction.
"""
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path
import win32process

DLL_SHA256 = "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556"
GATE_RVA = 188336600
CODE_RVA = 8801010
CODE_BYTES = bytes.fromhex("4885c90f84f7060000803dd67eb30a000f84")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = k32.ReadProcessMemory.argtypes
k32.WriteProcessMemory.restype = wintypes.BOOL


def _read(handle, address, size):
    buffer = ctypes.create_string_buffer(size)
    count = ctypes.c_size_t()
    if not k32.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
        raise RuntimeError("无法核对微信内存，未继续恢复。")
    return buffer.raw


def _write(handle, address, value):
    buffer = ctypes.create_string_buffer(bytes([value]))
    count = ctypes.c_size_t()
    if not k32.WriteProcessMemory(handle, address, buffer, 1, ctypes.byref(count)) or count.value != 1:
        raise RuntimeError("微信读取状态恢复失败。")
    if _read(handle, address, 1) != bytes([value]):
        raise RuntimeError("微信读取状态回读校验失败。")


class Recovery:
    def __init__(self, handle, address, original):
        self.handle, self.address, self.original = handle, address, original
        self.changed = original == 0

    def finish(self, success):
        if self.handle is None:
            return
        try:
            if not success and self.changed:
                _write(self.handle, self.address, self.original)
        finally:
            k32.CloseHandle(self.handle)
            self.handle = None


def recover(pid):
    """Return a transaction tied to the target chat process, not a saved PID."""
    handle = k32.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
    if not handle:
        raise RuntimeError("无法访问目标群所属微信进程，请保持工具与微信权限一致。")
    transaction = None
    try:
        exe = win32process.GetModuleFileNameEx(handle, 0)
        if Path(exe).name.lower() != "weixin.exe":
            raise RuntimeError("目标群所属进程不是 Weixin.exe，已停止恢复。")
        modules = [(base, win32process.GetModuleFileNameEx(handle, base))
                   for base in win32process.EnumProcessModulesEx(handle, 3)]
        matches = [(base, path) for base, path in modules if Path(path).name.lower() == "weixin.dll"]
        if len(matches) != 1:
            raise RuntimeError("未找到唯一的微信模块，已停止恢复。")
        base, path = matches[0]
        with open(path, "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != DLL_SHA256:
            raise RuntimeError("微信程序文件已变化，当前恢复方法只验证过 4.1.15.56 的指定文件；未修改内存，请重新适配。")
        if _read(handle, base + CODE_RVA, len(CODE_BYTES)) != CODE_BYTES:
            raise RuntimeError("微信内存指令与已验证版本不一致，未修改内存。")
        original = _read(handle, base + GATE_RVA, 1)[0]
        if original not in (0, 1):
            raise RuntimeError("微信读取状态值异常，未修改内存。")
        transaction = Recovery(handle, base + GATE_RVA, original)
        if transaction.changed:
            _write(handle, transaction.address, 1)
        return transaction
    except BaseException:
        if transaction is not None:
            transaction.finish(False)
        else:
            k32.CloseHandle(handle)
        raise
