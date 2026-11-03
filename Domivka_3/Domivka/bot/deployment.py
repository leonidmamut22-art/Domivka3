"""Fail closed before publishing another process's URL as this bot's Mini App."""
import os
from urllib.parse import urlsplit
import httpx


def production():
    return os.environ.get('DEPLOYMENT_MODE','development').lower() == 'production'


def validate_origin(value):
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/') or p.port not in (None,443):
        raise ValueError('WEBAPP_URL має бути окремою HTTPS-адресою без шляху, параметрів або пароля.')
    return value.rstrip('/')


def check_config():
    if production():
        validate_origin(os.environ.get('WEBAPP_URL',''))
        if os.environ.get('WEBAPP','on').lower()=='off':
            raise ValueError('У production WEBAPP не може бути вимкненим.')
        if os.environ.get('TUNNEL','off').lower()!='off':
            raise ValueError('У production потрібна постійна адреса: TUNNEL=off.')


async def verify_public_app(url,bot_id,*,client=None):
    url=validate_origin(url)
    async def check(cl):
        response=await cl.get(url+'/api/identity')
        response.raise_for_status()
        data=response.json()
        if data.get('service')!='domivka' or data.get('bot_id')!=int(bot_id):
            raise ValueError('WEBAPP_URL веде до іншого бота або сервісу. Кнопку Mini App не змінено.')
        return data
    if client is not None:return await check(client)
    async with httpx.AsyncClient(timeout=12,follow_redirects=False,trust_env=False) as cl:
        return await check(cl)
