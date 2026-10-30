import hashlib
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/ 'assistant'))
from delivery import DeliveryProbe
from server_session import disconnect_command


class SnapshotStub:
    def __init__(self, conn): self.conn=conn; self.closed=False
    def open(self): return self.conn
    def close(self): self.closed=True


class DeliveryChecks(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.table='Msg_'+hashlib.md5(b'room@chatroom').hexdigest()
        self.conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
        self.conn.executemany('INSERT INTO Name2Id VALUES (?)',[('other',),('bot',)])
        self.conn.execute(f'CREATE TABLE {self.table}(local_id INTEGER, server_id INTEGER, real_sender_id INTEGER, local_type INTEGER, message_content TEXT, compress_content BLOB)')
        self.insert(1,111,2,'answer')
        self.source=SimpleNamespace(self_id='bot',group_ids={'G':'room@chatroom'},snapshots={'s':SimpleNamespace(path='fake',key=b'fake')})
        self.snapshot=SnapshotStub(self.conn)
        with patch('delivery.Snapshot',return_value=self.snapshot):
            self.probe=DeliveryProbe(self.source,'G','answer')
    def insert(self,local,server,sender,content):
        self.conn.execute(f'INSERT INTO {self.table} VALUES (?,?,?,?,?,?)',(local,server,sender,1,content,None))
    def tearDown(self): self.probe.close(); self.conn.close()

    def test_old_message_is_not_confirmation(self):
        self.assertFalse(self.probe.confirmed())
    def test_other_person_same_text_is_not_confirmation(self):
        self.insert(2,112,1,'answer')
        self.assertFalse(self.probe.confirmed())
    def test_pending_own_message_waits_for_server_id(self):
        self.insert(2,0,2,'answer')
        self.assertFalse(self.probe.confirmed())
        self.conn.execute(f'UPDATE {self.table} SET server_id=112 WHERE local_id=2')
        self.assertTrue(self.probe.confirmed())
    def test_different_output_is_not_confirmation(self):
        self.insert(2,112,2,'different answer')
        self.assertFalse(self.probe.confirmed())
    def test_self_prefix_and_newline_normalization(self):
        self.insert(2,112,2,'bot:\nanswer\r\n')
        self.assertTrue(self.probe.confirmed())
    def test_timeout_does_not_retry_send(self):
        self.assertFalse(self.probe.wait(lambda:False,timeout=0))
    def test_cancelled_verification_returns_false(self):
        self.assertFalse(self.probe.wait(lambda:True))
    def test_failed_baseline_closes_snapshot(self):
        with patch.object(self.snapshot,'open',side_effect=RuntimeError('busy')), patch('delivery.Snapshot',return_value=self.snapshot):
            with self.assertRaises(RuntimeError): DeliveryProbe(self.source,'G','answer')
        self.assertTrue(self.snapshot.closed)


class SessionChecks(unittest.TestCase):
    def test_uses_actual_session_id_and_no_credentials(self):
        command=disconnect_command(37,0,2,True,True,'C:/Windows')
        self.assertEqual(command[1:],['37','/dest:console'])
    def test_rejects_local_pc_service_disconnected_console_and_non_admin(self):
        for args in [(37,0,2,False,True),(0,0,2,True,True),(37,4,2,True,True),(37,0,0,True,True),(37,0,2,True,False)]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                disconnect_command(*args,'C:/Windows')


if __name__=='__main__': unittest.main()
