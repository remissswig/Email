import contextlib
import copy
import importlib.util
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from unittest.mock import call, patch

from outlook_web.cluster.storage import (
    CLUSTER_PROTOCOL_VERSION,
    ReplicaApplyError,
    apply_increment,
    apply_snapshot,
)


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
WEB_OUTLOOK_APP_PATH = ROOT_DIR / 'web_outlook_app.py'
ISOLATED_SECRET_KEY = 'test-secret-key'


@contextlib.contextmanager
def temporary_environment(**updates):
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_isolated_web_outlook_app(*, role='primary', secret_key=ISOLATED_SECRET_KEY):
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix='public-mailbox-messages-tests-'))
    database_path = temp_dir / 'test.db'
    module_name = f'test_public_mailbox_messages_app_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(module_name, WEB_OUTLOOK_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with temporary_environment(
        DATABASE_PATH=str(database_path),
        SECRET_KEY=secret_key,
        NODE_ROLE=role,
        MASTER_URL='https://primary.example' if role == 'replica' else None,
    ):
        spec.loader.exec_module(module)

    module.resolve_secret_key = lambda: secret_key
    module.secret_key = secret_key
    module.app.secret_key = secret_key
    module._cipher_suite = None
    return module, temp_dir, module_name


def cleanup_isolated_web_outlook_app(module, temp_dir, module_name):
    try:
        with module.app.app_context():
            module.close_connection(None)
    except Exception:
        pass
    sys.modules.pop(module_name, None)
    shutil.rmtree(temp_dir, ignore_errors=True)


web_outlook_app = None
_temp_dir = None
_module_name = None


def setUpModule():
    global web_outlook_app, _temp_dir, _module_name
    web_outlook_app, _temp_dir, _module_name = load_isolated_web_outlook_app()


def tearDownModule():
    cleanup_isolated_web_outlook_app(
        web_outlook_app,
        _temp_dir,
        _module_name,
    )


class PublicMailboxMessageHelperTests(unittest.TestCase):
    def test_suite_uses_isolated_module(self):
        self.assertNotEqual(web_outlook_app.__name__, 'web_outlook_app')

    def test_graph_access_token_result_reuses_process_cache(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    'access_token': 'cached-graph-token',
                    'expires_in': 3600,
                }

        with web_outlook_app.access_token_cache_lock:
            web_outlook_app.access_token_cache.clear()

        with patch.object(
            web_outlook_app,
            'request_graph_token_response',
            return_value=FakeResponse(),
        ) as token_request_mock:
            first = web_outlook_app.get_access_token_graph_result('client-id', 'refresh-token')
            second = web_outlook_app.get_access_token_graph_result('client-id', 'refresh-token')

        self.assertTrue(first['success'])
        self.assertTrue(second['success'])
        self.assertEqual(first['access_token'], 'cached-graph-token')
        self.assertEqual(second['access_token'], 'cached-graph-token')
        token_request_mock.assert_called_once()

    def test_shared_email_header_matcher_matches_display_name_and_rejects_substrings(self):
        self.assertTrue(web_outlook_app.email_header_matches_address(
            'Hide My Email <01litany_muster@icloud.com>',
            '01litany_muster@icloud.com',
        ))
        self.assertFalse(web_outlook_app.email_header_matches_address(
            'user@example.com.evil.test',
            'user@example.com',
        ))

    def test_matches_display_name_address(self):
        self.assertTrue(web_outlook_app.public_mailbox_to_matches(
            'Hide My Email <01litany_muster@icloud.com>',
            '01litany_muster@icloud.com',
        ))

    def test_matches_case_insensitively_among_multiple_recipients(self):
        self.assertTrue(web_outlook_app.public_mailbox_to_matches(
            'First <first@example.com>, TARGET@EXAMPLE.COM',
            'target@example.com',
        ))

    def test_rejects_substring_match(self):
        self.assertFalse(web_outlook_app.public_mailbox_to_matches(
            'user@example.com.evil.test',
            'user@example.com',
        ))

    def test_matches_plus_tag_aliases_against_base_address(self):
        self.assertTrue(web_outlook_app.public_mailbox_to_matches(
            'Hide My Email <meaty.payers0p@icloud.com>',
            'meaty.payers0p+aa@icloud.com',
        ))

    def test_query_defaults(self):
        parsed, error = web_outlook_app.parse_public_mailbox_message_query({
            'mainemail': 'Owner@Example.com',
            'email': 'Target@Example.com',
        })

        self.assertEqual(error, '')
        self.assertEqual(parsed, {
            'mainemail': 'owner@example.com',
            'email': 'target@example.com',
            'format': 'html',
            'limit': 1,
        })

    def test_query_missing_or_blank_mainemail_uses_recipient(self):
        for query in (
            {'email': 'Target@Example.com'},
            {'mainemail': '', 'email': 'Target@Example.com'},
            {'mainemail': '   ', 'email': 'Target@Example.com'},
        ):
            with self.subTest(query=query):
                parsed, error = web_outlook_app.parse_public_mailbox_message_query(query)

                self.assertEqual(error, '')
                self.assertEqual(parsed, {
                    'mainemail': 'target@example.com',
                    'email': 'target@example.com',
                    'format': 'html',
                    'limit': 1,
                })

    def test_query_blank_values_use_defaults(self):
        for query in (
            {
                'mainemail': 'Owner@Example.com',
                'email': 'Target@Example.com',
                'format': '',
                'limit': '',
            },
            {
                'mainemail': 'Owner@Example.com',
                'email': 'Target@Example.com',
                'format': '   ',
                'limit': '   ',
            },
        ):
            with self.subTest(query=query):
                parsed, error = web_outlook_app.parse_public_mailbox_message_query(query)

                self.assertEqual(error, '')
                self.assertEqual(parsed, {
                    'mainemail': 'owner@example.com',
                    'email': 'target@example.com',
                    'format': 'html',
                    'limit': 1,
                })

    def test_query_explicit_json_preserved(self):
        parsed, error = web_outlook_app.parse_public_mailbox_message_query({
            'mainemail': 'Owner@Example.com',
            'email': 'Target@Example.com',
            'format': 'json',
            'limit': ' 2 ',
        })

        self.assertEqual(error, '')
        self.assertEqual(parsed, {
            'mainemail': 'owner@example.com',
            'email': 'target@example.com',
            'format': 'json',
            'limit': 2,
        })

    def test_query_rejects_invalid_values(self):
        invalid_queries = (
            {},
            {'mainemail': 'owner@example.com'},
            {'mainemail': 'bad address', 'email': 'target@example.com'},
            {'mainemail': 'owner@example.com', 'email': 'target@example.com', 'format': 'xml'},
            {'mainemail': 'owner@example.com', 'email': 'target@example.com', 'limit': 'abc'},
            {'mainemail': 'owner@example.com', 'email': 'target@example.com', 'limit': '0'},
            {'mainemail': 'owner@example.com', 'email': 'target@example.com', 'limit': '21'},
            {
                'mainemail': 'owner@example.com',
                'email': 'target@example.com',
                'limit': '2',
                'format': 'html',
            },
        )

        for query in invalid_queries:
            with self.subTest(query=query):
                parsed, error = web_outlook_app.parse_public_mailbox_message_query(query)
                self.assertIsNone(parsed)
                self.assertTrue(error)

    def test_parse_mailboxes_messages_scanned_count_accepts_integer_and_string(self):
        valid_cases = (
            (1, 1),
            ('1', 1),
            (10000, 10000),
            ('10000', 10000),
        )

        for raw_value, expected in valid_cases:
            with self.subTest(raw_value=raw_value):
                parsed, error = web_outlook_app.parse_mailboxes_messages_scanned_count(raw_value)
                self.assertEqual(parsed, expected)
                self.assertEqual(error, '')

    def test_parse_mailboxes_messages_scanned_count_rejects_invalid_values(self):
        invalid_values = (
            None,
            '',
            '   ',
            True,
            False,
            '1.5',
            '-1',
            0,
            '0',
            10001,
            '10001',
            'abc',
        )

        for raw_value in invalid_values:
            with self.subTest(raw_value=raw_value):
                parsed, error = web_outlook_app.parse_mailboxes_messages_scanned_count(raw_value)
                self.assertIsNone(parsed)
                self.assertEqual(error, '最多扫描邮件数必须是 1 到 10000 之间的整数')

    def test_get_mailboxes_messages_scanned_count_defaults_and_falls_back_for_invalid_storage(self):
        with self.app_context():
            self.assertEqual(
                web_outlook_app.get_mailboxes_messages_scanned_count(),
                100,
            )
            self.assertTrue(web_outlook_app.set_setting('mailboxes_messages_scanned_count', '350'))
            self.assertEqual(
                web_outlook_app.get_mailboxes_messages_scanned_count(),
                350,
            )

            for stored_value in ('', 'abc', '0', '10001', '1.5'):
                with self.subTest(stored_value=stored_value):
                    self.assertTrue(
                        web_outlook_app.set_setting('mailboxes_messages_scanned_count', stored_value)
                    )
                    self.assertEqual(
                        web_outlook_app.get_mailboxes_messages_scanned_count(),
                        100,
                    )

    def test_public_mailbox_html_result_response_renders_expected_html_contract(self):
        with self.app_context():
            html_response = web_outlook_app.public_mailbox_html_result_response({
                'success': True,
                'messages': [{
                    'body': '<b>123456</b>',
                    'body_type': 'html',
                }],
            })
            text_response = web_outlook_app.public_mailbox_html_result_response({
                'success': True,
                'messages': [{
                    'body': '<script>alert(1)</script>\nsecond line',
                    'body_type': 'text',
                }],
            })
            no_mail_response = web_outlook_app.public_mailbox_html_result_response({
                'success': False,
                'status': 404,
                'error': 'upstream not found',
            })

        self.assertEqual(html_response.status_code, 200)
        self.assertEqual(html_response.get_data(as_text=True), '<b>123456</b>')
        self.assertEqual(html_response.content_type, 'text/html; charset=utf-8')
        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(
            text_response.get_data(as_text=True),
            '<pre>&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line</pre>',
        )
        self.assertEqual(no_mail_response.status_code, 200)
        self.assertEqual(no_mail_response.get_data(as_text=True), '\u5f53\u524d\u65e0\u90ae\u4ef6')
        self.assertEqual(no_mail_response.headers['Cache-Control'], 'no-store')
        self.assertEqual(no_mail_response.headers['Referrer-Policy'], 'no-referrer')

    @staticmethod
    @contextlib.contextmanager
    def app_context():
        with web_outlook_app.app.app_context():
            web_outlook_app.init_db()
            yield


class PublicMailboxMessageSearchTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            'id': 7,
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }
        self.imap_account = {
            'id': 8,
            'email': 'imap-owner@example.com',
            'account_type': 'imap',
            'imap_password': 'plain-password',
            'imap_host': 'imap.example.com',
            'imap_port': 993,
            'provider': 'custom',
        }

    @staticmethod
    def item(message_id, recipient, date='2026-08-21T10:00:00Z'):
        return {
            'id': message_id,
            'subject': f'Subject {message_id}',
            'from': 'sender@example.com',
            'to': recipient,
            'date': date,
            'folder': 'inbox',
            'id_mode': 'graph',
        }

    @staticmethod
    def detail(item, body=None, body_type='html'):
        return {
            'success': True,
            'email': {
                **item,
                'body': body if body is not None else f'<p>{item["id"]}</p>',
                'body_type': body_type,
            },
        }

    def test_outlook_accounts_use_graph_recipient_search_without_detail_refetch(self):
        account = {
            **self.account,
            'client_id': 'client-id',
            'refresh_token': 'refresh-token',
        }
        matching = {
            **self.item('graph-fast-match', 'target@example.com', '2026-08-21T12:00:00Z'),
            '_detail': {
                'id': 'graph-fast-match',
                'subject': 'Fast match',
                'from': 'sender@example.com',
                'to': 'target@example.com',
                'date': '2026-08-21T12:00:00Z',
                'body': '<p>fast body</p>',
                'body_type': 'html',
            },
        }

        with patch.object(
            web_outlook_app,
            'fetch_account_graph_emails_by_recipient',
            return_value={
                'success': True,
                'emails': [matching],
                'recipient_search_supported': True,
                'request_method': 'graph',
            },
        ) as graph_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={'success': True, 'emails': [], 'has_more': False},
        ) as scan_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['messages'][0]['body'], '<p>fast body</p>')
        graph_search_mock.assert_called_once_with(account, 'inbox', 'target@example.com', 1)
        scan_mock.assert_not_called()
        detail_mock.assert_not_called()

    def test_outlook_graph_recipient_search_no_match_falls_back_to_limited_scan(self):
        account = {
            **self.account,
            'client_id': 'client-id',
            'refresh_token': 'refresh-token',
        }
        matching = self.item('fallback-match', 'target@example.com')

        with patch.object(
            web_outlook_app,
            'fetch_account_graph_emails_by_recipient',
            return_value={
                'success': True,
                'emails': [],
                'recipient_search_supported': True,
                'request_method': 'graph',
            },
        ) as graph_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [matching],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as scan_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['messages'][0]['id'], 'fallback-match')
        self.assertEqual(graph_search_mock.call_args_list, [
            call(account, 'inbox', 'target@example.com', 1),
            call(account, 'junkemail', 'target@example.com', 1),
            call(account, 'deleteditems', 'target@example.com', 1),
        ])
        scan_mock.assert_called_once_with(account, 'inbox', 0, 50)
        detail_mock.assert_called_once_with(
            account,
            'fallback-match',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_scans_next_page_and_passes_provider_method_to_detail_fetch(self):
        first_page_items = [
            self.item(f'other-{index}', 'other@example.com')
            for index in range(50)
        ]
        matching = self.item(
            'match-1',
            'Hide My Email <target@example.com>',
            '2026-08-21T11:00:00Z',
        )
        inbox_pages = {
            0: {
                'success': True,
                'emails': first_page_items,
                'has_more': True,
                'request_method': 'graph',
            },
            50: {
                'success': True,
                'emails': [matching],
                'has_more': False,
                'request_method': 'graph',
            },
        }

        def fetch_side_effect(account, folder, skip, top):
            if folder == 'inbox':
                return inbox_pages[skip]
            return {
                'success': True,
                'emails': [],
                'has_more': False,
                'request_method': 'graph',
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fetch_side_effect) as fetch_mock, \
             patch.object(
                 web_outlook_app,
                 'fetch_email_detail_for_account',
                 return_value=self.detail(matching),
             ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['messages'][0]['id'], 'match-1')
        self.assertEqual(fetch_mock.call_args_list, [
            call(self.account, 'inbox', 0, 50),
            call(self.account, 'inbox', 50, 50),
        ])
        detail_mock.assert_called_once_with(
            self.account,
            'match-1',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_imap_accounts_use_recipient_search_before_generic_scan(self):
        matching = {
            **self.item('imap-match', 'target@example.com', '2026-08-21T12:00:00Z'),
            'folder': 'deleteditems',
            'id_mode': 'uid',
        }

        def imap_side_effect(account, folder, recipient, limit, scan_limit):
            if folder == 'deleteditems':
                return {
                    'success': True,
                    'emails': [matching],
                    'request_method': 'imap',
                    'recipient_search_supported': True,
                }
            return {
                'success': True,
                'emails': [],
                'request_method': 'imap',
                'recipient_search_supported': True,
            }

        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            side_effect=imap_side_effect,
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [self.item('graph-match', 'target@example.com')],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(imap_search_mock.call_args_list, [
            call(self.imap_account, 'inbox', 'target@example.com', 100, 100),
            call(self.imap_account, 'junkemail', 'target@example.com', 100, 100),
            call(self.imap_account, 'deleteditems', 'target@example.com', 100, 100),
        ])
        fetch_mock.assert_not_called()
        detail_mock.assert_called_once_with(
            self.imap_account,
            'imap-match',
            'imap',
            'deleteditems',
            'uid',
            structured_error=True,
        )

    def test_imap_search_unsupported_falls_back_to_existing_scan(self):
        matching = self.item('fallback-match', 'target@example.com')

        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value={
                'success': False,
                'error': {
                    'code': 'IMAP_RECIPIENT_SEARCH_UNSUPPORTED',
                    'status': 501,
                },
                'recipient_search_supported': False,
            },
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [matching],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        imap_search_mock.assert_called_once_with(
            self.imap_account,
            'inbox',
            'target@example.com',
            1,
        )
        fetch_mock.assert_called_once_with(self.imap_account, 'inbox', 0, 50)
        detail_mock.assert_called_once_with(
            self.imap_account,
            'fallback-match',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_imap_search_success_with_no_matches_returns_clean_404_without_scan_fallback(self):
        empty_result = {
            'success': True,
            'emails': [],
            'request_method': 'imap',
            'recipient_search_supported': True,
        }
        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value=empty_result,
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [self.item('fallback-match', 'target@example.com')],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 404,
            'error': '未找到匹配邮件',
        })
        self.assertEqual(imap_search_mock.call_args_list, [
            call(self.imap_account, 'inbox', 'target@example.com', 100, 100),
            call(self.imap_account, 'junkemail', 'target@example.com', 100, 100),
            call(self.imap_account, 'deleteditems', 'target@example.com', 100, 100),
        ])
        fetch_mock.assert_not_called()
        self.assertNotIn('scan_limit_reached', result)
        self.assertNotIn('scanned_count', result)

    def test_imap_search_timeout_returns_504_without_scan_fallback(self):
        imap_error = {
            'success': False,
            'error': {
                'code': 'EMAIL_FETCH_TIMEOUT',
                'status': 504,
                'type': 'TimeoutError',
            },
            'recipient_search_supported': True,
        }
        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value=imap_error,
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={'success': True, 'emails': [], 'has_more': False},
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 504,
            'error': '邮箱服务查询超时',
        })
        fetch_mock.assert_not_called()

    def test_imap_search_generic_error_returns_502_without_scan_fallback(self):
        imap_error = {
            'success': False,
            'error': 'provider failed with password=secret-value',
            'recipient_search_supported': True,
        }
        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value=imap_error,
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={'success': True, 'emails': [], 'has_more': False},
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 502,
            'error': '邮箱服务查询失败',
        })
        fetch_mock.assert_not_called()

    def test_imap_accounts_use_recipient_search_before_generic_scan(self):
        matching = {
            **self.item('imap-match', 'target@example.com', '2026-08-21T12:00:00Z'),
            'folder': 'deleteditems',
            'id_mode': 'uid',
        }

        def imap_side_effect(account, folder, recipient, limit, scan_limit):
            if folder == 'deleteditems':
                return {
                    'success': True,
                    'emails': [matching],
                    'request_method': 'imap',
                    'recipient_search_supported': True,
                }
            return {
                'success': True,
                'emails': [],
                'request_method': 'imap',
                'recipient_search_supported': True,
            }

        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            side_effect=imap_side_effect,
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [self.item('graph-match', 'target@example.com')],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(imap_search_mock.call_args_list, [
            call(self.imap_account, 'inbox', 'target@example.com', 100, 100),
            call(self.imap_account, 'junkemail', 'target@example.com', 100, 100),
            call(self.imap_account, 'deleteditems', 'target@example.com', 100, 100),
        ])
        fetch_mock.assert_not_called()
        detail_mock.assert_called_once_with(
            self.imap_account,
            'imap-match',
            'imap',
            'deleteditems',
            'uid',
            structured_error=True,
        )

    def test_imap_search_unsupported_falls_back_to_existing_scan(self):
        matching = self.item('fallback-match', 'target@example.com')

        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value={
                'success': False,
                'error': {
                    'code': 'IMAP_RECIPIENT_SEARCH_UNSUPPORTED',
                    'status': 501,
                },
                'recipient_search_supported': False,
            },
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [matching],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        imap_search_mock.assert_called_once_with(
            self.imap_account,
            'inbox',
            'target@example.com',
            100,
            100,
        )
        self.assertEqual(fetch_mock.call_args_list, [call(self.imap_account, 'inbox', 0, 50)])
        detail_mock.assert_called_once_with(
            self.imap_account,
            'fallback-match',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_imap_search_success_with_no_matches_returns_clean_404_without_scan_fallback(self):
        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            return_value={
                'success': True,
                'emails': [],
                'request_method': 'imap',
                'recipient_search_supported': True,
                'scanned_count': 2,
                'scan_limit_reached': True,
            },
        ) as imap_search_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [self.item('fallback-match', 'target@example.com')],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.imap_account,
                'target@example.com',
                1,
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 404)
        self.assertEqual(imap_search_mock.call_args_list, [
            call(self.imap_account, 'inbox', 'target@example.com', 100, 100),
            call(self.imap_account, 'junkemail', 'target@example.com', 100, 100),
            call(self.imap_account, 'deleteditems', 'target@example.com', 100, 100),
        ])
        fetch_mock.assert_not_called()

    def test_imap_recipient_search_wrapper_passes_scan_limit_to_low_level_helper(self):
        with patch.object(
            web_outlook_app,
            'get_emails_imap_generic_by_recipient',
            return_value={
                'success': True,
                'emails': [self.item('imap-match', 'target@example.com')],
                'method': 'IMAP (Generic Recipient Search)',
                'has_more': False,
                'recipient_search_supported': True,
                'scanned_count': 1,
                'scan_limit_reached': False,
            },
        ) as helper_mock, patch.object(
            web_outlook_app,
            'get_account_proxy_url',
            return_value='socks5://proxy.local:1080',
        ):
            result = web_outlook_app.fetch_account_imap_emails_by_recipient(
                self.imap_account,
                'inbox',
                'target@example.com',
                1,
                25,
            )

        self.assertTrue(result['success'])
        helper_mock.assert_called_once_with(
            self.imap_account['email'],
            self.imap_account.get('imap_password', ''),
            self.imap_account.get('imap_host', ''),
            self.imap_account.get('imap_port', 993),
            'inbox',
            self.imap_account.get('provider', 'custom'),
            'target@example.com',
            1,
            'socks5://proxy.local:1080',
            25,
        )
        self.assertEqual(result['request_method'], 'imap')
        self.assertEqual(result['scanned_count'], 1)
        self.assertFalse(result['scan_limit_reached'])

    def test_non_imap_accounts_never_call_imap_recipient_search(self):
        matching = self.item('graph-match', 'target@example.com')

        with patch.object(
            web_outlook_app,
            'fetch_account_imap_emails_by_recipient',
            create=True,
            side_effect=AssertionError('IMAP search should not run for non-IMAP accounts'),
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [matching],
                'has_more': False,
                'request_method': 'graph',
            },
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(fetch_mock.call_args_list, [
            call(self.account, 'inbox', 0, 50),
        ])
        detail_mock.assert_called_once_with(
            self.account,
            'graph-match',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_sorts_matching_messages_by_received_time(self):
        older = self.item('older', 'target@example.com', '2026-08-20T10:00:00Z')
        newer = self.item('newer', 'target@example.com', '2026-08-21T10:00:00Z')
        page = {
            'success': True,
            'emails': [older, newer],
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=page), \
             patch.object(
                 web_outlook_app,
                 'fetch_email_detail_for_account',
                 side_effect=[self.detail(newer), self.detail(older)],
             ):
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                2,
            )

        self.assertEqual(
            [message['id'] for message in result['messages']],
            ['newer', 'older'],
        )

    def test_deduplicates_provider_messages(self):
        matching = self.item('duplicate', 'target@example.com')
        page = {
            'success': True,
            'emails': [matching, dict(matching)],
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=page), \
             patch.object(
                 web_outlook_app,
                 'fetch_email_detail_for_account',
                 return_value=self.detail(matching),
             ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                2,
            )

        self.assertEqual(result['count'], 1)
        detail_mock.assert_called_once()

    def test_stops_early_when_provider_has_no_more_messages(self):
        page = {
            'success': True,
            'emails': [self.item('other', 'other@example.com')],
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=page) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 404)
        self.assertFalse(result['scan_limit_reached'])
        self.assertEqual(fetch_mock.call_count, 3)

    def test_stops_after_configured_scan_limit_before_fourth_candidate_match(self):
        page = {
            'success': True,
            'emails': [
                self.item('other-1', 'other@example.com'),
                self.item('other-2', 'other@example.com'),
                self.item('other-3', 'other@example.com'),
                self.item('match-4', 'target@example.com'),
            ],
            'has_more': True,
            'request_method': 'graph',
        }

        with patch.object(
            web_outlook_app,
            'get_mailboxes_messages_scanned_count',
            return_value=3,
        ) as scan_limit_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value=page,
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 404)
        self.assertTrue(result['scan_limit_reached'])
        self.assertEqual(result['scanned_count'], 3)
        scan_limit_mock.assert_called_once_with()
        fetch_mock.assert_called_once_with(self.account, 'inbox', 0, 3)

    def test_exactly_consuming_available_candidates_does_not_claim_scan_limit_reached(self):
        page = {
            'success': True,
            'emails': [
                self.item('other-1', 'other@example.com'),
                self.item('other-2', 'other@example.com'),
                self.item('other-3', 'other@example.com'),
            ],
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(
            web_outlook_app,
            'get_mailboxes_messages_scanned_count',
            return_value=3,
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value=page,
        ) as fetch_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 404)
        self.assertFalse(result['scan_limit_reached'])
        self.assertEqual(result['scanned_count'], 3)
        fetch_mock.assert_called_once_with(self.account, 'inbox', 0, 3)

    def test_finding_match_at_configured_scan_limit_boundary_succeeds(self):
        matching = self.item('match-3', 'Hide My Email <target@example.com>')
        page = {
            'success': True,
            'emails': [
                self.item('other-1', 'other@example.com'),
                self.item('other-2', 'other@example.com'),
                matching,
                self.item('other-4', 'other@example.com'),
            ],
            'has_more': True,
            'request_method': 'graph',
        }

        with patch.object(
            web_outlook_app,
            'get_mailboxes_messages_scanned_count',
            return_value=3,
        ) as scan_limit_mock, patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value=page,
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value=self.detail(matching),
        ) as detail_mock:
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['messages'][0]['id'], 'match-3')
        scan_limit_mock.assert_called_once_with()
        fetch_mock.assert_called_once_with(self.account, 'inbox', 0, 3)
        detail_mock.assert_called_once_with(
            self.account,
            'match-3',
            'graph',
            'inbox',
            'graph',
            structured_error=True,
        )

    def test_maps_structured_timeout_to_504(self):
        upstream = {
            'success': False,
            'error': {
                'code': 'EMAIL_FETCH_TIMEOUT',
                'status': 504,
                'type': 'TimeoutError',
                'details': 'refresh_token=secret-value',
            },
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=upstream):
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 504,
            'error': '邮箱服务查询超时',
        })

    def test_public_mailbox_upstream_call_timeout_maps_to_504(self):
        def slow_fetch(*_args, **_kwargs):
            web_outlook_app.time.sleep(2)
            return {'success': True, 'emails': [], 'has_more': False}

        with patch.object(web_outlook_app, 'PUBLIC_MAILBOX_FETCH_TIMEOUT_SECONDS', 1), \
             patch.object(web_outlook_app, 'fetch_account_emails', side_effect=slow_fetch):
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 504,
            'error': '邮箱服务查询超时',
        })

    def test_maps_generic_upstream_failure_to_502(self):
        with patch.object(web_outlook_app, 'fetch_account_emails', return_value={
            'success': False,
            'error': 'provider failed with password=secret-value',
        }):
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 502,
            'error': '邮箱服务查询失败',
        })

    def test_maps_detail_failure_to_502(self):
        matching = self.item('match-1', 'target@example.com')
        page = {
            'success': True,
            'emails': [matching],
            'has_more': False,
            'request_method': 'imap',
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=page), \
             patch.object(web_outlook_app, 'fetch_email_detail_for_account', return_value={
                 'success': False,
                 'error': 'password=secret-value',
             }):
            result = web_outlook_app.find_public_mailbox_messages(
                self.account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 502,
            'error': '邮箱服务查询失败',
        })

    def test_maps_nested_detail_timeout_to_504_without_mocking_detail_fetch(self):
        account = {
            **self.account,
            'client_id': 'client-id',
            'refresh_token': 'refresh-token',
        }
        matching = self.item('match-1', 'target@example.com')
        page = {
            'success': True,
            'emails': [matching],
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=page), \
             patch.object(web_outlook_app, 'get_access_token_graph', return_value='graph-token'), \
             patch.object(
                 web_outlook_app,
                 'get_with_proxy_fallback',
                 side_effect=TimeoutError('graph detail timed out'),
             ), \
             patch.object(web_outlook_app, 'get_access_token_imap', return_value='imap-token'), \
             patch.object(
                 web_outlook_app.imaplib,
                 'IMAP4_SSL',
                 side_effect=TimeoutError('imap detail timed out'),
             ):
            result = web_outlook_app.find_public_mailbox_messages(
                account,
                'target@example.com',
                1,
            )

        self.assertEqual(result, {
            'success': False,
            'status': 504,
            'error': '邮箱服务查询超时',
        })

class PublicMailboxMessagesApiTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.account = {
            'id': 7,
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM public_mailbox_api_keys')
            db.execute('DELETE FROM recipient_mail_links')
            db.commit()
            created = web_outlook_app.create_public_mailbox_api_key(
                'Route test', '', None
            )
            self.api_key_id = created['id']
            self.api_key = web_outlook_app.get_public_mailbox_api_key_secret(
                self.api_key_id
            )
            web_outlook_app.set_public_mailbox_api_key_auth_enabled(True)

    def get_messages(self, url, headers=None):
        request_headers = {'X-API-Key': self.api_key}
        request_headers.update(headers or {})
        return self.client.get(url, headers=request_headers)

    def create_bound_api_key(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'INSERT OR IGNORE INTO accounts (email) VALUES (?)',
                ('private-owner@example.com',),
            )
            account_id = db.execute(
                'SELECT id FROM accounts WHERE email = ?',
                ('private-owner@example.com',),
            ).fetchone()['id']
            created = web_outlook_app.create_public_mailbox_api_key(
                name='Bound route key',
                secret='pmk_bound_route_key',
                account_id=account_id,
            )
            secret = web_outlook_app.get_public_mailbox_api_key_secret(
                created['id']
            )
        return account_id, secret

    def insert_account(self, email='owner@example.com'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            existing = db.execute(
                'SELECT id FROM accounts WHERE email = ?',
                (email,),
            ).fetchone()
            if existing:
                return int(existing['id'])
            cursor = db.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', '', '', 1, '', 'active', 'outlook', 'outlook', '', 993, '', 0)
                """,
                (email,),
            )
            db.commit()
            return int(cursor.lastrowid)

    def seed_public_link(
        self,
        recipient_display='Recipient01@iCloud.com',
        *,
        account_email='owner@example.com',
        account_id=None,
        main_email='Owner@Example.com',
        expires_at=None,
    ):
        if account_id is None:
            account_id = self.insert_account(account_email)
        with self.app.app_context():
            db = web_outlook_app.get_db()
            recipient = web_outlook_app.normalize_recipient_email(recipient_display)
            record = web_outlook_app.upsert_recipient_mail_link(
                db,
                account_id,
                main_email,
                recipient.display,
                recipient.normalized,
                expires_at,
            )
            db.commit()
            return record

    def get_public_link_row(self, record_id):
        with self.app.app_context():
            return web_outlook_app.get_db().execute(
                """
                SELECT id, account_id, primary_access_count, last_accessed_at, updated_at
                FROM recipient_mail_links
                WHERE id = ?
                """,
                (record_id,),
            ).fetchone()

    def get_account_share_segment(self, account_id):
        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                'SELECT recipient_share_segment FROM accounts WHERE id = ?',
                (int(account_id),),
            ).fetchone()
        return row['recipient_share_segment']

    def get_replication_event_count(self):
        with self.app.app_context():
            return int(
                web_outlook_app.get_db().execute(
                    'SELECT COUNT(*) AS count FROM replication_events'
                ).fetchone()['count']
            )

    def assert_public_token_headers(self, response):
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['X-Robots-Tag'], 'noindex')

    def assert_html_response(self, response, status, expected_text):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        self.assertEqual(response.get_data(as_text=True), expected_text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('sandbox', response.headers['Content-Security-Policy'])

    def log_in_for_settings(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True

    @staticmethod
    def success_result(body='<p>mail body</p>', body_type='html'):
        return {
            'success': True,
            'status': 200,
            'count': 1,
            'messages': [{
                'id': 'message-1',
                'subject': 'Verification code',
                'from': 'sender@example.com',
                'to': 'Hide My Email <01litany_muster@icloud.com>',
                'received_at': '2026-08-21T10:00:00Z',
                'body': body,
                'body_type': body_type,
            }],
        }

    @staticmethod
    def success_messages_result(count):
        return {
            'success': True,
            'status': 200,
            'count': count,
            'messages': [
                {
                    'id': f'message-{index}',
                    'subject': f'Verification code {index:02d}',
                    'from': 'sender@example.com',
                    'to': 'Hide My Email <01litany_muster@icloud.com>',
                    'received_at': f'2026-08-21T10:{index:02d}:00Z',
                    'body': f'<p>mail body {index}</p>',
                    'body_type': 'html',
                }
                for index in range(1, count + 1)
            ],
        }

    def test_public_html_response_accepts_header_key_without_session(self):
        with self.client.session_transaction() as session:
            self.assertNotIn('logged_in', session)

        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com&limit=1&format=html'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '<p>mail body</p>')
        self.assertEqual(response.mimetype, 'text/html')
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(
            response.headers['Content-Security-Policy'],
            "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
        )
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_missing_format_and_limit_defaults_to_html_response(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '<p>mail body</p>')
        self.assertEqual(response.mimetype, 'text/html')
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        search_mock.assert_called_once_with(
            self.account,
            '01litany_muster@icloud.com',
            1,
        )

    def test_blank_format_and_limit_default_to_html_response(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com&limit=&format='
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '<p>mail body</p>')
        self.assertEqual(response.mimetype, 'text/html')
        search_mock.assert_called_once_with(
            self.account,
            '01litany_muster@icloud.com',
            1,
        )

    def test_whitespace_encoded_format_and_limit_default_to_html_response(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com&limit=%20%20%20&format=%20%20%20'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '<p>mail body</p>')
        self.assertEqual(response.mimetype, 'text/html')
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        search_mock.assert_called_once_with(
            self.account,
            '01litany_muster@icloud.com',
            1,
        )

    def test_missing_auth_setting_defaults_off_and_preserves_public_route(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'DELETE FROM settings WHERE key = ?',
                ('public_mailbox_api_key_auth_enabled',),
            )
            db.commit()
        with patch.object(
            web_outlook_app,
            'authenticate_public_mailbox_api_key',
        ) as auth_mock, patch.object(
            web_outlook_app,
            'normalize_api_key',
        ) as normalize_mock, patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 200)
        auth_mock.assert_not_called()
        normalize_mock.assert_not_called()

    def test_settings_get_returns_normalized_scan_count(self):
        self.log_in_for_settings()

        with self.app.app_context():
            self.assertTrue(web_outlook_app.set_setting('mailboxes_messages_scanned_count', '0'))

        response = self.client.get('/api/settings')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['settings']['mailboxes_messages_scanned_count'], 100)

    def test_settings_put_persists_scan_count_as_canonical_decimal_string(self):
        self.log_in_for_settings()

        response = self.client.put(
            '/api/settings',
            json={'mailboxes_messages_scanned_count': '275'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])

        with self.app.app_context():
            self.assertEqual(
                web_outlook_app.get_setting('mailboxes_messages_scanned_count'),
                '275',
            )
            self.assertEqual(
                web_outlook_app.get_mailboxes_messages_scanned_count(),
                275,
            )

        refresh_response = self.client.get('/api/settings')
        refresh_payload = refresh_response.get_json()
        self.assertTrue(refresh_payload['success'])
        self.assertEqual(
            refresh_payload['settings']['mailboxes_messages_scanned_count'],
            275,
        )

    def test_settings_put_rejects_invalid_scan_count_without_overwriting_existing_value(self):
        self.log_in_for_settings()

        with self.app.app_context():
            self.assertTrue(web_outlook_app.set_setting('mailboxes_messages_scanned_count', '275'))

        response = self.client.put(
            '/api/settings',
            json={'mailboxes_messages_scanned_count': True},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error'], '最多扫描邮件数必须是 1 到 10000 之间的整数')

        with self.app.app_context():
            self.assertEqual(
                web_outlook_app.get_setting('mailboxes_messages_scanned_count'),
                '275',
            )
            self.assertEqual(
                web_outlook_app.get_mailboxes_messages_scanned_count(),
                275,
            )

    def test_auth_off_ignores_invalid_header_and_query_without_key_lookup(self):
        with self.app.app_context():
            web_outlook_app.set_public_mailbox_api_key_auth_enabled(False)
        with patch.object(
            web_outlook_app,
            'authenticate_public_mailbox_api_key',
        ) as auth_mock, patch.object(
            web_outlook_app,
            'normalize_api_key',
        ) as normalize_mock, patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json&key=deleted-or-expired',
                headers={'X-API-Key': 'invalid-header'},
            )

        self.assertEqual(response.status_code, 200)
        auth_mock.assert_not_called()
        normalize_mock.assert_not_called()
        with self.app.app_context():
            last_used_at = web_outlook_app.get_db().execute(
                'SELECT last_used_at FROM public_mailbox_api_keys WHERE id = ?',
                (self.api_key_id,),
            ).fetchone()['last_used_at']
        self.assertIsNone(last_used_at)

    def test_missing_key_returns_401_before_query_validation_or_resolution(self):
        with patch.object(
            web_outlook_app, 'resolve_account_by_address'
        ) as resolve_mock, patch.object(
            web_outlook_app, 'find_public_mailbox_messages'
        ) as search_mock:
            response = self.client.get('/api/v1/mailboxes/messages')

        self.assert_html_response(response, 401, '缺少 API 密钥')
        resolve_mock.assert_not_called()
        search_mock.assert_not_called()

    def test_json_auth_errors_remain_json(self):
        cases = (
            ({}, 401, '缺少 API 密钥'),
            ({'X-API-Key': 'invalid-key'}, 403, 'API 密钥无效或已过期'),
        )
        for headers, status, error in cases:
            with self.subTest(status=status), patch.object(
                web_outlook_app, 'resolve_account_by_address'
            ) as resolve_mock:
                response = self.client.get(
                    '/api/v1/mailboxes/messages?format=json',
                    headers=headers,
                )

            self.assertEqual(response.status_code, status)
            self.assertEqual(response.content_type, 'application/json')
            self.assertEqual(response.get_json(), {
                'success': False,
                'error': error,
            })
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            resolve_mock.assert_not_called()

    def test_query_key_authorizes_json_request(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json&key=' + self.api_key
            )
        self.assertEqual(response.status_code, 200)

    def test_present_invalid_header_never_falls_back_to_valid_query_key(self):
        with patch.object(
            web_outlook_app, 'resolve_account_by_address'
        ) as resolve_mock:
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&key=' + self.api_key,
                headers={'X-API-Key': ''},
            )
        self.assert_html_response(response, 403, 'API 密钥无效或已过期')
        resolve_mock.assert_not_called()

    def test_invalid_deleted_and_expired_keys_share_one_403_contract(self):
        with self.app.app_context():
            deleted = web_outlook_app.create_public_mailbox_api_key(
                'Deleted', '', None
            )
            deleted_secret = web_outlook_app.get_public_mailbox_api_key_secret(
                deleted['id']
            )
            web_outlook_app.delete_public_mailbox_api_key(deleted['id'])
            expired = web_outlook_app.create_public_mailbox_api_key(
                'Expired', '', '2000-01-01T00:00:00Z'
            )
            expired_secret = web_outlook_app.get_public_mailbox_api_key_secret(
                expired['id']
            )
        for candidate in (
            'pmk_random-invalid-key',
            deleted_secret,
            expired_secret,
        ):
            with self.subTest(candidate=candidate), patch.object(
                web_outlook_app, 'resolve_account_by_address'
            ) as resolve_mock:
                response = self.client.get(
                    '/api/v1/mailboxes/messages?format=html',
                    headers={'X-API-Key': candidate},
                )
                self.assert_html_response(response, 403, 'API 密钥无效或已过期')
                resolve_mock.assert_not_called()

    def test_last_used_timestamp_is_throttled_for_sixty_seconds(self):
        moments = (
            datetime(2026, 8, 22, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 22, 2, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 22, 2, 1, 1, tzinfo=timezone.utc),
        )
        url = (
            '/api/v1/mailboxes/messages?mainemail=owner@example.com'
            '&email=target@example.com'
        )
        with patch.object(
            web_outlook_app,
            'public_mailbox_api_key_utc_now',
            side_effect=moments,
        ), patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            self.get_messages(url)
            with self.app.app_context():
                first = web_outlook_app.get_db().execute(
                    'SELECT last_used_at FROM public_mailbox_api_keys'
                ).fetchone()['last_used_at']
            self.get_messages(url)
            with self.app.app_context():
                second = web_outlook_app.get_db().execute(
                    'SELECT last_used_at FROM public_mailbox_api_keys'
                ).fetchone()['last_used_at']
            self.get_messages(url)
            with self.app.app_context():
                third = web_outlook_app.get_db().execute(
                    'SELECT last_used_at FROM public_mailbox_api_keys'
                ).fetchone()['last_used_at']

        self.assertEqual(first, '2026-08-22T02:00:00Z')
        self.assertEqual(second, first)
        self.assertEqual(third, '2026-08-22T02:01:01Z')

    def test_valid_key_records_use_even_when_query_validation_fails(self):
        moment = datetime(2026, 8, 22, 4, 0, 0, tzinfo=timezone.utc)
        with patch.object(
            web_outlook_app,
            'public_mailbox_api_key_utc_now',
            return_value=moment,
        ), patch.object(
            web_outlook_app, 'resolve_account_by_address'
        ) as resolve_mock:
            response = self.get_messages('/api/v1/mailboxes/messages')

        self.assertEqual(response.status_code, 400)
        resolve_mock.assert_not_called()
        with self.app.app_context():
            last_used_at = web_outlook_app.get_db().execute(
                'SELECT last_used_at FROM public_mailbox_api_keys'
            ).fetchone()['last_used_at']
        self.assertEqual(last_used_at, '2026-08-22T04:00:00Z')

    def test_plain_text_html_response_is_escaped(self):
        result = self.success_result(
            '<script>alert(1)</script>\nsecond line',
            'text',
        )
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=result,
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com&format=html'
            )

        self.assertEqual(
            response.get_data(as_text=True),
            '<pre>&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line</pre>',
        )

    def test_public_token_html_response_succeeds_without_api_key_and_counts_primary_access(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=account,
        ) as account_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(body='<b>123456</b>', body_type='html'),
        ) as search_mock:
            response = self.client.get(f"/api/v2/mailboxes/{link['token']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        self.assertEqual(response.get_data(as_text=True), '<b>123456</b>')
        self.assert_public_token_headers(response)
        account_mock.assert_called_once_with(int(link['account_id']))
        search_mock.assert_called_once_with(
            account,
            'recipient01@icloud.com',
            1,
        )
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 1)
        self.assertIsNotNone(row['last_accessed_at'])

    def test_public_show_and_query_routes_use_shared_segment_and_return_expected_formats(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        shared = self.get_account_share_segment(link['account_id'])
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=account,
        ) as account_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(body='<b>123456</b>', body_type='html'),
        ) as search_mock:
            show_response = self.client.get(f"/show/{shared}/Recipient01@iCloud.com")

        self.assertEqual(show_response.status_code, 200)
        self.assertEqual(show_response.content_type, 'text/html; charset=utf-8')
        show_html = show_response.get_data(as_text=True)
        self.assertIn('邮箱展 - Recipient01@iCloud.com', show_html)
        self.assertIn('最新邮件', show_html)
        self.assertIn('Verification code', show_html)
        self.assertIn('srcdoc=', show_html)
        self.assertIn('查看更多', show_html)
        self.assertNotIn('邮件列表', show_html)
        self.assertNotIn('class="mail-item"', show_html)
        self.assert_public_token_headers(show_response)
        account_mock.assert_called_once_with(int(link['account_id']))
        search_mock.assert_called_once_with(
            account,
            'recipient01@icloud.com',
            1,
        )
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 1)
        self.assertIsNotNone(row['last_accessed_at'])

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=account,
        ) as account_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(body='<p>json body</p>', body_type='html'),
        ) as search_mock:
            query_response = self.client.get(f"/query/{shared}/Recipient01@iCloud.com")

        self.assertEqual(query_response.status_code, 200)
        self.assertTrue(query_response.content_type.startswith('application/json'))
        self.assertTrue(query_response.get_json()['success'])
        self.assert_public_token_headers(query_response)
        account_mock.assert_called_once_with(int(link['account_id']))
        search_mock.assert_called_once_with(
            account,
            'recipient01@icloud.com',
            1,
        )
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 2)
        self.assertIsNotNone(row['last_accessed_at'])

    def test_public_show_route_accepts_plus_alias_for_same_mailbox(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        shared = self.get_account_share_segment(link['account_id'])
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=account,
        ) as account_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(body='<b>123456</b>', body_type='html'),
        ) as search_mock:
            response = self.client.get(f"/show/{shared}/Recipient01+promo@iCloud.com")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        response_html = response.get_data(as_text=True)
        self.assertIn('最新邮件', response_html)
        self.assertIn('Verification code', response_html)
        self.assertIn('查看更多', response_html)
        self.assert_public_token_headers(response)
        account_mock.assert_called_once_with(int(link['account_id']))
        search_mock.assert_called_once_with(
            account,
            'recipient01+promo@icloud.com',
            1,
        )
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 1)
        self.assertIsNotNone(row['last_accessed_at'])

    def test_public_show_route_expands_to_accordion_view_when_requested(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        shared = self.get_account_share_segment(link['account_id'])
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_messages_result(40),
        ) as search_mock:
            response = self.client.get(f"/show/{shared}/Recipient01@iCloud.com?all=1")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('已加载 40 封', html)
        self.assertIn('查看更多', html)
        self.assertIn('收起邮件', html)
        self.assertIn('邮件列表', html)
        self.assertIn('class="mail-item"', html)
        self.assertIn('limit=40', html)
        search_mock.assert_called_once_with(
            account,
            'recipient01@icloud.com',
            20,
        )

    def test_touch_primary_recipient_link_keeps_updated_at_and_replication_events_unchanged(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        record_id = int(link['id'])
        original_updated_at = '2026-08-01T00:00:00Z'

        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM replication_events')
            db.execute(
                """
                UPDATE recipient_mail_links
                SET primary_access_count = 0,
                    last_accessed_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (original_updated_at, record_id),
            )
            db.commit()
            db.execute('DELETE FROM replication_events')
            db.commit()

