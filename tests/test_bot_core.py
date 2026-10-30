import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from core import question_from as parse_question, complete, normalize_groups, GroupConversations

def question_from(group, text, prefix="小深"):
    return parse_question(group, text, target="test", prefix=prefix)

class BotChecks(unittest.TestCase):
    def test_only_explicit_test_group_messages_trigger(self):
        for group, text in [('test2','小深 你好'), ('test','大家好'), ('test','[小深AI] 小深 你好'), ('test','小深度学习'), ('test','小深 '), ('private','小深 你好')]:
            self.assertIsNone(question_from(group,text))
        self.assertEqual(question_from('test','小深：你好'), '你好')
        self.assertEqual(question_from('test','小深\u2005你好'), '你好')

    def test_custom_name_must_be_at_start(self):
        self.assertIsNone(question_from('test', '你好，碧翠丝', prefix='碧翠丝'))
        self.assertEqual(question_from('test', '碧翠丝：你好', prefix='碧翠丝'), '你好')
        self.assertEqual(question_from('test', '碧翠丝 你好', prefix='碧翠丝'), '你好')

    def test_multiple_group_names_preserve_punctuation_and_deduplicate(self):
        self.assertEqual(normalize_groups('  朋友群\n项目,讨论群\n朋友群\n\n'), ['朋友群', '项目,讨论群'])
        groups = ['朋友群', '项目群']
        self.assertEqual(parse_question('项目群', '碧翠丝：你好', groups, '碧翠丝'), '你好')
        self.assertIsNone(parse_question('项目群2', '碧翠丝：你好', groups, '碧翠丝'))

    def test_group_context_is_isolated_and_bounded(self):
        context = GroupConversations(['朋友群', '项目群'])
        context.remember('朋友群', 'private topic', 'first answer', 'alice')
        self.assertEqual(len(context.messages('项目群', 'role', 'hello')), 2)
        self.assertEqual(json.loads(context.messages('朋友群', 'role', 'next', 'alice')[1]['content'])['消息'], 'private topic')
        self.assertEqual(len(context.messages('朋友群', 'role', 'next', 'bob')), 2)
        self.assertEqual(len(context.messages('朋友群', 'role', 'next')), 2)
        context.remember('朋友群', 'unknown private', 'answer')
        self.assertNotIn(('朋友群', None), context.history)
        for index in range(8):
            context.remember('朋友群', str(index), 'reply', 'alice')
        self.assertEqual(len(context.history['朋友群', 'alice']), 10)
        with self.assertRaises(KeyError):
            context.messages('未配置群', 'role', 'hello')

    def test_request_goes_to_official_endpoint_with_system_prompt(self):
        def response(req,timeout):
            self.assertEqual(req.full_url,'https://api.deepseek.com/chat/completions')
            self.assertEqual(req.get_header('Authorization'),'Bearer fake-test-key')
            self.assertEqual(json.loads(req.data)['messages'][0]['role'],'system')
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':'OK'}}]}).encode())
        self.assertEqual(complete('fake-test-key','deepseek-flash',[{'role':'system','content':'test'}],response),'OK')

    def test_errors_do_not_expose_response_body(self):
        def response(req,timeout):
            raise urllib.error.HTTPError(req.full_url,401,'secret body',{},io.BytesIO(b'private'))
        with self.assertRaisesRegex(RuntimeError,'API Key 无效'):
            complete('fake-test-key','deepseek-flash',[],response)

if __name__ == '__main__':unittest.main()
