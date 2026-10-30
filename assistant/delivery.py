"""Verify a new self-authored text record using sender-thread-owned snapshots."""
import hashlib
import time
from message_source import Snapshot, SnapshotBusy, decode_text


def normalized_text(value):
    return value.replace('\r\n','\n').replace('\r','\n').strip()


class DeliveryProbe:
    def __init__(self, source, group, content):
        self.self_id = source.self_id
        self.content = normalized_text(content)
        self.table = 'Msg_' + hashlib.md5(source.group_ids[group].encode()).hexdigest()
        self.snapshots, self.baseline = {}, {}
        try:
            # Share only immutable metadata; SQLite connections are thread-local.
            for shard, original in source.snapshots.items():
                if shard == 'contact':
                    continue
                snapshot = Snapshot(original.path, original.key)
                self.snapshots[shard] = snapshot
                conn = snapshot.open()
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (self.table,)).fetchone():
                    snapshot.close()
                    del self.snapshots[shard]
                    continue
                self.baseline[shard] = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM {self.table}').fetchone()[0]
            if not self.snapshots:
                raise RuntimeError('找不到目标会话的发送核验记录，未尝试发送。')
        except Exception:
            self.close()
            raise

    def confirmed(self):
        for shard, snapshot in self.snapshots.items():
            conn = snapshot.open()
            rows = conn.execute(f'''SELECT m.message_content, m.compress_content
                FROM {self.table} AS m JOIN Name2Id AS n ON n.rowid=m.real_sender_id
                WHERE m.local_id > ? AND m.server_id IS NOT NULL AND m.server_id != 0
                  AND (m.local_type & 4294967295)=1 AND n.user_name=?''',
                (self.baseline[shard], self.self_id)).fetchall()
            for row in rows:
                content = decode_text(row[0]) or decode_text(row[1])
                if content.startswith(self.self_id + ':\n'):
                    content = content[len(self.self_id)+2:]
                if normalized_text(content) == self.content:
                    return True
        return False

    def wait(self, stopped, timeout=15):
        deadline = time.monotonic() + timeout
        while not stopped():
            try:
                if self.confirmed():
                    return True
            except SnapshotBusy:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        return False

    def close(self):
        for snapshot in self.snapshots.values():
            snapshot.close()
        self.snapshots.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
