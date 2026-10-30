"""Read committed local WeChat messages, independent of window/scroll state.

Only the selected WeChat process is scanned. Keys, snapshots and chat data stay
in this process's memory; original WeChat databases are opened read-only.
"""
from dataclasses import dataclass
import hashlib
import re
import sqlite3
import struct
import time
from pathlib import Path
from vendor import wechat_db_primitives as p


@dataclass(frozen=True)
class ChatMessage:
    group: str
    content: str
    sender_id: str
    sender_name: str
    message_id: str
    timestamp: int


class SnapshotBusy(RuntimeError):
    """A concurrent WeChat write needs a later read, not a new baseline."""


def checksum(data, state=(0, 0), endian='<'):
    """SQLite WAL rolling checksum, over complete pairs of 32-bit words."""
    if len(data) % 8:
        raise ValueError('Invalid WAL checksum length')
    a, b = state
    for x, y in struct.iter_unpack(endian + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def committed_frames(wal):
    """Return only checksum-valid frames through the last complete commit."""
    if not wal:
        return [], None
    if len(wal) < 32:
        raise SnapshotBusy('微信消息日志正在切换，请稍后重试。')
    magic, version, page_size = struct.unpack('>III', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != p.PAGE_SZ:
        raise RuntimeError('微信消息日志格式不兼容，已停止读取。')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], endian=endian)
    if state != struct.unpack('>II', wal[24:32]):
        raise RuntimeError('微信消息日志头校验失败。')
    frames, committed, pages = [], 0, None
    for off in range(32, len(wal) - 4119, 4120):
        header, page = wal[off:off+24], wal[off+24:off+4120]
        if header[8:16] != wal[16:24]:
            break  # preallocated frames from an older WAL generation
        next_state = checksum(page, checksum(header[:8], state, endian), endian)
        if next_state != struct.unpack('>II', header[16:24]):
            break  # incomplete/torn tail: never apply it
        state = next_state
        pgno, db_size = struct.unpack('>II', header[:8])
        if pgno < 1 or pgno > 131072 or db_size > 131072:
            raise RuntimeError('微信消息日志页号异常。')
        frames.append((pgno, page))
        if db_size:
            committed, pages = len(frames), db_size
    return frames[:committed], pages


