import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import db
import market
import shipping
import delivery_service


class MarketplaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dbpath=Path(__file__).resolve().parent/('market-test-'+uuid.uuid4().hex+'.db')
        await db.init(str(self.dbpath))
        await db.ex("insert into users(id,name) values(5000000001,'Seller'),(5000000002,'Buyer'),(5000000003,'Other'),(9,'Admin')")
        await market.setup(SimpleNamespace(ADMINS={9},APPROVAL=False))
        self.seller,self.buyer,self.other=5000000001,5000000002,5000000003
        self.payload={'title':'Wooden desk','descr':'A desk in good used condition.','city':'Lutsk','category':'furniture','condition':'used','price':1500,'photos':[]}
        self.lid=(await market.dispatch(self.seller,'/listings',self.payload,'POST'))['item']['id']

    async def asyncTearDown(self):
        market.DB.close();market.DB=None
        await db.DB.close()
        for suffix in ('','-wal','-shm'):
            Path(str(self.dbpath)+suffix).unlink(missing_ok=True)

    def purchase(self,key='request-one',lid=None):
        return {'listing_id':lid or self.lid,'request_id':key,'carrier':'nova','payment':'cod',
                'recipient':{'name':'Buyer Name','phone':'+380501234567','city':'Lutsk','branch':'1','city_ref':'city','warehouse_ref':'branch'}}

    async def buy(self):
        return (await market.dispatch(self.buyer,'/orders',self.purchase(),'POST'))['item']

    async def test_owner_authorization_and_large_telegram_ids(self):
        profile=(await market.dispatch(self.buyer,f'/sellers/{self.seller}'))['item']
        self.assertEqual(profile['id'],self.seller)
        with self.assertRaises(market.MarketError):
            await market.dispatch(self.buyer,f'/listings/{self.lid}',{'price':1},'PATCH')
        self.assertEqual((await market.dispatch(self.buyer,f'/listings/{self.lid}'))['item']['price'],1500)

    async def test_atomic_reservation_and_idempotency(self):
        results=await asyncio.gather(market.dispatch(self.buyer,'/orders',self.purchase(),'POST'),
                                     market.dispatch(self.other,'/orders',self.purchase('another-request'),'POST'),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        first=(await market.dispatch(self.buyer,'/orders',self.purchase(),'POST'))['item']
        again=(await market.dispatch(self.buyer,'/orders',self.purchase(),'POST'))['item']
        self.assertEqual(first['id'],again['id'])
        self.assertEqual(market.one('select count(*) n from market_orders')['n'],1)

    async def test_private_order_and_chat_access(self):
        o=await self.buy()
        with self.assertRaises(market.MarketError):await market.dispatch(self.other,f"/orders/{o['id']}")
        c=(await market.dispatch(self.buyer,'/chats',{'listing_id':self.lid},'POST'))['item']
        with self.assertRaises(market.MarketError):await market.dispatch(self.other,f"/chats/{c['id']}/messages")
        await market.dispatch(self.buyer,f"/chats/{c['id']}/messages",{'body':'Hello'},'POST')
        await market.dispatch(self.seller,'/blocks',{'user_id':self.buyer},'POST')
        with self.assertRaises(market.MarketError):await market.dispatch(self.buyer,f"/chats/{c['id']}/messages",{'body':'Again'},'POST')

    async def test_review_only_after_completed_purchase(self):
        o=await self.buy();p=f"/orders/{o['id']}"
        with self.assertRaises(market.MarketError):await market.dispatch(self.buyer,p+'/review',{'rating':5},'POST')
        with self.assertRaises(market.MarketError):await market.dispatch(self.buyer,p,{'status':'accepted'},'PATCH')
        await market.dispatch(self.seller,p,{'status':'accepted'},'PATCH')
        await market.dispatch(self.buyer,p,{'status':'completed'},'PATCH')
        await market.dispatch(self.buyer,p+'/review',{'rating':5,'text':'Great'},'POST')
        with self.assertRaises(market.MarketError):await market.dispatch(self.buyer,p+'/review',{'rating':5},'POST')
        profile=(await market.dispatch(self.buyer,f'/sellers/{self.seller}'))['item']
        self.assertEqual(profile['review_count'],1)
        self.assertEqual(profile['rating'],5)

    async def test_moderation_survives_cancel(self):
        o=await self.buy()
        await market.dispatch(9,'/moderate',{'listing_id':self.lid,'status':'blocked'},'POST')
        await market.dispatch(self.buyer,f"/orders/{o['id']}",{'status':'cancelled'},'PATCH')
        self.assertEqual(market.one('select status from market_listings where id=?',(self.lid,))['status'],'blocked')
        self.assertEqual((await market.dispatch(self.buyer,'/listings'))['items'],[])

    async def test_banned_and_arbitrary_photo_rejected(self):
        with self.assertRaises(market.MarketError):await market.dispatch(self.seller,'/listings',{**self.payload,'photos':['a'*30]},'POST')
        await market.register_upload(self.seller,'a'*30)
        item=(await market.dispatch(self.seller,'/listings',{**self.payload,'photos':['a'*30]},'POST'))['item']
        self.assertEqual(item['photos'],['a'*30])
        await db.ex('update users set banned=1 where id=?',(self.buyer,))
        with self.assertRaises(market.MarketError):await market.dispatch(self.buyer,'/orders',self.purchase(),'POST')

    def normal_sender_profile(self):
        return {'name':'Петренко Олена','phone':'380501234567',
                'city_ref':'11111111-1111-1111-1111-111111111111',
                'warehouse_ref':'22222222-2222-2222-2222-222222222222'}

    def save_sender_profile(self):
        market.DB.execute('insert into market_sender_profiles values(?,?)',
                          (self.seller,json.dumps(self.normal_sender_profile())))
        market.DB.commit()

    async def test_shipping_document_is_not_dispatch(self):
        o=await self.buy();p=f"/orders/{o['id']}"
        self.save_sender_profile()
        fake=AsyncMock(return_value={'state':'created','code':'created','message':'Накладну створено.',
                                    'ttn':'12345678901234','document_ref':'ref'})
        with patch.object(delivery_service,'auto_create',fake):
            result=await market.dispatch(self.seller,p,{'status':'accepted','parcel':{'weight':1,'length':10,'width':10,'height':10}},'PATCH')
            self.assertEqual(result['item']['status'],'accepted')
            self.assertEqual(result['item']['shipping_state'],'created')
            self.assertEqual(result['item']['ttn'],'12345678901234')
            self.assertEqual(fake.await_args.args[2],self.normal_sender_profile())
            await market.delivery(self.seller,'/delivery/waybill',{'order_id':o['id'],'parcel':{}},'POST')
            fake.assert_awaited_once()

    async def test_uncertain_shipping_blocks_retries_and_cancellation(self):
        o=await self.buy();p=f"/orders/{o['id']}"
        self.save_sender_profile()
        fake=AsyncMock(return_value={'state':'reconciliation_required','code':'shipping_uncertain','message':'Перевірте кабінет перевізника.'})
        with patch.object(delivery_service,'auto_create',fake):
            result=await market.dispatch(self.seller,p,{'status':'accepted'},'PATCH')
            self.assertEqual(result['item']['shipping_state'],'reconciliation_required')
            with self.assertRaises(market.MarketError):
                await market.delivery(self.seller,'/delivery/waybill',{'order_id':o['id'],'parcel':{}},'POST')
            fake.assert_awaited_once()
        with self.assertRaises(market.MarketError):
            await market.dispatch(self.buyer,p,{'status':'cancelled'},'PATCH')
        self.assertEqual((await market.dispatch(self.buyer,p))['item']['shipping_state'],'reconciliation_required')

    async def test_sender_settings_reject_api_credentials(self):
        payload={'api_key':'a'*32,'sender':self.normal_sender_profile()}
        with self.assertRaises(market.MarketError):
            await market.delivery(self.seller,'/delivery/settings',payload,'POST')
        self.assertIsNone(market.one('select * from market_sender_profiles where seller_id=?',(self.seller,)))

    async def test_normal_sender_settings_without_personal_api_key(self):
        payload={'sender':self.normal_sender_profile()}
        with patch.dict(os.environ,{'NOVA_POSHTA_API_KEY':''}):
            await market.delivery(self.seller,'/delivery/settings',payload,'POST')
            result=await market.delivery(self.seller,'/delivery/settings',method='GET')
        self.assertEqual(result['item']['sender'],self.normal_sender_profile())
        self.assertEqual(result['item']['mode'],'central_not_configured')
        self.assertNotIn('api_key',json.dumps(result))

    async def test_automatic_accept_without_central_configuration_stays_pending(self):
        o=await self.buy();p=f"/orders/{o['id']}"
        self.save_sender_profile()
        with patch.dict(os.environ,{'NOVA_POSHTA_API_KEY':'','NOVA_POSHTA_MARKETPLACE_ENABLED':''}):
            result=await market.dispatch(self.seller,p,{'status':'accepted'},'PATCH')
        self.assertEqual(result['item']['status'],'accepted')
        self.assertEqual(result['item']['shipping_state'],'awaiting_carrier_setup')
        self.assertEqual(result['item']['ttn'],'')


if __name__=='__main__':unittest.main()
