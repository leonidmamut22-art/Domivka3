"""Loopback-only disposable UI fixture. Never used by bot startup."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bot'))
import bot as B
import db
import feat
import market
import cart
import business
import billing
import webapp

TOKEN='123456:disposable-browser-test-token'
UID=5000000002

async def main():
    database=ROOT/'tests'/('.browser-'+uuid.uuid4().hex+'.sqlite')
    await db.init(str(database))
    for uid,name,role in [(UID,'Тестовий Покупець','guest'),(5000000001,'Тестовий Продавець','host'),(9,'Тестовий Адміністратор','guest')]:
        await db.ex('insert into users(id,name,role,phone) values(?,?,?,?)',(uid,name,role,'+380501234567'))
    class DummyBot:
        token=TOKEN
        async def send_message(self,*a,**k):return SimpleNamespace(message_id=1)
    B.bot=DummyBot();B.BOT_USERNAME='domivka_browser_test';B.PAYMODE='off';B.APPROVAL=False;B.ADMINS={9}
    feat.setup(B)
    await market.setup(B);await cart.setup(B);await business.setup(B);await billing.setup(B)
    for title,cat,price in [('Стіл для тесту','furniture',3200),('Телефон для тесту','electronics',5000)]:
        await market.dispatch(5000000001,'/listings',{'title':title,'descr':'Демонстраційний товар тільки для автоматичного тестування. Не продається.','city':'Луцьк','category':cat,'condition':'used','price':price,'photos':[]},'POST')
    await db.ex('insert into listings(owner,title,city,district,price_night,price_month,descr) values(?,?,?,?,?,?,?)',(5000000001,'Квартира для тесту','Луцьк','Центр',1200,14000,'Демонстраційний запис'))
    def signed():
        d={'auth_date':str(int(time.time())),'user':json.dumps({'id':UID,'first_name':'Тестовий','last_name':'Покупець'})}
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        d['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(d.items())).encode(),hashlib.sha256).hexdigest()
        return urlencode(d)
    @web.middleware
    async def fixture(request,handler):
        if request.path=='/':
            mock='''<script>window.Telegram={WebApp:{initData:INIT,initDataUnsafe:{user:{id:5000000002}},colorScheme:'light',themeParams:{},ready(){},expand(){},disableVerticalSwipes(){},onEvent(){},setHeaderColor(){},setBackgroundColor(){},BackButton:{show(){},hide(){},onClick(){}},HapticFeedback:{impactOccurred(){},notificationOccurred(){}},openTelegramLink(){},openLink(){},openInvoice(){}}};</script>'''.replace('INIT',json.dumps(signed()))
            src=webapp.INDEX.read_text(encoding='utf-8').replace('__BOT__','domivka_browser_test')
            src=re.sub(r'<script[^>]+src=["\'][^"\']*telegram[^"\']*["\'][^>]*></script>',mock,src)
            return web.Response(text=src,content_type='text/html')
        return await handler(request)
    app=webapp.make_app(B);app.middlewares.insert(0,fixture)
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    await web.TCPSite(runner,'127.0.0.1',8765).start()
    print('Browser fixture ready',flush=True)
    try:await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        for conn in (market.DB,cart.DB,business.DB,billing.DB):
            if conn:conn.close()
        await db.DB.close()
        for suffix in ('','-wal','-shm'):Path(str(database)+suffix).unlink(missing_ok=True)

asyncio.run(main())
