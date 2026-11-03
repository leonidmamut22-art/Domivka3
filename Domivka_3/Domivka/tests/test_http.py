"""Exercise the actual aiohttp route/auth integration without Telegram or carrier IO."""
import hashlib
import hmac
import json
import logging
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import db
import market
import webapp

TOKEN='123456:integration-test-token'
SELLER=5000000001
BUYER=5000000002


def signed(uid):
    d={'auth_date':str(int(time.time())),'query_id':'test-query','user':json.dumps({'id':uid,'first_name':'Test'},separators=(',',':'))}
    check='\n'.join(f'{k}={v}' for k,v in sorted(d.items()))
    key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    d['hash']=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
    return {'X-Init':urlencode(d)}


class HttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dbpath=Path(__file__).resolve().parent/('http-test-'+uuid.uuid4().hex+'.db')
        await db.init(str(self.dbpath))
        await db.ex("insert into users(id,name) values(?,?),(?,?)",(SELLER,'Seller',BUYER,'Buyer'))
        self.mod=SimpleNamespace(bot=SimpleNamespace(token=TOKEN),ADMINS={9},APPROVAL=False,logging=logging)
        await market.setup(self.mod)
        webapp.HITS.clear()
        self.client=TestClient(TestServer(webapp.make_app(self.mod)))
        await self.client.start_server()
        self.payload={'title':'A nice desk','descr':'A useful desk in good condition.','city':'Lutsk','category':'furniture','condition':'used','price':1000,'photos':[]}
        self.lid=(await market.dispatch(SELLER,'/listings',self.payload,'POST'))['item']['id']

    async def asyncTearDown(self):
        await self.client.close()
        market.DB.close();market.DB=None
        await db.DB.close()
        for suffix in ('','-wal','-shm'):Path(str(self.dbpath)+suffix).unlink(missing_ok=True)

    async def test_unauthenticated_and_tampered_api_rejected(self):
        response=await self.client.get('/api/market/listings')
        self.assertEqual(response.status,401)
        self.assertIn('error',await response.json())
        headers=signed(BUYER)
        headers['X-Init']=headers['X-Init'].replace('test-query','wrong-query')
        response=await self.client.get('/api/market/listings',headers=headers)
        self.assertEqual(response.status,401)

    async def test_signed_buyer_can_read_listings(self):
        response=await self.client.get('/api/market/listings',headers=signed(BUYER))
        self.assertEqual(response.status,200)
        data=await response.json()
        self.assertEqual(data['items'][0]['id'],self.lid)

    async def test_public_photo_cannot_read_pending_listing(self):
        market.DB.execute("update market_listings set status='pending' where id=?",(self.lid,));market.DB.commit()
        response=await self.client.get(f'/api/market/photo/{self.lid}/0')
        self.assertEqual(response.status,404)
        self.assertIn('error',await response.json())

    async def test_patch_and_xss_are_plain_json_data(self):
        title='<img src=x onerror=alert(1)>'
        response=await self.client.patch(f'/api/market/listings/{self.lid}',headers=signed(SELLER),json={'title':title})
        self.assertEqual(response.status,200)
        self.assertEqual(response.content_type,'application/json')
        self.assertEqual((await response.json())['item']['title'],title)
        denied=await self.client.patch(f'/api/market/listings/{self.lid}',headers=signed(BUYER),json={'price':1})
        self.assertEqual(denied.status,403)

    async def test_assets_allowlist_and_traversal_denied(self):
        response=await self.client.get('/assets/market.js')
        self.assertEqual(response.status,200)
        for path in ('/assets/market.py','/assets/%2e%2e%2fbot%2fmarket.py','/assets/../bot/market.py','/assets/.env'):
            response=await self.client.get(path)
            self.assertEqual(response.status,404,path)

    async def test_malformed_body_has_json_error(self):
        response=await self.client.post('/api/market/listings',headers={**signed(SELLER),'Content-Type':'application/json'},data='{bad')
        self.assertEqual(response.status,400)
        self.assertIn('error',await response.json())
        response=await self.client.post('/api/market/listings',headers=signed(SELLER),json=[1])
        self.assertEqual(response.status,400)
        self.assertIn('error',await response.json())


if __name__=='__main__':unittest.main()
