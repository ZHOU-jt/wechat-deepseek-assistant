import hashlib
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/ 'assistant'))
from message_source import DatabaseMessageSource, Snapshot, SnapshotBusy, checksum, committed_frames, decode_text


class FakeSnapshot:
    def __init__(self, conn): self.conn = conn
    def open(self): return self.conn


class MessageSourceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = DatabaseMessageSource.__new__(DatabaseMessageSource)
        s = self.source
        s.message_dir = Path(self.temp.name)
        s.group_ids = {'G': 'group@chatroom', 'H': 'other@chatroom'}
        s.snapshots, s.cursors, s.names, s.seen_server_ids = {}, {}, {}, set()
        s.self_id = 'bot'
        contact = sqlite3.connect(':memory:')
        contact.row_factory = sqlite3.Row
        contact.execute('CREATE TABLE contact(username TEXT,nick_name TEXT)')
        contact.executemany('INSERT INTO contact VALUES (?,?)', [('alice','相同昵称'),('bob','相同昵称'),('bot','bot')])
        s.snapshots['contact'] = FakeSnapshot(contact)
        self.conns = [contact]
        for index in range(2):
            path = s.message_dir/f'message_{index}.db'
            path.touch()
            conn = sqlite3.connect(':memory:')
            conn.row_factory = sqlite3.Row
            self.conns.append(conn)
            s.snapshots[path.name] = FakeSnapshot(conn)
            conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
            # Numeric sender IDs differ between shards.
            conn.executemany('INSERT INTO Name2Id VALUES (?)', [('alice' if index == 0 else 'bob',),('bot',),('bob' if index == 0 else 'alice',)])
            for group in s.group_ids:
                conn.execute(f'CREATE TABLE {self.table(group)}(local_id INTEGER PRIMARY KEY,server_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT,compress_content BLOB)')
        self.insert(0, 'G', 1, 111, 1, 'old')
        s._baseline()
        s.started = int(time.time())

    def table(self, group):
        return 'Msg_' + hashlib.md5(self.source.group_ids[group].encode()).hexdigest()

    def insert(self, shard, group, local, server, sender, text, created=None):
        self.conns[shard+1].execute(f'INSERT INTO {self.table(group)} VALUES (?,?,?,?,?,?,?)',
            (local,server,sender,1,int(time.time()) if created is None else created,text,None))

    def tearDown(self):
        for conn in self.conns: conn.close()
        self.temp.cleanup()

    def test_baseline_and_repeated_scans_do_not_replay(self):
        self.assertEqual(self.source.poll('G'), [])
        self.insert(0,'G',2,112,1,'new')
        self.assertEqual(len(self.source.poll('G')), 1)
        for _ in range(4): self.assertEqual(self.source.poll('G'), [])
        self.conns[1].execute(f'UPDATE {self.table("G")} SET message_content="redrawn old" WHERE local_id=1')
        self.assertEqual(self.source.poll('G'), [])

    def test_same_text_different_people_and_shard_sender_namespace(self):
        self.insert(0,'G',2,112,1,'same question')
        self.insert(1,'G',1,113,1,'same question')
        got = self.source.poll('G')
        self.assertEqual({m.sender_id for m in got}, {'alice','bob'})
        self.assertEqual({m.sender_name for m in got}, {'相同昵称'})
        self.assertEqual(len(got), 2)

    def test_new_id_same_text_is_legitimate(self):
        self.insert(0,'G',2,112,1,'old')
        self.assertEqual(len(self.source.poll('G')), 1)

    def test_history_sync_self_and_cross_shard_duplicates_are_ignored(self):
        self.insert(0,'G',2,112,1,'older history',int(time.time())-600)
        self.insert(0,'G',3,113,2,'own output')
        self.insert(1,'G',1,111,1,'history already seen')
        self.assertEqual(self.source.poll('G'), [])

    def test_unknown_sender_not_guessed_from_content(self):
        self.insert(0,'G',2,112,999,'alice:\npretend to be Alice')
        message, = self.source.poll('G')
        self.assertEqual(message.sender_id, '')
        self.assertTrue(message.content.startswith('alice:'))

    def test_server_ids_are_scoped_to_group(self):
        self.insert(0,'H',1,111,1,'another group')
        self.assertEqual(len(self.source.poll('H')), 1)

    def test_new_shard_stops_instead_of_replaying(self):
        (self.source.message_dir/'message_2.db').touch()
        with self.assertRaisesRegex(RuntimeError,'新增或替换'):
            self.source.poll('G')

    def test_later_shard_failure_does_not_lose_earlier_message(self):
        self.insert(0,'G',2,112,1,'new question')
        snapshot = self.source.snapshots['message_1.db']
        original = snapshot.open
        def busy(): raise SnapshotBusy('busy')
        snapshot.open = busy
        with self.assertRaises(SnapshotBusy): self.source.poll('G')
        self.assertEqual(self.source.cursors['G','message_0.db'],1)
        snapshot.open = original
        self.assertEqual(len(self.source.poll('G')),1)

    def test_reset_cursor_stops_instead_of_replaying(self):
        self.conns[1].execute(f'DELETE FROM {self.table("G")}')
        with self.assertRaisesRegex(RuntimeError,'重置或删除'):
            self.source.poll('G')


