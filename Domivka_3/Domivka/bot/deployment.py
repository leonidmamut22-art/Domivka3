"""Fixed-origin deployment and awaited Telegram webhook execution.

Completed update IDs are persisted only after dispatcher processing succeeds.
Telegram delivery is at least once: a crash after business commits but before the
receipt is stored can still replay an update. Payment/order handlers keep their
own idempotency; these receipts do not claim an exactly-once transaction.
"""
import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import re
import signal
from urllib.parse import urlsplit

import httpx

WEBHOOK_PATH = '/telegram/webhook'
MAX_UPDATE_BYTES = 1024 * 1024


def render_hosted():
    return os.environ.get('RENDER', '').lower() == 'true' or bool(os.environ.get('RENDER_EXTERNAL_URL', '').strip())


def execution_mode():
    mode = os.environ.get('BOT_MODE', '').strip().lower() or ('webhook' if render_hosted() else 'polling')
    if mode not in ('polling', 'webhook'):
        raise ValueError('BOT_MODE має бути polling або webhook.')
    return mode


def production():
    return os.environ.get('DEPLOYMENT_MODE', 'development').lower() == 'production' or render_hosted()


def postgres_storage():
    return urlsplit(os.environ.get('DATABASE_URL', '').strip()).scheme in ('postgres', 'postgresql')


def validate_origin(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/') or p.port not in (None, 443):
        raise ValueError('WEBAPP_URL має бути окремою HTTPS-адресою без шляху, параметрів або пароля.')
    return value.rstrip('/')


def public_origin():
    value = os.environ.get('WEBAPP_URL', '').strip() or os.environ.get('RENDER_EXTERNAL_URL', '').strip()
    return validate_origin(value) if value else ''


def webhook_secret():
    secret = os.environ.get('WEBHOOK_SECRET', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise ValueError('WEBHOOK_SECRET має містити 1–256 латинських літер, цифр, _ або -. Задайте випадкове значення в секретах сервера.')
    return secret


def webhook_url():
    origin = public_origin()
    if not origin:
        raise ValueError('Для webhook потрібна постійна HTTPS-адреса WEBAPP_URL або RENDER_EXTERNAL_URL.')
    return origin + WEBHOOK_PATH


def server_binding(default_port):
    hosted = render_hosted() or execution_mode() == 'webhook'
    value = os.environ.get('PORT', str(default_port)) if hosted else str(default_port)
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('PORT має бути числом від 1 до 65535.')
    host = os.environ.get('WEBAPP_HOST', '').strip() or ('0.0.0.0' if hosted else '127.0.0.1')
    if render_hosted():
        host = '0.0.0.0'
    return host, int(value)


def check_config():
    mode = execution_mode()
    if production() or mode == 'webhook':
        if not public_origin():
            raise ValueError('Потрібна постійна HTTPS-адреса WEBAPP_URL або RENDER_EXTERNAL_URL.')
        if os.environ.get('WEBAPP', 'on').lower() == 'off':
            raise ValueError('У production/webhook WEBAPP не може бути вимкненим.')
        if os.environ.get('TUNNEL', 'off').lower() != 'off':
            raise ValueError('У production/webhook потрібна постійна адреса: TUNNEL=off.')
    if mode == 'webhook':
        webhook_secret()
    if render_hosted() and not postgres_storage():
        raise ValueError('Для Render Free потрібна зовнішня PostgreSQL база DATABASE_URL: локальні SQLite-файли на цьому тарифі не зберігаються після перезапуску.')


async def verify_public_app(url, bot_id, *, client=None):
    url = validate_origin(url)

    async def check(cl):
        response = await cl.get(url + '/api/identity')
        response.raise_for_status()
        data = response.json()
        if data.get('service') != 'domivka' or data.get('bot_id') != int(bot_id):
            raise ValueError('WEBAPP_URL веде до іншого бота або сервісу. Кнопку Mini App не змінено.')
        return data

    if client is not None:
        return await check(client)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as cl:
        return await check(cl)


class WebhookReceipts:
    """Persist no update bodies, user messages or secrets; only completion IDs."""
    async def setup(self):
        import db
        await db.ex('create table if not exists webhook_updates(update_id bigint primary key,completed_at text not null)')

    async def completed(self, update_id):
        import db
        return bool(await db.one('select 1 from webhook_updates where update_id=?', (update_id,)))

    async def complete(self, update_id):
        import db
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        await db.ex('insert or ignore into webhook_updates(update_id,completed_at) values(?,?)', (update_id, stamp))


def make_webhook_handler(bot, dispatcher, secret, *, receipts=None):
    """Await feed_update directly; feed_webhook_update can detach after timeout."""
    from aiohttp import web
    from aiogram.methods.base import TelegramMethod
    from aiogram.types import Update
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise ValueError('Invalid Telegram webhook secret')
    receipts = receipts if receipts is not None else WebhookReceipts()
    lock = asyncio.Lock()

    async def handle(request):
        incoming = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(incoming.encode('utf-8'), secret.encode('ascii')):
            raise web.HTTPForbidden()
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        if request.content_length is not None and request.content_length > MAX_UPDATE_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPDATE_BYTES, actual_size=request.content_length)
        raw = bytearray()
        async for chunk in request.content.iter_chunked(16384):
            raw.extend(chunk)
            if len(raw) > MAX_UPDATE_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPDATE_BYTES, actual_size=len(raw))
        try:
            payload = json.loads(raw)
            update = Update.model_validate(payload, context={'bot': bot})
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None
        async with lock:
            try:
                if await receipts.completed(update.update_id):
                    return web.json_response({'ok': True})
                result = await dispatcher.feed_update(bot, update)
                if isinstance(result, TelegramMethod):
                    await bot(result)
                await receipts.complete(update.update_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Do not log update content, tokens or upstream error messages.
                logging.warning('Telegram webhook update failed: id=%s error=%s; awaiting Telegram retry', update.update_id, type(e).__name__)
                raise web.HTTPServiceUnavailable() from None
        return web.json_response({'ok': True})

    return handle


def add_webhook(app, bot_module):
    from aiohttp import web
    if execution_mode() != 'webhook':
        return
    receipts = WebhookReceipts()

    async def setup_receipts(_app):
        await receipts.setup()

    app.on_startup.append(setup_receipts)
    app.add_routes([web.post(WEBHOOK_PATH, make_webhook_handler(bot_module.bot, bot_module.dp, webhook_secret(), receipts=receipts))])


async def wait_for_shutdown():
    """Gracefully drain awaited HTTP updates when Render sends SIGTERM."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    registered = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
            registered.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stopped.wait()
    finally:
        for signum in registered:
            loop.remove_signal_handler(signum)