            web_outlook_app.touch_primary_recipient_link(record_id)

        row = self.get_public_link_row(record_id)
        self.assertEqual(int(row['primary_access_count'] or 0), 1)
        self.assertIsNotNone(row['last_accessed_at'])
        self.assertEqual(row['updated_at'], original_updated_at)
        self.assertEqual(self.get_replication_event_count(), 0)

    def test_public_token_read_stays_available_when_access_count_write_fails(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        for exc in (
            sqlite3.OperationalError('counter write failed'),
            RuntimeError('counter write exploded'),
        ):
            with self.subTest(exc_type=type(exc).__name__), patch.object(
                web_outlook_app,
                'get_account_by_id',
                return_value=account,
            ) as account_mock, patch.object(
                web_outlook_app,
                'touch_primary_recipient_link',
                side_effect=exc,
            ) as touch_mock, patch.object(
                web_outlook_app,
                'find_public_mailbox_messages',
                return_value=self.success_result(body='<b>123456</b>', body_type='html'),
            ) as search_mock, patch.object(
                web_outlook_app.app.logger,
                'exception',
            ) as logger_mock:
                response = self.client.get(f"/api/v2/mailboxes/{link['token']}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, 'text/html; charset=utf-8')
            self.assertEqual(response.get_data(as_text=True), '<b>123456</b>')
            self.assert_public_token_headers(response)
            self.assertNotIn(str(exc), response.get_data(as_text=True))
            account_mock.assert_called_once_with(int(link['account_id']))
            touch_mock.assert_called_once_with(int(link['id']))
            search_mock.assert_called_once_with(
                account,
                'recipient01@icloud.com',
                1,
            )
            logger_mock.assert_called_once()
            row = self.get_public_link_row(int(link['id']))
            self.assertEqual(int(row['primary_access_count'] or 0), 0)
            self.assertIsNone(row['last_accessed_at'])

    def test_public_token_responses_map_upstream_results_to_html_and_increment_access_count(self):
        cases = (
            ({
                'success': False,
                'status': 404,
                'error': 'upstream not found',
            }, 200, '\u5f53\u524d\u65e0\u90ae\u4ef6', 'Recipient01@iCloud.com'),
            ({
                'success': False,
                'status': 504,
                'error': 'upstream timeout',
            }, 504, 'upstream timeout', 'Recipient02@iCloud.com'),
            ({
                'success': False,
                'status': 502,
                'error': 'upstream failure',
            }, 502, 'upstream failure', 'Recipient03@iCloud.com'),
        )

        for result, expected_status, expected_body, recipient_display in cases:
            link = self.seed_public_link(recipient_display)
            account = {
                'id': int(link['account_id']),
                'email': 'owner@example.com',
                'account_type': 'outlook',
            }
            with self.subTest(upstream_status=result['status']), patch.object(
                web_outlook_app,
                'get_account_by_id',
                return_value=account,
            ), patch.object(
                web_outlook_app,
                'find_public_mailbox_messages',
                return_value=result,
            ) as search_mock:
                response = self.client.get(f"/api/v2/mailboxes/{link['token']}")

            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.content_type, 'text/html; charset=utf-8')
            self.assertEqual(response.get_data(as_text=True), expected_body)
            self.assert_public_token_headers(response)
            search_mock.assert_called_once_with(
                account,
                recipient_display.lower(),
                1,
            )
            row = self.get_public_link_row(int(link['id']))
            self.assertEqual(int(row['primary_access_count'] or 0), 1)

    def test_public_token_invalid_and_missing_tokens_return_404_without_access_count(self):
        link = self.seed_public_link('Recipient01@iCloud.com')

        with patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            side_effect=AssertionError('finder should not be called'),
        ):
            invalid_response = self.client.get('/api/v2/mailboxes/not-a-real-token')
            missing_response = self.client.get('/api/v2/mailboxes/')

