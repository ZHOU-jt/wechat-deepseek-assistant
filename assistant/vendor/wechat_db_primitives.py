# Adapted from fanyuantaier/wechatauto-replica, Apache-2.0.
# Revision d322c4a9b1124228820bab363e5676016f188fc6.
# Changes: only read-only primitives retained; key persistence and all UI removed.
from __future__ import annotations
import ctypes, glob, hashlib, hmac as hmac_mod, json, os, re, struct, time, unicodedata
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


PAGE_SZ = 4096

RESERVE_SZ = 80

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"

CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)

HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]

class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.OpenProcess.restype = wintypes.HANDLE

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

_k32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
]

_k32.VirtualQueryEx.restype = ctypes.c_size_t

_k32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]

def _pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)

def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()

def _verify_enc_key(enc_key: bytes, page1: bytes, salt: Optional[bytes] = None) -> bool:
    """验证 enc_key 是否为 page1 的 SQLCipher 4 密钥。

    - enc_key 为 32 字节裸 key：默认用文件头前 16 字节作 salt（标准形式）；
    - enc_key 为 48 字节 key+salt：显式 salt 优先于文件头（明文头模式）；
    - 也允许单独传 salt 参数覆盖（供提取逻辑按 96hex 拆分尝试）。
    """
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = enc_key[:32], enc_key[32:]
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = _pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac

