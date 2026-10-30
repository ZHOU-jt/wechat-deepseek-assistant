"""Synthetic API responses, never makes a network request."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assistant'))
from core import complete


class ApiResponseTests(unittest.TestCase):
    def test_invalid_key_is_rejected_before_network_without_disclosing_value(self):
        for key in ('测试密钥-secret', 'sk-隐藏\u200b', 'sk-\u200bsecret', 'sk-全角', 'sk-abc\r\ndef',
                    'sk-abc def', 'sk-caf\u00e9', '••••••', '', None):
            opener = Mock()
            with self.subTest(key=key), self.assertRaises(RuntimeError) as caught:
                complete(key, 'deepseek-flash', [], opener)
            opener.assert_not_called()
            self.assertNotIn('secret', str(caught.exception))

    def invoke(self, data):
        def opener(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['reasoning_effort'], 'none')
            self.assertEqual(timeout, 45)
            return io.BytesIO(json.dumps(data).encode())
        return complete('fake-key', 'deepseek-flash', [{'role': 'user', 'content': 'test'}], opener)

    def response(self, content='', reason='stop', reasoning=''):
        return {'choices': [{'message': {'content': content, 'reasoning_content': reasoning},
                             'finish_reason': reason}], 'usage': {'completion_tokens': 500}}

    def test_returns_only_final_answer(self):
        self.assertEqual(self.invoke(self.response(' OK ', reasoning='private reasoning')), 'OK')

    def test_empty_final_and_reasoning_only_are_distinguished(self):
        for content in ('', None, ' \n '):
            with self.subTest(content=content), self.assertRaisesRegex(RuntimeError, '空正文'):
                self.invoke(self.response(content))
        with self.assertRaisesRegex(RuntimeError, '只返回了思考内容') as caught:
            self.invoke(self.response(reasoning='DO-NOT-LOG-PRIVATE-REASONING'))
        self.assertNotIn('DO-NOT-LOG', str(caught.exception))

    def test_length_failure_includes_counts_without_response_text(self):
        with self.assertRaisesRegex(RuntimeError, '长度限制') as caught:
            self.invoke(self.response(None, 'length', 'secret reasoning'))
        self.assertIn('结束原因=length', str(caught.exception))
        self.assertIn('输出tokens=500', str(caught.exception))
        self.assertNotIn('secret reasoning', str(caught.exception))

    def test_bad_json_and_shapes_give_specific_errors(self):
        for response in (None, [], {'choices': []}, {'choices': None}, {'choices': [None]},
                         {'choices': [{'message': None}]}, self.response(['private text'])):
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, '响应格式异常'):
                self.invoke(response)
        with self.assertRaisesRegex(RuntimeError, 'JSON') as caught:
            complete('fake-key', 'model', [], lambda *args, **kwargs: io.BytesIO(b'<html>private-content</html>'))
        self.assertNotIn('private-content', str(caught.exception))

    def test_upstream_error_filter_and_resources_are_not_empty_answers(self):
        with self.assertRaisesRegex(RuntimeError, '错误对象') as caught:
            self.invoke({'error': {'message': 'DO-NOT-LOG'}})
        self.assertNotIn('DO-NOT-LOG', str(caught.exception))
        for reason, expected in [('content_filter', '内容过滤'),
                                 ('insufficient_system_resource', '服务资源不足'), ('tool_calls', '工具调用')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError, expected):
                self.invoke(self.response(reason=reason))

    def test_untrusted_status_or_usage_strings_are_never_logged(self):
        response = self.response(reason=['SECRET'])
        response['usage'] = {'completion_tokens': 'SECRET', 'prompt_tokens': -1}
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(response)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertIn('结束原因=未知', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