        self.assertEqual(invalid_response.status_code, 404)
        self.assertEqual(invalid_response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(invalid_response)
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(missing_response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(missing_response)
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 0)
        self.assertIsNone(row['last_accessed_at'])

    def test_public_token_expired_deleted_and_missing_bound_account_do_not_increment_access_count(self):
        expired_link = self.seed_public_link(
            'Recipient02@iCloud.com',
            expires_at='2000-01-01T00:00:00Z',
        )
        deleted_link = self.seed_public_link('Recipient03@iCloud.com')
        orphan_link = self.seed_public_link(
            'Recipient04@iCloud.com',
            account_email='orphan-owner@example.com',
            main_email='Orphan@Example.com',
        )

        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'DELETE FROM recipient_mail_links WHERE id = ?',
                (int(deleted_link['id']),),
            )
            db.execute(
                'DELETE FROM accounts WHERE id = ?',
                (int(orphan_link['account_id']),),
            )
            db.commit()

        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            side_effect=AssertionError('account lookup should not run for expired or orphaned links'),
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            side_effect=AssertionError('finder should not be called'),
        ):
            expired_response = self.client.get(f"/api/v2/mailboxes/{expired_link['token']}")
            deleted_response = self.client.get(f"/api/v2/mailboxes/{deleted_link['token']}")
            orphan_response = self.client.get(f"/api/v2/mailboxes/{orphan_link['token']}")

        self.assertEqual(expired_response.status_code, 410)
        self.assertEqual(expired_response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(expired_response)
        self.assertEqual(deleted_response.status_code, 404)
        self.assertEqual(deleted_response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(deleted_response)
        self.assertEqual(orphan_response.status_code, 404)
        self.assertEqual(orphan_response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(orphan_response)
        expired_row = self.get_public_link_row(int(expired_link['id']))
        self.assertEqual(int(expired_row['primary_access_count'] or 0), 0)
        self.assertIsNone(expired_row['last_accessed_at'])

    def test_json_response_returns_approved_fields_and_no_store(self):
        result = self.success_result()
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=result,
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=01litany_muster@icloud.com&format=json'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'success': True,
            'count': 1,
            'messages': result['messages'],
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Mailbox-Node'], 'primary')

    def test_route_preserves_plus_addresses_and_uses_mainemail_for_resolution(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ) as resolve_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=alias+team@example.com'
                '&email=target+tag@example.com&format=json'
            )

        self.assertEqual(response.status_code, 200)
        resolve_mock.assert_called_once_with('alias+team@example.com')
        search_mock.assert_called_once_with(
            self.account,
            'target+tag@example.com',
            1,
        )

    def test_route_uses_recipient_for_account_resolution_when_mainemail_is_missing(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ) as resolve_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 200)
        resolve_mock.assert_called_once_with('target@example.com')
        search_mock.assert_called_once_with(
            self.account,
            'target@example.com',
            1,
        )

    def test_primary_app_does_not_start_replica_sync_worker_on_request(self):
        with patch.object(web_outlook_app, 'start_replica_sync_worker') as start_mock:
            response = self.client.get('/health/live')

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        start_mock.assert_not_called()

    def test_bound_key_header_and_url_parameter_resolve_account_without_mainemail(self):
        account_id, bound_secret = self.create_bound_api_key()
        bound_account = {**self.account, 'id': account_id, 'email': 'private-owner@example.com'}
        requests = (
            (
                '/api/v1/mailboxes/messages?email=target@example.com&format=json',
                {'X-API-Key': bound_secret},
            ),
            (
                '/api/v1/mailboxes/messages?email=target@example.com'
                '&format=json&key=' + bound_secret,
                {},
            ),
        )

        for url, headers in requests:
            with self.subTest(url=url), patch.object(
                web_outlook_app,
                'get_account_by_id',
                return_value=bound_account,
            ) as get_account_mock, patch.object(
                web_outlook_app,
                'resolve_account_by_address',
            ) as resolve_mock, patch.object(
                web_outlook_app,
                'find_public_mailbox_messages',
                return_value=self.success_result(),
            ) as search_mock:
                response = self.client.get(url, headers=headers)

            self.assertEqual(response.status_code, 200)
            get_account_mock.assert_called_once_with(account_id)
            resolve_mock.assert_not_called()
            search_mock.assert_called_once_with(
                bound_account,
                'target@example.com',
                1,
            )

    def test_bound_key_accepts_matching_legacy_mainemail(self):
        account_id, bound_secret = self.create_bound_api_key()
        bound_account = {**self.account, 'id': account_id, 'email': 'private-owner@example.com'}
        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=bound_account,
        ), patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=bound_account,
        ) as resolve_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=private-owner@example.com'
                '&email=target@example.com&format=json',
                headers={'X-API-Key': bound_secret},
            )

        self.assertEqual(response.status_code, 200)
        resolve_mock.assert_called_once_with('private-owner@example.com')
        search_mock.assert_called_once()

    def test_bound_key_rejects_mismatched_legacy_mainemail(self):
        account_id, bound_secret = self.create_bound_api_key()
        bound_account = {**self.account, 'id': account_id, 'email': 'private-owner@example.com'}
        other_account = {**self.account, 'id': account_id + 1, 'email': 'other@example.com'}
        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=bound_account,
        ), patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=other_account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
        ) as search_mock:
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=other@example.com'
                '&email=target@example.com',
                headers={'X-API-Key': bound_secret},
            )

        self.assert_html_response(response, 403, 'API 密钥无权访问该主邮箱')
        search_mock.assert_not_called()

    def test_bound_key_mismatch_preserves_json_error_contract(self):
        account_id, bound_secret = self.create_bound_api_key()
        bound_account = {**self.account, 'id': account_id, 'email': 'private-owner@example.com'}
        with patch.object(
            web_outlook_app,
            'get_account_by_id',
            return_value=bound_account,
        ), patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=None,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
        ) as search_mock:
            response = self.client.get(
                '/api/v1/mailboxes/messages?mainemail=other@example.com'
                '&email=target@example.com&format=json&key=' + bound_secret,
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content_type, 'application/json')
        self.assertEqual(response.get_json(), {
            'success': False,
            'error': 'API 密钥无权访问该主邮箱',
        })
        search_mock.assert_not_called()

    def test_route_does_not_fallback_from_plus_tagged_mainemail(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=None,
        ) as exact_resolve_mock, patch.object(
            web_outlook_app,
            'resolve_account_for_email_api',
            return_value=self.account,
        ) as fallback_resolve_mock, patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=alias+fallback@example.com'
                '&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 404)
        exact_resolve_mock.assert_called_once_with('alias+fallback@example.com')
        fallback_resolve_mock.assert_not_called()

    def test_html_validation_errors_return_html_without_caching(self):
        invalid_cases = (
            (
                '/api/v1/mailboxes/messages',
                'email 参数缺失或格式无效',
            ),
            (
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=bad address',
                'email 参数缺失或格式无效',
            ),
            (
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&limit=0',
                'limit 参数必须在 1 到 20 之间',
            ),
            (
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&format=html&limit=2',
                'format=html 时 limit 必须为 1',
            ),
        )

        for url, expected_text in invalid_cases:
            with self.subTest(url=url):
                response = self.get_messages(url)
                self.assert_html_response(response, 400, expected_text)

    def test_json_and_unknown_format_validation_errors_remain_json(self):
        invalid_urls = (
            '/api/v1/mailboxes/messages?format=json',
            '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=bad address&format=json',
            '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&format=xml',
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                response = self.get_messages(url)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()['success'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_unknown_mainemail_returns_404(self):
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=None,
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=missing@example.com'
                '&email=target@example.com'
            )

        self.assert_html_response(response, 404, '主邮箱或别名不存在')

    def test_html_no_match_returns_currently_no_mail_page_with_200(self):
        result = {
            'success': False,
            'status': 404,
            'error': '未在扫描范围内找到匹配邮件',
            'scan_limit_reached': True,
            'scanned_count': 3,
        }
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=result,
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com'
            )

        self.assert_html_response(response, 200, '当前无邮件')

    def test_json_scan_limit_404_keeps_public_boundary_fields(self):
        result = {
            'success': False,
            'status': 404,
            'error': '未在扫描范围内找到匹配邮件',
            'scan_limit_reached': True,
            'scanned_count': 3,
        }
        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=self.account,
        ), patch.object(
            web_outlook_app,
            'find_public_mailbox_messages',
            return_value=result,
        ):
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {
            'success': False,
            'error': '未在扫描范围内找到匹配邮件',
            'scan_limit_reached': True,
            'scanned_count': 3,
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_upstream_status_is_propagated_without_internal_status_field(self):
        for status, error in (
            (502, '邮箱服务查询失败'),
            (504, '邮箱服务查询超时'),
        ):
            with self.subTest(status=status), patch.object(
                web_outlook_app,
                'resolve_account_by_address',
                return_value=self.account,
            ), patch.object(
                web_outlook_app,
                'find_public_mailbox_messages',
                return_value={
                    'success': False,
                    'status': status,
                    'error': error,
                },
            ):
                response = self.get_messages(
                    '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                    '&email=target@example.com'
                )

            self.assert_html_response(response, status, error)

    def test_plain_imap_recipient_search_timeout_returns_504_json_without_scan_fallback(self):
        imap_account = {
            'id': 9,
            'email': 'owner@example.com',
            'account_type': 'imap',
            'imap_password': 'plain-password',
            'imap_host': 'imap.example.com',
            'imap_port': 993,
            'provider': 'custom',
        }

        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=imap_account,
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={'success': True, 'emails': [], 'has_more': False},
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'create_imap_connection',
            side_effect=TimeoutError('plain imap detail timed out'),
        ) as create_imap_connection_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.get_json(), {
            'success': False,
            'error': '\u90ae\u7bb1\u670d\u52a1\u67e5\u8be2\u8d85\u65f6',
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        fetch_mock.assert_not_called()
        self.assertEqual(create_imap_connection_mock.call_count, 3)

    def test_plain_imap_search_timeout_returns_504_json_without_scan_fallback(self):
        imap_account = {
            'id': 9,
            'email': 'owner@example.com',
            'account_type': 'imap',
            'imap_password': 'plain-password',
            'imap_host': 'imap.example.com',
            'imap_port': 993,
            'provider': 'custom',
        }

        class TimeoutSearchMail:
            def __init__(self):
                self.logged_out = False

            def login(self, *_args, **_kwargs):
                return 'OK', [b'logged in']

            def xatom(self, *_args, **_kwargs):
                return 'OK', [b'ID completed']

            def select(self, name, readonly=True):
                if name == 'INBOX':
                    return 'OK', [b'']
                return 'NO', [b'folder not found']

            def list(self):
                return 'OK', [b'(\\HasNoChildren) "." "INBOX"']

            def uid(self, command, *args, **kwargs):
                if command == 'SEARCH':
                    raise TimeoutError('search timed out')
                return 'OK', [b'']

            def search(self, *_args, **_kwargs):
                raise AssertionError('sequence search should not run after timeout')

            def logout(self):
                self.logged_out = True
                return 'BYE', [b'logout']

        mail = TimeoutSearchMail()

        with patch.object(
            web_outlook_app,
            'resolve_account_by_address',
            return_value=imap_account,
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={'success': True, 'emails': [], 'has_more': False},
        ) as fetch_mock, patch.object(
            web_outlook_app,
            'create_imap_connection',
            return_value=mail,
        ) as create_imap_connection_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com'
                '&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.get_json(), {
            'success': False,
            'error': '\u90ae\u7bb1\u670d\u52a1\u67e5\u8be2\u8d85\u65f6',
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        fetch_mock.assert_not_called()
        self.assertEqual(create_imap_connection_mock.call_count, 3)
        self.assertTrue(mail.logged_out)


class ReplicaPublicMailboxMessagesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replica_module, cls._temp_dir, cls._module_name = load_isolated_web_outlook_app(role='replica')

    @classmethod
    def tearDownClass(cls):
        cleanup_isolated_web_outlook_app(
            cls.replica_module,
            cls._temp_dir,
            cls._module_name,
        )

    def setUp(self):
        self.app = self.replica_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.account = {
            'id': 7,
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }
        with self.app.app_context():
            self.replica_module.init_db()
            db = self.replica_module.get_db()
            db.execute('DELETE FROM public_mailbox_api_keys')
            db.execute('DELETE FROM accounts')
            db.execute('DELETE FROM recipient_mail_links')
            db.execute(
                "DELETE FROM cluster_replica_state WHERE key IN ('last_success_at', 'node_id', 'protocol_version', 'last_error')"
            )
            db.commit()
            created = self.replica_module.create_public_mailbox_api_key(
                'Replica route test', '', None
            )
            self.api_key = self.replica_module.get_public_mailbox_api_key_secret(created['id'])
            self.replica_module.set_public_mailbox_api_key_auth_enabled(True)

    def get_messages(self, url, headers=None):
        request_headers = {'X-API-Key': self.api_key}
        request_headers.update(headers or {})
        return self.client.get(url, headers=request_headers)

    def insert_account(self, email='owner@example.com'):
        with self.app.app_context():
            db = self.replica_module.get_db()
            existing = db.execute(
                'SELECT id FROM accounts WHERE email = ?',
                (email,),
            ).fetchone()
            if existing:
                return int(existing['id'])
            cursor = db.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', '', '', 1, '', 'active', 'outlook', 'outlook', '', 993, '', 0)
                """,
                (email,),
            )
            db.commit()
            return int(cursor.lastrowid)

    def seed_public_link(
        self,
        recipient_display='Recipient01@iCloud.com',
        *,
        account_email='owner@example.com',
        account_id=None,
        main_email='Owner@Example.com',
        expires_at=None,
    ):
        if account_id is None:
            account_id = self.insert_account(account_email)
        with self.app.app_context():
            db = self.replica_module.get_db()
            recipient = self.replica_module.normalize_recipient_email(recipient_display)
            record = self.replica_module.upsert_recipient_mail_link(
                db,
                account_id,
                main_email,
                recipient.display,
                recipient.normalized,
                expires_at,
            )
            db.commit()
            return record

    def get_public_link_row(self, record_id):
        with self.app.app_context():
            return self.replica_module.get_db().execute(
                """
                SELECT id, primary_access_count, last_accessed_at
                FROM recipient_mail_links
                WHERE id = ?
                """,
                (record_id,),
            ).fetchone()

    def assert_public_token_headers(self, response):
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['X-Robots-Tag'], 'noindex')

    def set_replica_state(
        self,
        *,
        cursor=0,
        last_success_at: datetime | None,
        node_id='replica-node-1',
        protocol_version=1,
        last_error='',
    ):
        with self.app.app_context():
            db = self.replica_module.get_db()
            entries = {
                'cursor': str(cursor),
                'node_id': node_id,
                'protocol_version': str(protocol_version),
                'last_error': last_error,
            }
            if last_success_at is not None:
                entries['last_success_at'] = last_success_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            else:
                db.execute("DELETE FROM cluster_replica_state WHERE key = 'last_success_at'")
            for key, value in entries.items():
                db.execute(
                    '''
                    INSERT INTO cluster_replica_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    ''',
                    (key, value),
                )
            db.commit()

    @staticmethod
    def success_result():
        return {
            'success': True,
            'status': 200,
            'count': 1,
            'messages': [{
                'id': 'message-1',
                'subject': 'Verification code',
                'from': 'sender@example.com',
                'to': 'target@example.com',
                'received_at': '2026-08-28T08:00:00Z',
                'body': '<p>mail body</p>',
                'body_type': 'html',
            }],
        }

    def test_replica_route_returns_not_ready_before_first_snapshot(self):
        self.set_replica_state(last_success_at=None)

        with patch.object(self.replica_module, 'resolve_account_by_address') as resolve_mock, patch.object(
            self.replica_module,
            'find_public_mailbox_messages',
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error_code'], 'replica_not_ready')
        resolve_mock.assert_not_called()
        search_mock.assert_not_called()

    def test_replica_route_returns_data_expired_when_last_sync_is_older_than_a_day(self):
        self.set_replica_state(
            cursor=40,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=24, seconds=1),
        )

        with patch.object(self.replica_module, 'resolve_account_by_address') as resolve_mock, patch.object(
            self.replica_module,
            'find_public_mailbox_messages',
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error_code'], 'replica_data_expired')
        resolve_mock.assert_not_called()
        search_mock.assert_not_called()

    def test_replica_route_serves_recent_data_and_sets_mailbox_node_header(self):
        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=23),
            node_id='replica-node-23h',
        )

        with patch.object(
            self.replica_module,
            'resolve_account_by_address',
            return_value=self.account,
        ) as resolve_mock, patch.object(
            self.replica_module,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.get_messages(
                '/api/v1/mailboxes/messages?mainemail=owner@example.com&email=target@example.com&format=json'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Mailbox-Node'], 'replica-node-23h')
        self.assertTrue(response.get_json()['success'])
        resolve_mock.assert_called_once_with('owner@example.com')
        search_mock.assert_called_once_with(
            self.account,
            'target@example.com',
            1,
        )

    def test_public_token_html_replica_not_ready_returns_html_security_headers(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        self.set_replica_state(last_success_at=None)

        with patch.object(self.replica_module, 'get_account_by_id') as account_mock, patch.object(
            self.replica_module,
            'find_public_mailbox_messages',
        ) as search_mock:
            response = self.client.get(f"/api/v2/mailboxes/{link['token']}")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        self.assert_public_token_headers(response)
        account_mock.assert_not_called()
        search_mock.assert_not_called()
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 0)
        self.assertIsNone(row['last_accessed_at'])

    def test_public_token_html_replica_ready_serves_without_access_count_writes(self):
        link = self.seed_public_link('Recipient01@iCloud.com')
        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=1),
            node_id='replica-node-token',
        )
        account = {
            'id': int(link['account_id']),
            'email': 'owner@example.com',
            'account_type': 'outlook',
        }

        with patch.object(
            self.replica_module,
            'get_account_by_id',
            return_value=account,
        ) as account_mock, patch.object(
            self.replica_module,
            'find_public_mailbox_messages',
            return_value=self.success_result(),
        ) as search_mock:
            response = self.client.get(f"/api/v2/mailboxes/{link['token']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'text/html; charset=utf-8')
        self.assertEqual(response.get_data(as_text=True), '<p>mail body</p>')
        self.assertEqual(response.headers['X-Mailbox-Node'], 'replica-node-token')
        self.assert_public_token_headers(response)
        account_mock.assert_called_once_with(int(link['account_id']))
        search_mock.assert_called_once_with(
            account,
            'recipient01@icloud.com',
            1,
        )
        row = self.get_public_link_row(int(link['id']))
        self.assertEqual(int(row['primary_access_count'] or 0), 0)
        self.assertIsNone(row['last_accessed_at'])

    def test_primary_show_url_resolves_on_replica_with_independent_secret(self):
        primary_module, temp_dir, module_name = load_isolated_web_outlook_app(
            secret_key='independent-primary-secret',
        )
        try:
            with primary_module.app.app_context():
                primary_module.init_db()
                db = primary_module.get_db()
                cursor = db.execute(
                    "INSERT INTO accounts (email) VALUES (?)",
                    ('owner@example.com',),
                )
                account_id = int(cursor.lastrowid)
                recipient = primary_module.normalize_recipient_email('Recipient01@iCloud.com')
                primary_module.upsert_recipient_mail_link(
                    db,
                    account_id,
                    'Owner@Example.com',
                    recipient.display,
                    recipient.normalized,
                    None,
                )
                db.commit()
                snapshot = primary_module.build_snapshot(db, primary_module.decrypt_data)

            with primary_module.app.test_request_context(base_url='https://primary.example'):
                share_url = primary_module.build_recipient_link_public_url(
                    account_id,
                    recipient.display,
                )
            primary_shared = urlparse(share_url).path.strip('/').split('/')[1]

            self.assertEqual(CLUSTER_PROTOCOL_VERSION, 3)
            self.assertEqual(
                snapshot['accounts'][0]['recipient_share_segment'],
                primary_shared,
            )

            with self.app.app_context():
                db = self.replica_module.get_db()
                apply_snapshot(db, snapshot, self.replica_module.encrypt_data)
                db.commit()
                replica_account = db.execute(
                    "SELECT recipient_share_segment FROM accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
            self.assertEqual(replica_account['recipient_share_segment'], primary_shared)
            self.set_replica_state(
                cursor=int(snapshot['snapshot_cursor']),
                last_success_at=datetime.now(timezone.utc),
                node_id='replica-independent-secret',
            )

            with patch.object(
                self.replica_module,
                'find_public_mailbox_messages',
                return_value=self.success_result(),
            ):
                response = self.client.get(urlparse(share_url).path)

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.headers['X-Mailbox-Node'], 'replica-independent-secret')

            malformed_snapshot = copy.deepcopy(snapshot)
            malformed_snapshot['accounts'][0]['recipient_share_segment'] = 'not base64url!'
            with self.app.app_context(), self.assertRaises(ReplicaApplyError):
                apply_snapshot(
                    self.replica_module.get_db(),
                    malformed_snapshot,
                    self.replica_module.encrypt_data,
                )
        finally:
            cleanup_isolated_web_outlook_app(primary_module, temp_dir, module_name)

    def test_new_share_segment_replicates_incrementally(self):
        primary_module, temp_dir, module_name = load_isolated_web_outlook_app(
            secret_key='increment-primary-secret',
        )
        try:
            with primary_module.app.app_context():
                primary_module.init_db()
                db = primary_module.get_db()
                cursor = db.execute(
                    "INSERT INTO accounts (email) VALUES (?)",
                    ('increment-owner@example.com',),
                )
                account_id = int(cursor.lastrowid)
                db.commit()
                snapshot = primary_module.build_snapshot(db, primary_module.decrypt_data)

            with self.app.app_context():
                db = self.replica_module.get_db()
                apply_snapshot(db, snapshot, self.replica_module.encrypt_data)
                db.commit()

            with primary_module.app.app_context():
                db = primary_module.get_db()
                recipient = primary_module.normalize_recipient_email('increment@icloud.com')
                primary_module.upsert_recipient_mail_link(
                    db,
                    account_id,
                    'increment-owner@example.com',
                    recipient.display,
                    recipient.normalized,
                    None,
                )
                db.commit()
                increment = primary_module.build_increment(
                    db,
                    int(snapshot['snapshot_cursor']),
                    100,
                    primary_module.decrypt_data,
                )

            self.assertEqual(len(increment['accounts']), 1)
            expected_segment = increment['accounts'][0]['recipient_share_segment']
            self.assertRegex(expected_segment, r'^[A-Za-z0-9_-]{43}$')

            with self.app.app_context():
                db = self.replica_module.get_db()
                apply_increment(db, increment, self.replica_module.encrypt_data)
                db.commit()
                row = db.execute(
                    'SELECT recipient_share_segment FROM accounts WHERE id = ?',
                    (account_id,),
                ).fetchone()
            self.assertEqual(row['recipient_share_segment'], expected_segment)
        finally:
            cleanup_isolated_web_outlook_app(primary_module, temp_dir, module_name)

    def test_replica_schema_upgrade_forces_fresh_snapshot(self):
        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc),
        )
        with self.app.app_context():
            db = self.replica_module.get_db()
            db.execute('ALTER TABLE accounts DROP COLUMN recipient_share_segment')
            db.commit()

        self.replica_module.init_db()
        with self.app.app_context():
            db = self.replica_module.get_db()
            columns = {
                row['name']
                for row in db.execute('PRAGMA table_info(accounts)').fetchall()
            }
            state = self.replica_module.load_replica_state(db)

        self.assertIn('recipient_share_segment', columns)
        self.assertIsNone(state.last_success_at)
        self.assertEqual(state.protocol_version, CLUSTER_PROTOCOL_VERSION)

    def test_replica_health_routes_reflect_liveness_and_readiness_window(self):
        self.set_replica_state(last_success_at=None)

        live_response = self.client.get('/health/live')
        ready_response = self.client.get('/health/ready')

        self.assertEqual(live_response.status_code, 200, live_response.get_data(as_text=True))
        self.assertEqual(ready_response.status_code, 503, ready_response.get_data(as_text=True))
        self.assertEqual(ready_response.get_json()['error_code'], 'replica_not_ready')

        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=23),
            node_id='replica-node-health',
        )
        ready_response = self.client.get('/health/ready')
        self.assertEqual(ready_response.status_code, 200, ready_response.get_data(as_text=True))

        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=24, seconds=1),
            node_id='replica-node-health',
        )
        expired_response = self.client.get('/health/ready')
        self.assertEqual(expired_response.status_code, 503, expired_response.get_data(as_text=True))
        self.assertEqual(expired_response.get_json()['error_code'], 'replica_data_expired')

    def test_replica_app_starts_sync_worker_once_on_first_request(self):
        self.replica_module._replica_sync_worker_start_attempted = False
        worker_thread = __import__('threading').Thread(target=lambda: None)
        with patch.object(
            self.replica_module,
            'start_replica_sync_worker',
            return_value=worker_thread,
        ) as start_mock:
            first_response = self.client.get('/health/live')
            second_response = self.client.get('/health/live')

        self.assertEqual(first_response.status_code, 200, first_response.get_data(as_text=True))
        self.assertEqual(second_response.status_code, 200, second_response.get_data(as_text=True))
        start_mock.assert_called_once()

    def test_replica_health_ready_quarantines_value_error_replica_state_and_reinitializes_clean_state(self):
        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=23),
            node_id='replica-node-invalid-state',
        )

        with self.app.app_context():
            db = self.replica_module.get_db()
            db.execute(
                "UPDATE cluster_replica_state SET value = 'not-a-number' WHERE key = 'cursor'"
            )
            db.commit()

        database_path = pathlib.Path(self.replica_module.DATABASE)
        try:
            with self.app.app_context():
                self.replica_module.close_connection(None)

            response = self.client.get('/health/ready')

            self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
            self.assertEqual(response.get_json()['error_code'], 'replica_not_ready')

            quarantined = sorted(database_path.parent.glob(f'{database_path.name}.corrupt-*'))
            self.assertTrue(quarantined, 'expected a quarantined replica database copy')

            with self.app.app_context():
                repaired_state = self.replica_module.load_replica_state(self.replica_module.get_db())

            self.assertEqual(repaired_state.cursor, 0)
            self.assertIsNone(repaired_state.last_success_at)
        finally:
            if database_path.exists():
                database_path.unlink()
            with self.app.app_context():
                self.replica_module.init_db()

    def test_replica_health_ready_quarantines_corrupt_database_and_reinitializes_clean_state(self):
        self.set_replica_state(
            cursor=41,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=23),
            node_id='replica-node-corrupt',
        )

        database_path = pathlib.Path(self.replica_module.DATABASE)
        try:
            with self.app.app_context():
                self.replica_module.close_connection(None)

            database_path.write_bytes(b'not a sqlite database')

            response = self.client.get('/health/ready')

            self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
            self.assertEqual(response.get_json()['error_code'], 'replica_not_ready')

            quarantined = sorted(database_path.parent.glob(f'{database_path.name}.corrupt-*'))
            self.assertTrue(quarantined, 'expected a quarantined replica database copy')

            with self.app.app_context():
                repaired_state = self.replica_module.load_replica_state(self.replica_module.get_db())

            self.assertEqual(repaired_state.cursor, 0)
            self.assertIsNone(repaired_state.last_success_at)
        finally:
            if database_path.exists():
                database_path.unlink()
            with self.app.app_context():
                self.replica_module.init_db()