def file_stamp(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


class Snapshot:
    def __init__(self, path, key):
        self.path, self.key = Path(path), key
        self.wal = Path(str(path) + '-wal')
        self.conn = self.base = self.stamp = self.base_stamp = None

    def open(self):
        for _ in range(3):
            stamp = (file_stamp(self.path), file_stamp(self.wal))
            if stamp == self.stamp and self.conn is not None:
                return self.conn
            if stamp[0] is None or stamp[0][1] > 256 * 1024 * 1024:
                raise RuntimeError('消息数据库不存在或超过当前读取上限。')
            base = self.base
            if stamp[0] != self.base_stamp or base is None:
                raw = self.path.read_bytes()
                if len(raw) % p.PAGE_SZ or not p._verify_enc_key(self.key, raw[:p.PAGE_SZ]):
                    raise RuntimeError('微信账号或消息数据库已变化，请重新检查读取。')
                base = b''.join(p._decrypt_page(self.key, raw[i:i+p.PAGE_SZ], i//p.PAGE_SZ+1)
                                for i in range(0, len(raw), p.PAGE_SZ))
            try:
                wal = self.wal.read_bytes() if stamp[1] else b''
            except FileNotFoundError:
                continue
            if (file_stamp(self.path), file_stamp(self.wal)) != stamp:
                continue
            frames, pages = committed_frames(wal)
            image = bytearray(base)
            if pages is not None:
                size = pages * p.PAGE_SZ
                image = image[:size]
                if len(image) < size:
                    image.extend(b'\0' * (size - len(image)))
            for pgno, page in frames:
                if pgno * p.PAGE_SZ <= len(image):
                    image[(pgno-1)*p.PAGE_SZ:pgno*p.PAGE_SZ] = p._decrypt_page(self.key, page, pgno)
            # A deserialized database has no filesystem WAL sidecar.
            image[18:20] = b'\x01\x01'
            if pages is not None:
                image[28:32] = struct.pack('>I', pages)
            conn = sqlite3.connect(':memory:')
            try:
                conn.deserialize(bytes(image))
                conn.row_factory = sqlite3.Row
                conn.text_factory = lambda b: b.decode('utf-8') if _utf8(b) else b
                conn.execute('PRAGMA query_only=ON')
                conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
            except Exception:
                conn.close()
                raise
            if self.conn:
                self.conn.close()
            self.conn, self.base, self.base_stamp, self.stamp = conn, base, stamp[0], stamp
            return conn
        raise SnapshotBusy('微信消息正在写入，稍后继续读取，不重置消息进度。')

    def close(self):
        if self.conn:
            self.conn.close()
        self.conn = self.base = self.key = None


def _utf8(data):
    try:
        data.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def decode_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        zstd = p._get_zstd_module()
        if zstd:
            text = p._zstd_decompress(zstd, value)
            if text:
                return text
        if _utf8(value):
            return value.decode('utf-8')
        return p._extract_text_from_blob(value) or ''
    return ''


class DatabaseMessageSource:
    def __init__(self, pid, groups, notify=lambda text: None, all_friends=False):
        self.snapshots, self.cursors, self.seen_server_ids = {}, {}, set()
        self.group_ids, self.names = {}, {}
        self.all_friends = all_friends
        self.targets = {}
        self._cycle_connections = None
        from conversation_targets import ChatTarget
        self.started = time.time()
        module = p._find_weixin_module(pid)
        if not module:
            raise RuntimeError('未找到目标群所属微信模块。')
        from accessibility_recovery import DLL_SHA256
        with open(module[2], 'rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() != DLL_SHA256:
                raise RuntimeError('微信版本已变化，需重新适配消息读取。')
        root = p.auto_detect_db_dir()
        if not root:
            raise RuntimeError('找不到本机微信数据目录。')
        notify('正在核对当前微信账号的消息读取信息，首次检查可能需要稍等…')
        candidates = p.KeyScanner()._collect_key_candidates(pid, set())
        if not candidates:
            raise RuntimeError('当前微信会话未提供可用的数据库读取信息。请确认已登录。')
        matches = []
        for account in p._find_account_dirs(root):
            contact = Path(account, 'db_storage', 'contact', 'contact.db')
            if not contact.exists():
                continue
            key = self._match_key(contact, candidates)
            if key:
                matches.append((Path(account), contact, key))
        if len(matches) != 1:
            raise RuntimeError('无法唯一确定当前微信账号，已停止，避免混用账号。')
        self.account, contact, key = matches[0]
        self.self_id = re.sub(r'_\w{4}$', '', self.account.name)
        self.snapshots['contact'] = Snapshot(contact, key)
        conn = self.snapshots['contact'].open()
        for group in groups:
            found = conn.execute("SELECT username FROM contact WHERE username LIKE '%@chatroom' AND nick_name=?", (group,)).fetchall()
            if len(found) != 1:
                raise RuntimeError(f'「{group}」在当前账号中未找到唯一的完整群名，请检查群名。')
            self.group_ids[group] = found[0][0]
            self.targets[group] = ChatTarget(group, found[0][0], group, 'group')
        if all_friends:
            self.refresh_friends()
        self.message_dir = self.account/'db_storage'/'message'
        paths = sorted(self.message_dir.glob('message_*.db'))
        paths = [path for path in paths if re.fullmatch(r'message_\d+\.db', path.name)]
        if not paths:
            raise RuntimeError('当前账号还没有可读取的消息数据库。')
        for path in paths:
            key = self._match_key(path, candidates)
            if not key:
                raise RuntimeError('部分消息库尚未准备好，请在微信打开目标群后重试。')
            self.snapshots[path.name] = Snapshot(path, key)
        candidates.clear()
        self._baseline()

    def refresh_friends(self):
        from conversation_targets import friend_targets
        friends = friend_targets(self.snapshots['contact'].open(), self.self_id)
        groups = {key: value for key, value in self.targets.items() if value.kind == 'group'}
        if groups.keys() & friends.keys():
            raise RuntimeError('会话名称与内部标识冲突，请修改群名。')
        self.targets = {**groups, **friends}
        self.group_ids = {key: target.username for key, target in self.targets.items()}

    def poll_all(self):
        """Refresh each encrypted shard once per cycle, even with many contacts."""
        if self.all_friends:
            self.refresh_friends()
        self._cycle_connections = {shard: snapshot.open() for shard, snapshot in self.snapshots.items()
                                   if shard != 'contact'}
        try:
            for key in list(self.group_ids):
                yield from self.poll(key)
        finally:
            self._cycle_connections = None

    @staticmethod
    def _match_key(path, candidates):
        with open(path, 'rb') as f:
            page = f.read(p.PAGE_SZ)
        for key, salt in candidates:
            if p._verify_enc_key(key, page):
                return key
            if salt and p._verify_enc_key(key, page, salt):
                return key + salt
        return None

    def _tables(self, group):
        table = 'Msg_' + hashlib.md5(self.group_ids[group].encode()).hexdigest()
        for shard, snapshot in self.snapshots.items():
            if shard == 'contact':
                continue
            cycle = getattr(self, '_cycle_connections', None)
            conn = cycle[shard] if cycle is not None else snapshot.open()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                yield shard, conn, table

    def _baseline(self):
        self_verified = False
        for group in self.group_ids:
            found = False
            for shard, conn, table in self._tables(group):
                found = True
                self_verified |= bool(conn.execute('SELECT 1 FROM Name2Id WHERE user_name=? LIMIT 1', (self.self_id,)).fetchone())
                self.cursors[group, shard] = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM {table}').fetchone()[0]
                rows = conn.execute(f'SELECT server_id FROM {table} WHERE server_id != 0').fetchall()
                self.seen_server_ids.update((group, str(row[0])) for row in rows)
            private = getattr(self, 'targets', {}).get(group)
            if not found and not (private and private.kind == 'contact'):
                raise RuntimeError(f'「{group}」没有本机消息记录，请先在微信打开群并同步一条消息。')
        if not self_verified:
            for shard, snapshot in self.snapshots.items():
                if shard != 'contact':
                    self_verified |= bool(snapshot.open().execute('SELECT 1 FROM Name2Id WHERE user_name=? LIMIT 1', (self.self_id,)).fetchone())
        if not self_verified:
            raise RuntimeError('无法核对助手自己的账号标识，已停止，避免回复自身消息。')
        self.started = time.time()

    def _sender(self, conn, row):
        hit = conn.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (row['real_sender_id'],)).fetchone()
        sender = hit[0] if hit else ''
        # Sender namespace is per shard. Never infer identity from nickname/text.
        if not isinstance(sender, str) or not sender or sender.endswith('@chatroom'):
            return '', ''
        if sender not in self.names:
            contact = self.snapshots['contact'].open()
            nick = contact.execute('SELECT nick_name FROM contact WHERE username=? LIMIT 1', (sender,)).fetchone()
            self.names[sender] = (nick[0] if nick and nick[0] else '群成员')
        return sender, self.names[sender]

    def recent_identity_check(self, group):
        """Return aggregate counts only; never print message text or identifiers."""
        total, recognized, people = 0, 0, set()
        for shard, conn, table in self._tables(group):
            for row in conn.execute(f'SELECT real_sender_id FROM {table} ORDER BY local_id DESC LIMIT 30'):
                total += 1
                sender, _ = self._sender(conn, row)
                if sender:
                    recognized += 1
                    people.add(sender)
        return total, recognized, len(people)

    def poll(self, group):
        current = {path.name for path in self.message_dir.glob('message_*.db') if re.fullmatch(r'message_\d+\.db', path.name)}
        if current != set(self.snapshots) - {'contact'}:
            raise RuntimeError('微信新增或替换了消息库，请停止并重新检查读取。')
        messages, next_cursors, new_server_ids = [], dict(self.cursors), set()
        for shard, conn, table in self._tables(group):
            cursor = self.cursors.get((group, shard), 0)
            maximum = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM {table}').fetchone()[0]
            if maximum < cursor:
                raise RuntimeError('微信消息记录已重置或删除，请重新检查读取。')
            rows = conn.execute(f'SELECT local_id, server_id, real_sender_id, local_type, create_time, message_content, compress_content FROM {table} WHERE local_id > ? ORDER BY local_id LIMIT 200', (cursor,)).fetchall()
            for row in rows:
                next_cursors[group, shard] = row['local_id']
                server = str(row['server_id'] or '')
                if server and ((group, server) in self.seen_server_ids or (group, server) in new_server_ids):
                    continue
                if server:
                    new_server_ids.add((group, server))
                # History sync may insert old rows with a fresh local row number.
                if row['create_time'] < int(self.started) or (row['local_type'] & 0xffffffff) != 1:
                    continue
                sender, name = self._sender(conn, row)
                if sender == self.self_id:
                    continue
                target = getattr(self, 'targets', {}).get(group)
                if target and target.kind == 'contact' and sender != target.username:
                    # Unknown direction must not accidentally reply to our own messages.
                    continue
                content = decode_text(row['message_content']) or decode_text(row['compress_content'])
                if sender and content.startswith(sender + ':\n'):
                    content = content[len(sender)+2:]
                if not content:
                    continue
                message_id = server or f'{shard}:{row["local_id"]}'
                messages.append(ChatMessage(group, content, sender, name, message_id, row['create_time']))
        # Commit together: a busy later shard must not discard earlier new rows.
        self.cursors = next_cursors
        self.seen_server_ids.update(new_server_ids)
        return sorted(messages, key=lambda m: (m.timestamp, m.message_id))

    def close(self):
        for snapshot in self.snapshots.values():
            snapshot.close()
        self.names.clear()
        self.seen_server_ids.clear()