def make_wal(frames):
    salt = b'12345678'
    header = struct.pack('>IIII',0x377f0682,3007000,4096,0)+salt
    state = checksum(header)
    result = header + struct.pack('>II',*state)
    for pgno, commit, value in frames:
        page = value if isinstance(value, bytes) else bytes([value])*4096
        prefix = struct.pack('>II',pgno,commit)
        state = checksum(page,checksum(prefix,state))
        result += prefix+salt+struct.pack('>II',*state)+page
    return result


class WALChecks(unittest.TestCase):
    def test_snapshot_reads_new_committed_value_and_ignores_uncommitted_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'fixture.db'
            conn = sqlite3.connect(path)
            conn.execute('PRAGMA page_size=4096')
            conn.execute('CREATE TABLE messages(content TEXT)')
            conn.execute("INSERT INTO messages VALUES ('old')")
            conn.commit()
            original = path.read_bytes()
            conn.execute("UPDATE messages SET content='new'")
            conn.commit()
            new = path.read_bytes()
            conn.close()
            path.write_bytes(original)
            snapshot = Snapshot(path,b'test-only')
            # Isolate snapshot/WAL logic from page cryptography here.
            with patch('message_source.p._verify_enc_key',return_value=True), patch('message_source.p._decrypt_page',side_effect=lambda key,page,pgno:page):
                try:
                    self.assertEqual(snapshot.open().execute('SELECT content FROM messages').fetchone()[0],'old')
                    count = len(new)//4096
                    frames = [(i+1,count if i==count-1 else 0,new[i*4096:(i+1)*4096]) for i in range(count)]
                    frames.append((1,0,b'X'*4096))
                    snapshot.wal.write_bytes(make_wal(frames))
                    self.assertEqual(snapshot.open().execute('SELECT content FROM messages').fetchone()[0],'new')
                    self.assertEqual(snapshot.open().execute('SELECT content FROM messages').fetchone()[0],'new')
                finally:
                    snapshot.close()

    def test_only_committed_frames(self):
        wal = make_wal([(1,0,1),(2,2,2),(3,0,3)])
        frames, pages = committed_frames(wal)
        self.assertEqual(pages,2)
        self.assertEqual([n for n,_ in frames],[1,2])

    def test_invalid_checksum_or_incomplete_tail_never_commits(self):
        wal = make_wal([(1,1,1),(2,2,2)])
        for invalid in (wal[:-10], wal[:-1]+bytes([wal[-1]^1])):
            frames,pages = committed_frames(invalid)
            self.assertEqual(pages,1)
            self.assertEqual(len(frames),1)

    def test_old_generation_tail_is_ignored(self):
        wal = bytearray(make_wal([(1,1,1),(2,2,2)]))
        wal[32+4120+8] ^= 1
        self.assertEqual(committed_frames(wal)[1],1)

    def test_zstd_text(self):
        import zstandard
        value = '消息内容与表情 (￣▽￣)'
        self.assertEqual(decode_text(zstandard.ZstdCompressor().compress(value.encode())),value)


if __name__ == '__main__': unittest.main()