def _find_weixin_module(pid: int) -> Optional[Tuple[int, int, str]]:
    """返回 (模块基址, 模块大小, 路径); weixin.dll 为微信 4.x 主模块"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1 or snap == (1 << 64) - 1:
        return None
    try:
        me = _MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == "weixin.dll":
                return (me.modBaseAddr or 0), me.modBaseSize, me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None

def _decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        # 48 字节 key（key+salt）= cipher_plaintext_header_size 明文头模式：
        # 页 1 前 16 字节是明文头（非加密 salt），加密数据从 offset 16 开始；
        # 解密后拼接明文头 + 明文数据 + reserve。
        if len(enc_key) == 48:
            enc = page[16: PAGE_SZ - RESERVE_SZ]
            return page[:16] + _aes_cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
        enc = page[16: PAGE_SZ - RESERVE_SZ]
        return b"SQLite format 3\x00" + _aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[: PAGE_SZ - RESERVE_SZ]
    return _aes_cbc_decrypt(enc_key[:32] if len(enc_key) == 48 else enc_key, iv, enc) + b"\x00" * RESERVE_SZ

def _looks_like_text(t: str) -> bool:
    """判断解码结果是否像可读文本（**不要求包含中文**）。

    纯英文 / 纯数字 / URL / Emoji 消息同样需要还原，因此不再用「必须含中文」
    的粗暴门槛；改用可打印字符占比 + 控制/未分配类字符占比双重判定，避免把
    二进制噪声误判成文本。Cf（格式类，如 ZWJ）不计入噪声，否则 Emoji 组合
    序列（👨\u200d👩\u200d👧）会被误杀。
    """
    if not t:
        return False
    bad = 0
    printable = 0
    for ch in t:
        if ch in "\n\r\t":
            printable += 1
            continue
        if unicodedata.category(ch) in ("Cc", "Co", "Cs", "Cn"):
            bad += 1
        else:
            printable += 1
    if printable / len(t) < 0.7:
        return False
    return bad / len(t) <= 0.1

def _extract_text_from_blob(content: bytes) -> Optional[str]:
    """从微信消息容器头中还原 UTF-8 明文文本。

    微信 4.x 部分文本消息的 message_content 为「容器头(0x28 b5 2f fd...)
    + UTF-8 明文 + 尾部填充(\x01\x00...)」，多数消息明文从第 10 字节开始；
    长消息可能加密，无法还原返回 None。
    """
    def _try_off(off: int) -> Optional[str]:
        if off >= len(content):
            return None
        chunk = content[off:]
        if b"\x01\x00" in chunk:
            chunk = chunk.split(b"\x01\x00")[0]
        try:
            t = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
        # 保留换行符(\n和\r)，删除其他控制字符
        t = re.sub(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]+", "", t).strip()
        if not t:
            return None
        if not _looks_like_text(t):
            return None
        return t

    t = _try_off(10)
    if t:
        return t
    for off in range(0, min(16, len(content))):
        if off == 10:
            continue
        t = _try_off(off)
        if t:
            return t
    return None

_ZSTD_MODULE = None

def _get_zstd_module():
    """惰性加载 zstd 模块，兼容 `zstandard` / `zstd` 两种包名。

    WeChat 4.x 长文本消息的 message_content 为 zstd 压缩帧；缺少该第三方
    库时无法解压（会退化为 `[类型]` 占位符）。此函数做了延迟导入 + 双包名
    兼容，缺失库时返回 None（由调用方决定如何兜底）。
    """
    global _ZSTD_MODULE
    if _ZSTD_MODULE is not None:
        return _ZSTD_MODULE
    for mod_name in ("zstandard", "zstd"):
        try:
            _ZSTD_MODULE = __import__(mod_name)
            return _ZSTD_MODULE
        except Exception:
            continue
    _ZSTD_MODULE = False
    return None

def _zstd_decompress(zstd, content: bytes) -> Optional[str]:
    """用 zstd 解压微信消息帧并解码 UTF-8，失败返回 None。"""
    if not content:
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(content, max_output_size=200000)
        if not decompressed:
            return None
        text = decompressed.decode("utf-8", "ignore").strip()
        return text if text else None
    except Exception:
        return None

def _find_account_dirs(db_dir: str) -> List[str]:
    """列出 db_dir 下所有含 db_storage 子目录的账号目录。

    微信号目录不一定以 wxid_ 开头（如自定义微信号），这里只依赖
    db_storage 子目录的存在性判断。
    """
    out = []
    try:
        for name in os.listdir(db_dir):
            p = os.path.join(db_dir, name, "db_storage")
            if os.path.isdir(p):
                out.append(os.path.join(db_dir, name))
    except OSError:
        pass
    return sorted(out)

class KeyScanner:

    def _collect_key_candidates(self, pid: int, seen: set) -> set:
        """收集候选密钥材料（**不做账号校验**）：{(cand, salt_or_None), ...}

        与 `_extract_keys_pid` 的区别：这里只负责“找出可能是密钥的 32 字节
        （或 32+16 带显式 salt）”，校验交给调用方对**每个账号目录**分别做。
        原因：内存里的密钥属于**当前登录账号**，只有对正确的账号目录才能通过
        页1 HMAC；这样账号选择由密码学校验决定，不再依赖“最近修改 .db”启发式
        （微信每次更新会重写 .db，mtime 全变 → 启发式会选错账号 → 0 密钥）。
        """
        h = _k32.OpenProcess(16 | 1024, False, pid)
        if not h:
            return set()
        out: set = set()
        try:

            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                    return buf.raw[:br.value]
                return None
            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [struct.pack('<Q', addr) + struct.pack('<Q', len(CONFIG_CIPHER_NAME)) for addr in needles]
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 16, 80)
                    if not node or len(node) < 64:
                        continue
                    if struct.unpack_from('<Q', node, 16)[0] not in needles:
                        continue
                    if struct.unpack_from('<Q', node, 24)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from('<Q', node, 40)[0]
                    if not 65536 <= config_ptr < 140737488355328:
                        continue
                    obj = read(config_ptr + 136, 40)
                    if not obj or len(obj) < 24:
                        continue
                    data_ptr = struct.unpack_from('<Q', obj, 8)[0]
                    data_len = struct.unpack_from('<Q', obj, 16)[0]
                    if not (0 < data_len <= 1024 and 65536 <= data_ptr < 140737488355328):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes((v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)] for i, v in enumerate(blob)))
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if cand in seen or not self._probable_key(cand):
                                continue
                            seen.add(cand)
                            out.add((cand, None))
                            if s + 96 <= len(run):
                                out.add((cand, bytes.fromhex(run[s + 64:s + 96])))
        finally:
            _k32.CloseHandle(h)
        return out

    @staticmethod
    def _probable_key(b: bytes) -> bool:
        return len(b) == 32 and len(set(b)) >= 15 and (b not in {b'\x00' * 32, b'\xff' * 32})

    @staticmethod
    def _find_bytes(h, read, needle: bytes) -> List[int]:
        hits = []
        addr = 0
        while True:
            mbi = _MBI()
            r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if r == 0:
                break
            if mbi.State == 4096 and mbi.Protect & 255 & 230 and (not mbi.Protect & 256) and (0 < mbi.RegionSize < 268435456):
                buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
                if buf:
                    base = mbi.BaseAddress or 0
                    pos = 0
                    while True:
                        pos = buf.find(needle, pos)
                        if pos < 0:
                            break
                        hits.append(base + pos)
                        pos += 1
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return hits

def _extract_path_from_config(content: str) -> Optional[str]:
    """从配置内容中提取数据目录路径，兼容 JSON 字段 / 纯路径 / 任意文本。

    微信 4.x 不同版本配置文件格式不一：有的是纯路径，有的是 JSON
    （字段如 dataDir / fileSavePath）。这里统一兜底提取第一个 Windows 路径。
    """
    content = (content or "").strip().lstrip("\ufeff")
    if not content:
        return None
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("dataDir", "data_dir", "fileSavePath", "savePath",
                        "path", "defaultFileSavePath"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and re.match(r"^[A-Za-z]:[\\/]", item):
                    return item
    except Exception:
        pass
    if re.match(r"^[A-Za-z]:[\\/]", content):
        return content
    m = re.search(r"[A-Za-z]:[\\/][^\s\x00-\x1f\"']+", content)
    if m:
        return _abs_root(m.group(0))        # 不能用 rstrip：'d:\\' 会变成 'd:'（盘符相对）
    return None

def _config_candidates() -> List[str]:
    """可能的微信 4.x 配置目录（按新旧版本与 32/64 位安装差异）。"""
    out = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env, "")
        if base:
            out.extend([
                os.path.join(base, "Tencent", "xwechat"),
                os.path.join(base, "Tencent", "xwechat", "config"),
                os.path.join(base, "Tencent", "WeChat"),
            ])
    return out

def _registry_data_dirs() -> List[str]:
    """从注册表读取可能指向数据目录的值（用户自定义保存位置时补充来源）。"""
    import winreg
    dirs = []
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat\config"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
    ):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(data, str) or not data.strip():
                    continue
                low = name.lower()
                if "path" in low or "dir" in low or "save" in low:
                    dirs.append(data.strip())
        finally:
            winreg.CloseKey(key)
    return dirs

def _abs_root(path: str) -> str:
    """把目录路径规范化成绝对路径，尤其修好「盘符根目录」这一类。

    以前这里用 ``rstrip("\\\\/")`` 去掉尾分隔符，于是 ``d:\\`` 变成 ``d:`` ——
    而 ``d:`` 在 Windows 上是**盘符相对路径**（相对该盘当前目录），
    ``os.path.join('d:', 'xwechat_files')`` 得到 ``d:xwechat_files``，会按进程在
    D 盘的当前目录解析：从 ``D:\\`` 启动恰好对，从 ``D:\\某个子目录`` 启动就找错地方
    （issue #32）。normpath 保留根目录语义；再补一手「只写了盘符没写分隔符」的输入。
    """
    p = os.path.normpath(path or "")
    if re.match(r"^[A-Za-z]:$", p):          # "d:" 这种盘符相对写法
        p = p + os.sep
    return p

def _locate_account_root(root: Optional[str]) -> Optional[str]:
    """在候选根目录下定位「包含账号目录」的目录。

    账号目录以含 db_storage 子目录为准（微信号不一定以 wxid_ 开头）。
    兼容两种布局：
      <root>/xwechat_files/<account>/db_storage
      <root>/<account>/db_storage
    返回的目录即 WeChatDB.db_dir（账号目录的父目录）。
    """
    if not root or not os.path.isdir(root):
        return None
    root = _abs_root(root)
    candidates = [root]
    for name in ("xwechat_files", "WeChat Files", "xwechat_files_data"):
        candidates.append(os.path.join(root, name))
    seen = set()
    for cand in candidates:
        cand = _abs_root(cand)
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        try:
            dirs = os.listdir(cand)
        except OSError:
            continue
        if any(
            os.path.isdir(os.path.join(cand, d, "db_storage"))
            for d in dirs
        ):
            return cand
    return None

def auto_detect_db_dir() -> Optional[str]:
    """自动定位微信 4.x 数据目录（不同电脑存储位置不同）。

    探测顺序：
      1. 微信配置文件（%APPDATA%/%LOCALAPPDATA%，支持 JSON/纯路径/任意文本）；
      2. 注册表；
      3. 常见默认目录（Documents / 用户主目录）。
    """
    # 1) 配置文件
    for cfg_dir in _config_candidates():
        if not os.path.isdir(cfg_dir):
            continue
        for fp in glob.glob(os.path.join(cfg_dir, "*")):
            if os.path.isdir(fp):
                continue
            try:
                raw = open(fp, "r", encoding="utf-8").read(8192)
            except (UnicodeDecodeError, OSError):
                try:
                    raw = open(fp, "r", encoding="gbk").read(8192)
                except (UnicodeDecodeError, OSError):
                    continue
            path = _extract_path_from_config(raw)
            if not path:
                continue
            hit = _locate_account_root(path)
            if hit:
                return hit
    # 2) 注册表
    for p in _registry_data_dirs():
        hit = _locate_account_root(p)
        if hit:
            return hit
    # 3) 常见默认目录兜底
    userprofile = os.environ.get("USERPROFILE", "")
    for base in (os.path.join(userprofile, "Documents"), userprofile):
        hit = _locate_account_root(base)
        if hit:
            return hit
    return None

_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
