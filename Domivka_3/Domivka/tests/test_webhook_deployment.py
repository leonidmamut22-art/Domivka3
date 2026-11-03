"""Webhook transport regressions: authentication, completion receipts and awaited ACK."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import deployment


class Receipts:
    def __init__(self):
        self.ids = set()
        self.started = None
        self.release = None

    async def completed(self, update_id):
        return update_id in self.ids

    async def complete(self, update_id):
        if self.started:
            self.started.set()
        if self.release:
            await self.release.wait()
        self.ids.add(update_id)


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = AsyncMock()
        self.dispatcher = SimpleNamespace(feed_update=AsyncMock(return_value=None))
        self.receipts = Receipts()
        app = web.Application(client_max_size=deployment.MAX_UPDATE_BYTES * 2)
        app.router.add_post(deployment.WEBHOOK_PATH, deployment.make_webhook_handler(
            self.bot, self.dispatcher, 'test-secret', receipts=self.receipts))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def headers(self):
        return {'X-Telegram-Bot-Api-Secret-Token': 'test-secret'}

    async def post(self, update_id=42):
        return await self.client.post(deployment.WEBHOOK_PATH, json={'update_id': update_id}, headers=self.headers())

    async def test_missing_or_wrong_secret_is_rejected_before_dispatch(self):
        for value in ('', 'wrong-secret'):
            response = await self.client.post(deployment.WEBHOOK_PATH, json={'update_id': 42}, headers={'X-Telegram-Bot-Api-Secret-Token': value})
            self.assertEqual(response.status, 403)
        self.dispatcher.feed_update.assert_not_awaited()
        self.assertEqual(self.receipts.ids, set())

    async def test_malformed_update_is_not_acknowledged(self):
        response = await self.client.post(deployment.WEBHOOK_PATH, json={'message': {}}, headers=self.headers())
        self.assertEqual(response.status, 400)
        self.dispatcher.feed_update.assert_not_awaited()

    async def test_oversize_update_is_rejected(self):
        response = await self.client.post(deployment.WEBHOOK_PATH, data=b' ' * (deployment.MAX_UPDATE_BYTES + 1), headers={**self.headers(), 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 413)
        self.dispatcher.feed_update.assert_not_awaited()

    async def test_response_waits_for_dispatch_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(*_args):
            entered.set()
            await release.wait()

        self.dispatcher.feed_update.side_effect = process
        request = asyncio.create_task(self.post())
        await asyncio.wait_for(entered.wait(), 2)
        self.assertFalse(request.done())
        self.assertEqual(self.receipts.ids, set())
        release.set()
        response = await asyncio.wait_for(request, 2)
        self.assertEqual(response.status, 200)
        self.assertIn(42, self.receipts.ids)

    async def test_response_waits_for_persisted_completion_receipt(self):
        self.receipts.started = asyncio.Event()
        self.receipts.release = asyncio.Event()
        request = asyncio.create_task(self.post())
        await asyncio.wait_for(self.receipts.started.wait(), 2)
        self.assertFalse(request.done())
        self.assertNotIn(42, self.receipts.ids)
        self.receipts.release.set()
        response = await asyncio.wait_for(request, 2)
        self.assertEqual(response.status, 200)
        self.assertIn(42, self.receipts.ids)

    async def test_completed_delivery_is_not_dispatched_twice(self):
        first = await self.post()
        second = await self.post()
        self.assertEqual((first.status, second.status), (200, 200))
        self.dispatcher.feed_update.assert_awaited_once()

    async def test_failed_update_returns_retry_and_is_not_marked_complete(self):
        self.dispatcher.feed_update.side_effect = RuntimeError('do not acknowledge')
        failed = await self.post()
        self.assertEqual(failed.status, 503)
        self.assertNotIn(42, self.receipts.ids)
        self.dispatcher.feed_update.side_effect = None
        retry = await self.post()
        self.assertEqual(retry.status, 200)
        self.assertEqual(self.dispatcher.feed_update.await_count, 2)

    async def test_returned_telegram_method_is_awaited_before_receipt(self):
        from aiogram.methods import SendMessage
        result = SendMessage(chat_id=7, text='reply')
        self.dispatcher.feed_update.return_value = result
        response = await self.post()
        self.assertEqual(response.status, 200)
        self.bot.assert_awaited_once_with(result)
        self.assertIn(42, self.receipts.ids)


class RenderConfigurationTests(unittest.TestCase):
    def test_render_uses_stable_origin_webhook_and_exact_public_port(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'RENDER_EXTERNAL_URL': 'https://domivka.onrender.com', 'DATABASE_URL': 'postgresql://db.example/domivka', 'WEBHOOK_SECRET': 'generated-secret', 'PORT': '10000'}, clear=True):
            deployment.check_config()
            self.assertEqual(deployment.execution_mode(), 'webhook')
            self.assertEqual(deployment.public_origin(), 'https://domivka.onrender.com')
            self.assertEqual(deployment.webhook_url(), 'https://domivka.onrender.com/telegram/webhook')
            self.assertEqual(deployment.server_binding(8080), ('0.0.0.0', 10000))

    def test_local_default_keeps_polling_and_loopback_binding(self):
        with patch.dict(os.environ, {'PORT': '10000'}, clear=True):
            deployment.check_config()
            self.assertEqual(deployment.execution_mode(), 'polling')
            self.assertEqual(deployment.server_binding(8080), ('127.0.0.1', 8080))

    def test_render_refuses_ephemeral_sqlite_storage(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'RENDER_EXTERNAL_URL': 'https://domivka.onrender.com', 'WEBHOOK_SECRET': 'generated-secret'}, clear=True):
            with self.assertRaises(ValueError):
                deployment.check_config()

    def test_webhook_requires_secret_and_fixed_origin(self):
        with patch.dict(os.environ, {'BOT_MODE': 'webhook', 'WEBAPP_URL': 'https://domivka.example'}, clear=True):
            with self.assertRaises(ValueError):
                deployment.check_config()
        with patch.dict(os.environ, {'BOT_MODE': 'webhook', 'WEBHOOK_SECRET': 'generated-secret'}, clear=True):
            with self.assertRaises(ValueError):
                deployment.check_config()

    def test_render_never_uses_a_conflicting_loopback_override(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'WEBAPP_HOST': '127.0.0.1', 'PORT': '12000'}, clear=True):
            self.assertEqual(deployment.server_binding(8080), ('0.0.0.0', 12000))


if __name__ == '__main__':
    unittest.main()
