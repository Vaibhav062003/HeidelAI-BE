"""Offline routing checks; no network or application configuration imports."""
import ast
import os
from pathlib import Path
from urllib.parse import urlsplit
from unittest import TestCase
from unittest.mock import patch

source = Path(__file__).parent/'services/agent_service.py'
if not source.exists():
    source = Path(__file__).with_name('agent_service.py')
tree = ast.parse(source.read_text())
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_chatbot_endpoint')
namespace = {'os': os, 'urlsplit': urlsplit, 'CHATBOT_BASE_URL': 'https://normal.example'}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
endpoint = namespace['_chatbot_endpoint']


class TestChatbotRouting(TestCase):
    def test_normal_route_is_unchanged_without_an_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(endpoint('org_heidelai'), 'https://normal.example/api/v2/chat')

    def test_override_matches_exactly_one_org_and_can_be_removed(self):
        with patch.dict(os.environ, {'CHATBOT_TEST_ORG_ID': 'org_heidelai', 'CHATBOT_TEST_BASE_URL': 'https://test.example/'}, clear=True):
            self.assertEqual(endpoint('org_heidelai'), 'https://test.example/api/v2/chat')
            for other in ('org_other', 'org_heidelai_extra', ''):
                self.assertEqual(endpoint(other), 'https://normal.example/api/v2/chat')
            os.environ.pop('CHATBOT_TEST_ORG_ID')
            self.assertEqual(endpoint('org_heidelai'), 'https://normal.example/api/v2/chat')

    def test_invalid_test_endpoint_does_not_affect_other_orgs(self):
        for url in ('', 'http://test.example', 'https://user:secret@test.example',
                    'https://test.example/path', 'https://test.example?key=secret',
                    'https://test.example#fragment', 'https://test.example:invalid',
                    'https://test.\nexample'):
            with self.subTest(url=url), patch.dict(os.environ, {'CHATBOT_TEST_ORG_ID': 'org_heidelai', 'CHATBOT_TEST_BASE_URL': url}, clear=True):
                self.assertEqual(endpoint('org_other'), 'https://normal.example/api/v2/chat')
                with self.assertRaises(ValueError):
                    endpoint('org_heidelai')

    def test_real_sender_uses_org_routing_and_preserves_signing(self):
        sender = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'send_to_agent')
        calls = [node for node in ast.walk(sender) if isinstance(node, ast.Call)]
        self.assertTrue(any(isinstance(node.func, ast.Name) and node.func.id == '_chatbot_endpoint'
                            and isinstance(node.args[0], ast.Name) and node.args[0].id == 'org_id' for node in calls))
        self.assertTrue(any(isinstance(node.func, ast.Name) and node.func.id == 'signed_json'
                            and isinstance(node.args[1], ast.Constant) and node.args[1].value == '/api/v2/chat' for node in calls))
