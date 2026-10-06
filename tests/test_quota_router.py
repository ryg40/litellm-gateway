import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
import quota_router as router


def quota_payload(used=2, reset=2000, secondary=None):
    return {"rate_limit": {"allowed": used < 100, "limit_reached": used >= 100,
                           "primary_window": {"used_percent": used, "reset_at": reset,
                                              "limit_window_seconds": 18000},
                           "secondary_window": secondary}}


def write_token(directory, name):
    path = Path(directory) / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "auth.json").write_text(json.dumps({"access_token": "secret-token", "account_id": "secret-account"}))
    return path / "auth.json"


class OrderTests(unittest.TestCase):
    def test_default_order(self):
        self.assertEqual(router.account_order(None), ["codex2", "codex3", "codex1"])

    def test_custom_order(self):
        self.assertEqual(router.account_order(" codex1 ,codex2"), ["codex1", "codex2"])

    def test_rejects_bad_orders(self):
        for value in ("codex2", "codex2,codex2", "codex2,other", "codex0,codex1", ""):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                router.account_order(value)


class WindowTests(unittest.TestCase):
    def test_available(self):
        self.assertEqual(router.exhausted_until(quota_payload(), 1000), 0)

    def test_five_hour_exhaustion(self):
        self.assertEqual(router.exhausted_until(quota_payload(100), 1000), 2000)

    def test_weekly_exhaustion(self):
        payload = quota_payload(2, secondary={"used_percent": 100, "reset_at": 9000})
        self.assertEqual(router.exhausted_until(payload, 1000), 9000)

    def test_both_exhausted(self):
        payload = quota_payload(100, secondary={"used_percent": 100, "reset_at": 9000})
        self.assertEqual(router.exhausted_until(payload, 1000), 9000)

    def test_reset_after(self):
        payload = {"rate_limit": {"primary_window": {"used_percent": 100, "reset_after_seconds": 500}}}
        self.assertEqual(router.exhausted_until(payload, 1000), 1500)

    def test_missing_data(self):
        with self.assertRaises(ValueError):
            router.exhausted_until({}, 1000)


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_returns_to_account_and_cache(self):
        now = [1000]
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=quota_payload(100 if now[0] < 2000 else 0))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_token(tmp, "codex2")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                quota = router.Quota(client, "codex2", path, clock=lambda: now[0])
                self.assertFalse(await quota.usable())
                now[0] = 1030
                self.assertFalse(await quota.usable())
                self.assertEqual(len(calls), 1)
                # While blocked, re-validate once per CACHE_SECONDS.
                now[0] = 1500
                self.assertFalse(await quota.usable())
                self.assertEqual(len(calls), 2)
                now[0] = 2001
                self.assertTrue(await quota.usable())
                self.assertTrue(await quota.usable())
                self.assertEqual(len(calls), 3)

    async def test_missing_token_skips_account(self):
        # No login file: the account cannot serve. It is skipped, not blocked.
        async with httpx.AsyncClient() as client:
            quota = router.Quota(client, "codex3", "/nonexistent/auth.json", clock=lambda: 1000)
            with self.assertLogs("quota_router", "INFO") as logs:
                self.assertFalse(await quota.usable())
            self.assertIn("codex3 skipped source=no-login", logs.output[0])
            self.assertFalse(quota.logged_in())
            self.assertEqual((quota.blocked_until, quota.block_reason), (0, None))

    async def test_placeholder_token_skips_account(self):
        # A worker that waits for a device code writes only device_code_requested_at.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({"device_code_requested_at": 1000.0}))
            async with httpx.AsyncClient() as client:
                quota = router.Quota(client, "codex3", path, clock=lambda: 1000)
                self.assertFalse(quota.logged_in())
                self.assertFalse(await quota.usable())
            path.write_text("not json")
            async with httpx.AsyncClient() as client:
                self.assertFalse(router.Quota(client, "codex3", path).logged_in())

    async def test_usage_failure_keeps_account_usable(self):
        # A login exists but the usage check fails: the worker gets the request.
        def handler(request):
            return httpx.Response(500)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_token(tmp, "codex2")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                quota = router.Quota(client, "codex2", path, clock=lambda: 1000)
                self.assertTrue(await quota.usable())

    async def test_retry_after_without_usage_is_bounded(self):
        # Unknown usage: a 429 blocks for at most CACHE_SECONDS.
        with tempfile.TemporaryDirectory() as tmp:
            path = write_token(tmp, "codex2")
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
                quota = router.Quota(client, "codex2", path, clock=lambda: 1000)
                await quota.rate_limited(429, {'retry-after': '300'}, b'{}')
                self.assertEqual(quota.blocked_until, 1000 + router.CACHE_SECONDS)
                self.assertEqual(quota.block_reason, '429')
                self.assertFalse(await quota.usable())


