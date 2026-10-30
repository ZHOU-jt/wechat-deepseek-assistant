"""Atomic, current-Windows-user encrypted storage; no plaintext files."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from core import validate_api_key


class EncryptedFile:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return None
        try:
            import win32crypt
            if self.path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('size')
            return win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)[1]
        except Exception:
            raise RuntimeError('无法读取加密缓存。请使用保存时的 Windows 账号，勿直接覆盖原缓存。') from None

    def write(self, value):
        temporary = None
        try:
            import win32crypt
            encrypted = win32crypt.CryptProtectData(value, 'WeChatBot', None, None, None, 0)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            raise RuntimeError('加密缓存保存失败，请检查服务器磁盘空间和文件权限。') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self):
        self.path.unlink(missing_ok=True)


class ContextStore:
    def __init__(self, directory, account_id, group_ids):
        self.directory = Path(directory) / hashlib.sha256(account_id.encode('utf-8')).hexdigest()
        self.group_ids = dict(group_ids)

    def _file(self, group):
        name = hashlib.sha256(self.group_ids[group].encode('utf-8')).hexdigest() + '.bin'
        return EncryptedFile(self.directory / name)

    def load(self, group):
        raw = self._file(group).read()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError('群聊缓存格式无法读取，已停止以保留原文件。') from None

    def save(self, group, value):
        self._file(group).write(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def load_api_key(directory):
    raw = EncryptedFile(Path(directory) / 'api-key.bin').read()
    if raw is None:
        return ''
    try:
        return validate_api_key(raw.decode('utf-8'))
    except UnicodeError:
        raise RuntimeError('保存的 API Key 无法读取。') from None


def save_api_key(directory, value):
    file = EncryptedFile(Path(directory) / 'api-key.bin')
    value = value.strip()
    if value:
        value = validate_api_key(value)
        file.write(value.encode('utf-8'))
    else:
        file.delete()