class BlockTests(unittest.IsolatedAsyncioTestCase):
    """Block and unblock decisions, re-validation and 429 bounds."""

    async def run_quota(self, test):
        self.now = [1000]
        self.calls = []
        self.payload = [quota_payload(0)]
        def handler(request):
            self.calls.append(request)
            if isinstance(self.payload[0], int):
                return httpx.Response(self.payload[0])
            return httpx.Response(200, json=self.payload[0])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_token(tmp, "codex2")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await test(router.Quota(client, "codex2", path, clock=lambda: self.now[0]))

    async def test_usage_block_is_logged_without_secrets(self):
        async def test(quota):
            self.payload[0] = quota_payload(2, secondary={'used_percent': 100, 'reset_at': 9000})
            with self.assertLogs('quota_router', 'INFO') as logs:
                self.assertFalse(await quota.usable())
            text = '\n'.join(logs.output)
            self.assertIn('codex2 block source=usage', text)
            self.assertIn('blocked_until=1970-01-01T02:30:00+00:00', text)
            self.assertIn('secondary_window.used_percent=100', text)
            self.assertNotIn('secret', text)
            self.assertEqual((quota.block_reason, quota.blocked_since), ('usage', 1000))
        await self.run_quota(test)

    async def test_revalidation_clears_block(self):
        async def test(quota):
            self.payload[0] = quota_payload(2, secondary={'used_percent': 100, 'reset_at': 9000})
            self.assertFalse(await quota.usable())
            self.payload[0] = quota_payload(0)
            self.now[0] = 1000 + router.CACHE_SECONDS - 1
            self.assertFalse(await quota.usable())
            self.assertEqual(len(self.calls), 1)
            self.now[0] = 1000 + router.CACHE_SECONDS
            with self.assertLogs('quota_router', 'INFO') as logs:
                self.assertTrue(await quota.usable())
            self.assertIn('codex2 unblock source=usage', logs.output[0])
            self.assertEqual(len(self.calls), 2)
            self.assertEqual((quota.blocked_until, quota.block_reason, quota.blocked_since), (0, None, None))
        await self.run_quota(test)

    async def test_revalidation_failure_keeps_block(self):
        async def test(quota):
            self.payload[0] = quota_payload(100, reset=9000)
            self.assertFalse(await quota.usable())
            for status in (500, 401):
                self.payload[0] = status
                self.now[0] += router.CACHE_SECONDS
                with self.assertLogs('quota_router', 'INFO') as logs:
                    self.assertFalse(await quota.usable())
                self.assertIn('block kept source=usage-check-failed', logs.output[0])
                self.assertEqual(quota.blocked_until, 9000)
            self.assertEqual(len(self.calls), 3)
        await self.run_quota(test)

    async def test_revalidation_still_exhausted_keeps_block(self):
        async def test(quota):
            self.payload[0] = quota_payload(100, reset=9000)
            self.assertFalse(await quota.usable())
            self.now[0] += router.CACHE_SECONDS
            self.assertFalse(await quota.usable())
            self.assertEqual((quota.blocked_until, quota.block_reason, quota.blocked_since), (9000, 'usage', 1000))
        await self.run_quota(test)

    async def test_429_on_healthy_account_is_short(self):
        async def test(quota):
            body = json.dumps({'error': {'resets_at': 1000 + 48 * 3600}}).encode()
            with self.assertLogs('quota_router', 'INFO') as logs:
                await quota.rate_limited(429, {'retry-after': '172800'}, body)
            self.assertEqual(quota.blocked_until, 1000 + router.CACHE_SECONDS)
            self.assertIn('source=429', logs.output[0])
            self.assertIn('status=429', logs.output[0])
            self.assertIn('retry-after=172800', logs.output[0])
            self.assertIn('resets_at=1970-01-03T00:16:40+00:00', logs.output[0])
            # The next check after CACHE_SECONDS finds the account healthy.
            self.now[0] += router.CACHE_SECONDS
            self.assertTrue(await quota.usable())
            self.assertIsNone(quota.block_reason)
        await self.run_quota(test)

    async def test_429_not_later_than_usage_reset(self):
        async def test(quota):
            self.payload[0] = quota_payload(2, secondary={'used_percent': 100, 'reset_at': 9000})
            await quota.rate_limited(429, {}, json.dumps({'error': {'resets_at': 99999}}).encode())
            self.assertEqual(quota.blocked_until, 9000)
            self.assertEqual(quota.block_reason, '429')
        await self.run_quota(test)

    async def test_429_retry_after_within_exhausted_window(self):
        async def test(quota):
            self.payload[0] = quota_payload(100, reset=9000)
            await quota.rate_limited(429, {'retry-after': '300'}, b'not json')
            self.assertEqual(quota.blocked_until, 1300)
        await self.run_quota(test)

    async def test_expired_block_is_logged(self):
        async def test(quota):
            await quota.rate_limited(429, {}, b'{}')
            self.now[0] += router.CACHE_SECONDS
            with self.assertLogs('quota_router', 'INFO') as logs:
                self.assertTrue(await quota.usable())
            self.assertIn('codex2 unblock source=expired was=429', logs.output[0])
        await self.run_quota(test)


class ProxyTests(unittest.TestCase):
    """Requests through the app with the default order codex2, codex3, codex1."""

    def invoke(self, responses, payload=None, blocked=(), logins=("codex2",), capture=None, order=None):
        calls = []
        def handler(request):
            calls.append(request.url.host)
            if capture is not None:
                capture.append(request.content)
            code, content = responses[len(calls)-1]
            content_type = 'text/event-stream' if content.startswith(b'data:') else 'application/json'
            return httpx.Response(code, content=content, headers={'content-type': content_type})
        with tempfile.TemporaryDirectory() as tmp:
            for name in logins:
                write_token(tmp, name)
            env = {'CODEX_MASTER_KEY': 'test-key', 'CODEX_TOKEN_DIR': tmp}
            if order is not None:
                env['CODEX_ACCOUNT_ORDER'] = order
            with patch.dict('os.environ', env):
                with TestClient(router.app) as client:
                    original = router.app.state.client
                    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    router.app.state.client = mock
                    for name, q in router.app.state.router.quotas.items():
                        q.available = True
                        q.checked = q.clock()
                        q.blocked_until = q.clock() + 60 if name in blocked else 0
                    result = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test-key'},
                                         json=payload or {'model': 'test', 'messages': []})
                    self.quotas = router.app.state.router.quotas
                    client.portal.call(mock.aclose)
                    router.app.state.client = original
        return result, calls

    def test_primary_only(self):
        result, calls = self.invoke([(200, b'{"choices": []}')])
        self.assertEqual(calls, ['codex2'])
        self.assertEqual(result.headers['x-codex-account'], 'codex2')

    def test_blocked_preferred_selects_last(self):
        result, calls = self.invoke([(200, b'{}')], blocked=('codex2',))
        self.assertEqual(calls, ['codex1'])

    def test_blocked_preferred_selects_third_when_logged_in(self):
        result, calls = self.invoke([(200, b'{}')], blocked=('codex2',), logins=('codex2', 'codex3'))
        self.assertEqual(calls, ['codex3'])
        self.assertEqual(result.headers['x-codex-account'], 'codex3')

    def test_429_falls_back_once_without_third_login(self):
        result, calls = self.invoke([(429, b'{}'), (200, b'{}')])
        self.assertEqual(calls, ['codex2', 'codex1'])
        self.assertEqual(result.headers['x-codex-account'], 'codex1')

    def test_429_moves_to_third_account(self):
        result, calls = self.invoke([(429, b'{}'), (200, b'{}')], logins=('codex2', 'codex3'))
        self.assertEqual(calls, ['codex2', 'codex3'])
        self.assertEqual(result.headers['x-codex-account'], 'codex3')
        self.assertEqual(self.quotas['codex2'].block_reason, '429')
        self.assertIsNone(self.quotas['codex3'].block_reason)

    def test_429_cascades_through_all_accounts(self):
        result, calls = self.invoke([(429, b'{}'), (429, b'{}'), (200, b'{}')], logins=('codex2', 'codex3'))
        self.assertEqual(calls, ['codex2', 'codex3', 'codex1'])
        self.assertEqual(result.headers['x-codex-account'], 'codex1')
        self.assertEqual(self.quotas['codex2'].block_reason, '429')
        self.assertEqual(self.quotas['codex3'].block_reason, '429')

    def test_all_exhausted_no_loop(self):
        result, calls = self.invoke([(429, b'{}'), (429, b'{}'), (429, b'{}')], logins=('codex2', 'codex3'))
        self.assertEqual(result.status_code, 429)
        self.assertEqual(len(calls), 3)

    def test_custom_order(self):
        result, calls = self.invoke([(429, b'{}'), (200, b'{}')], logins=('codex1',), order='codex1,codex2')
        self.assertEqual(calls, ['codex1', 'codex2'])

    def test_other_errors_do_not_spend_later_accounts(self):
        for status in (400, 401, 403, 500):
            result, calls = self.invoke([(status, b'{}')], logins=('codex2', 'codex3'))
            self.assertEqual(result.status_code, status)
            self.assertEqual(calls, ['codex2'])

    def test_streaming(self):
        result, calls = self.invoke([(200, b'data: ok\n\ndata: [DONE]\n\n')],
                                    payload={'model': 'test', 'stream': True})
        self.assertIn('data: [DONE]', result.text)
        self.assertEqual(calls, ['codex2'])

    def test_nonstream_aggregation(self):
        chunk = {'id': 'chatcmpl-test', 'object': 'chat.completion.chunk', 'created': 1000,
                 'model': 'gpt-5.6-luna', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]}
        content = ('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode()
        result, calls = self.invoke([(200, content)])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['choices'][0]['message']['content'], 'OK')

    def test_partial_nonstream_is_not_retried(self):
        result, calls = self.invoke([(200, b'data: {}\n\n')])
        self.assertEqual(result.status_code, 502)
        self.assertEqual(calls, ['codex2'])

    def test_session_id_derived_from_prefix(self):
        base = {'model': 'gpt-6-luna', 'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'first'}]}
        longer = {**base, 'messages': base['messages'] + [{'role': 'assistant', 'content': 'a'}, {'role': 'user', 'content': 'second'}]}
        other = {**base, 'messages': [{'role': 'system', 'content': 'other rules'}, {'role': 'user', 'content': 'first'}]}
        self.assertEqual(router.cache_session_id(base), router.cache_session_id(longer))
        self.assertNotEqual(router.cache_session_id(base), router.cache_session_id(other))
        self.assertTrue(router.cache_session_id(base).startswith('prefix-'))
        responses = {'model': 'gpt-6-luna', 'instructions': 'rules', 'input': [{'role': 'user', 'content': 'first'}]}
        self.assertEqual(router.cache_session_id(responses),
                         router.cache_session_id({**responses, 'input': responses['input'] + [{'role': 'user', 'content': 'second'}]}))

    def test_session_id_from_client_is_kept(self):
        self.assertEqual(router.cache_session_id({'litellm_session_id': 'pi-session'}), 'pi-session')
        self.assertEqual(router.cache_session_id({'extra_body': {'litellm_session_id': 'pi-session'}}), 'pi-session')

    def test_upstream_request_carries_session_id(self):
        sent = []
        result, calls = self.invoke([(200, b'{"choices": []}')], payload={'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]}, capture=sent)
        self.assertEqual(calls, ['codex2'])
        body = json.loads(sent[0])
        self.assertTrue(body['litellm_session_id'].startswith('prefix-'))
        self.assertTrue(body['stream'])

    def test_continuation_rejected(self):
        result, calls = self.invoke([], payload={'previous_response_id': 'resp_test'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(calls, [])

    def test_status_reports_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_token(tmp, 'codex2')
            with patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key', 'CODEX_TOKEN_DIR': tmp}), TestClient(router.app) as client:
                q = router.app.state.router.quotas['codex2']
                now = q.clock()
                q.blocked_until, q.block_reason, q.blocked_since, q.checked = now + 30, '429', now, now
                body = client.get('/routing/status', headers={'Authorization': 'Bearer test-key'}).json()
                self.assertEqual(body['order'], ['codex2', 'codex3', 'codex1'])
                self.assertEqual(body['preferred_account'], 'codex2')
                self.assertEqual(body['selected_account'], 'codex1')
                self.assertEqual(body['block_reason'], '429')
                self.assertEqual(body['blocked_since'], router.iso(now))
                self.assertEqual(body['codex2_blocked_until'], now + 30)
                self.assertEqual(body['accounts']['codex2']['block_reason'], '429')
                self.assertTrue(body['accounts']['codex2']['logged_in'])
                self.assertFalse(body['accounts']['codex3']['logged_in'])
                self.assertFalse(body['accounts']['codex1']['usage_checked'])
                q.blocked_until, q.block_reason, q.blocked_since, q.available = 0, None, None, True
                body = client.get('/routing/status', headers={'Authorization': 'Bearer test-key'}).json()
                self.assertEqual((body['selected_account'], body['block_reason'], body['blocked_since']), ('codex2', None, None))
                self.assertNotIn('secret', json.dumps(body))

    def test_authentication(self):
        with patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key'}), TestClient(router.app) as client:
            self.assertEqual(client.post('/v1/chat/completions', json={}).status_code, 401)
            self.assertEqual(client.get('/routing/status').status_code, 401)
            self.assertEqual(client.get('/health/liveliness').status_code, 200)


class TimeoutTests(unittest.TestCase):
    def test_default_deadline(self):
        with patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key'}):
            os.environ.pop('CODEX_REQUEST_TIMEOUT_SECONDS', None)
            with TestClient(router.app):
                self.assertEqual(router.app.state.client.timeout.read, 180)
                self.assertEqual(router.app.state.client.timeout.connect, 10)

    def test_reads_deadline(self):
        with patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key', 'CODEX_REQUEST_TIMEOUT_SECONDS': '555'}):
            with TestClient(router.app):
                self.assertEqual(router.app.state.client.timeout.read, 555)
                self.assertEqual(router.app.state.client.timeout.connect, 10)

    def test_rejects_bad_deadlines(self):
        for value in ['0', 'nan', 'inf', '3601', 'not-a-number']:
            with self.subTest(value=value), patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key', 'CODEX_REQUEST_TIMEOUT_SECONDS': value}):
                with self.assertRaises((ValueError, RuntimeError)):
                    with TestClient(router.app):
                        pass

    def test_rejects_bad_order(self):
        with patch.dict('os.environ', {'CODEX_MASTER_KEY': 'test-key', 'CODEX_ACCOUNT_ORDER': 'codex2'}):
            with self.assertRaises(RuntimeError):
                with TestClient(router.app):
                    pass


if __name__ == '__main__':
    unittest.main()
