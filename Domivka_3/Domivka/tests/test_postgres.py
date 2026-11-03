"""Real PostgreSQL coverage. CI supplies DOMIVKA_TEST_POSTGRES_URL only.

Each test creates/drops its own random schema; no production schema is touched.
The ordinary local SQLite suite skips this class when the test URL is absent.
"""
import asyncio
import json
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import storage
import db
import market
import cart
import business
import billing

URL=os.environ.get('DOMIVKA_TEST_POSTGRES_URL','')


class TranslationTests(unittest.TestCase):
    def test_literals_are_not_placeholders_or_identifiers(self):
        result=storage.translate("select 'user ? datetime(''now'') 100%s', user from favs where user=?",True)
        self.assertIn("'user ? datetime(''now'') 100%%s'",result)
        self.assertIn('"user"=%s',result)
        self.assertEqual(len(storage.statements("select ';'; select 'x';")),2)

    def test_dates_and_bigint_identity(self):
        result=storage.translate("create table t(id integer primary key autoincrement,user integer,created text default(datetime('now')))")
        self.assertIn('bigserial primary key',result)
        self.assertIn('"user" bigint',result)
        self.assertIn("AT TIME ZONE 'UTC'",result)
        self.assertIn("INTERVAL '-1 minute'",storage.translate("select datetime('now','-1 minute')"))


@unittest.skipUnless(URL,'DOMIVKA_TEST_POSTGRES_URL not configured')
class PostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='domivka_test_'+uuid.uuid4().hex
        self.environment=patch.dict(os.environ,{'DATABASE_URL':URL,'DATABASE_SCHEMA':self.schema,'OWNER_ID':'9','ADMIN_IDS':'9'})
        self.environment.start()
        self.bot=SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://t.me/$test'),refund_star_payment=AsyncMock(return_value=True))
        self.module=SimpleNamespace(ADMINS={9},APPROVAL=False,bot=self.bot)
        await db.init('unused-local.sqlite')
        await market.setup(self.module)
        await cart.setup(self.module)
        await business.setup(self.module)
        await billing.setup(self.module)
        await db.ex("insert into users(id,name) values(5000000001,'Seller'),(5000000002,'Buyer'),(5000000003,'Other'),(9,'Owner')")
        self.seller,self.buyer,self.other=5000000001,5000000002,5000000003
        self.payload={'title':'Wooden desk','descr':'A wooden desk in good condition.','city':'Lutsk','category':'furniture','condition':'used','price':1000,'photos':[]}
        self.lid=(await market.dispatch(self.seller,'/listings',self.payload,'POST'))['item']['id']

    async def asyncTearDown(self):
        try:
            for module in (billing,business,cart,market):
                if module.DB is not None:module.DB.close();module.DB=None
            await db.DB.close()
            import psycopg
            from psycopg import sql
            with psycopg.connect(URL,autocommit=True) as conn:
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:self.environment.stop()

    def purchase(self,key='purchase-request'):
        return {'listing_id':self.lid,'request_id':key,'carrier':'pickup','payment':'pickup','recipient':{'name':'Buyer Person','phone':'+380501234567','city':'Lutsk','branch':''}}

    async def test_modules_share_persistent_pg_and_large_identifiers(self):
        self.assertTrue(db.is_postgres())
        self.assertEqual((await db.one('select id from users where id=?',(self.seller,)))['id'],self.seller)
        detail=(await market.dispatch(self.buyer,f'/listings/{self.lid}'))['item']
        self.assertEqual(detail['price'],1000)
        self.assertEqual(detail['photos'],[])
        self.assertIsInstance(detail['created'],str)
        self.assertEqual(len(detail['created']),19)
        second=storage.connect(URL)
        try:
            self.assertEqual(second.execute('select title from market_listings where id=?',(self.lid,)).fetchone()['title'],'Wooden desk')
            columns=second.execute('pragma table_info(market_orders)').fetchall()
            self.assertIn('bundle_primary',[r['name'] for r in columns])
        finally:second.close()

    async def test_reserved_user_upsert_ignore_replace_and_json_aggregates(self):
        await db.ex('insert or ignore into favs(user,listing_id) values(?,?)',(self.buyer,10))
        await db.ex('insert or ignore into favs(user,listing_id) values(?,?)',(self.buyer,10))
        self.assertEqual((await db.one('select count(*) n from favs where user=?',(self.buyer,)))['n'],1)
        await db.ex("insert or replace into verif(user,doc,selfie,status) values(?,?,?,'pending')",(self.buyer,'doc','selfie'))
        await db.ex("insert or replace into verif(user,doc,selfie,status) values(?,?,?,'pending')",(self.buyer,'new','selfie'))
        self.assertEqual((await db.one('select doc from verif where user=?',(self.buyer,)))['doc'],'new')
        await db.ex("insert into subs(user,plan,until) values(?,?,?) on conflict(user) do update set until=excluded.until",(self.buyer,'pro','2099-01-01'))
        self.assertIsNotNone(await db.one("select user from subs where until>=date('now')"))
        self.assertIsInstance((await db.one('select coalesce(sum(price),0) n from market_listings'))['n'],int)
        json.dumps(await db.one('select avg(price) rating from market_listings'))
        await db.ex("insert into points_log(user,kind,ref,pts) values(?,'ref_visit','one',5)",(self.buyer,))
        self.assertEqual((await db.one("select count(*) n from points_log where kind like 'ref_%' and user=?",(self.buyer,)))['n'],1)

    async def test_market_reservation_idempotent_rollback_and_completed_review(self):
        order=(await market.dispatch(self.buyer,'/orders',self.purchase(),'POST'))['item']
        again=(await market.dispatch(self.buyer,'/orders',self.purchase(),'POST'))['item']
        self.assertEqual(order['id'],again['id'])
        with self.assertRaises(market.MarketError):await market.dispatch(self.other,'/orders',self.purchase('competing-request'),'POST')
        await market.dispatch(self.seller,f"/orders/{order['id']}",{'status':'accepted'},'PATCH')
        await market.dispatch(self.buyer,f"/orders/{order['id']}",{'status':'completed'},'PATCH')
        await market.dispatch(self.buyer,f"/orders/{order['id']}/review",{'rating':5,'text':'Great'},'POST')
        profile=(await market.dispatch(self.buyer,f'/sellers/{self.seller}'))['item']
        self.assertEqual(profile['rating'],5)
        json.dumps(profile)

    async def test_same_seller_cart_checkout_is_atomic_and_never_double_reserves(self):
        second=(await market.dispatch(self.seller,'/listings',{**self.payload,'title':'Second desk','price':700},'POST'))['item']['id']
        for lid in (self.lid,second):await cart.dispatch(self.buyer,'/items',{'listing_id':lid},'POST')
        payload={'seller_id':self.seller,'request_id':'bundle-checkout-one','expected_total':1700,'carrier':'pickup','payment':'pickup','recipient':{'name':'Buyer Person','phone':'+380501234567','city':'Lutsk','branch':''}}
        first=(await cart.dispatch(self.buyer,'/checkout',payload,'POST'))['item']
        self.assertEqual(first['price'],1700)
        again=(await cart.dispatch(self.buyer,'/checkout',payload,'POST'))['item']
        self.assertEqual(first['id'],again['id'])
        self.assertEqual((await db.one("select count(*) n from market_listings where status='reserved'"))['n'],2)
        self.assertEqual((await db.one('select sum(price) n from market_orders'))['n'],1700)

    async def test_daily_points_atomic_perks_and_audit(self):
        results=await asyncio.gather(*(business.dispatch(self.buyer,'/daily',{},'POST') for _ in range(4)))
        self.assertEqual(sum(r['awarded_today'] for r in results),5)
        self.assertEqual((await business.dispatch(self.buyer,'/profile'))['item']['points'],5)
        await business.dispatch(9,'/admin/delegates',{'user_id':self.other,'role':'moderator'},'POST')
        await business.dispatch(self.other,'/admin/moderate',{'service':'market','listing_id':self.lid,'action':'block','reason':'Check listing'},'POST')
        logs=(await business.dispatch(9,'/admin/audit'))['items']
        self.assertTrue(any(r['action']=='moderate' for r in logs))
        self.assertTrue(business.exists('market_orders'))
        json.dumps((await business.dispatch(9,'/admin/dashboard'))['item'])

    async def test_billing_receipt_and_entitlement_transaction_replay(self):
        offer=(await business.dispatch(9,'/admin/offers',{'title':'Digital VIP','body':'Platform digital features','kind':'vip','price_stars':100,'point_cost':0,'price_uah':0,'duration_days':7,'active':True},'POST'))['item']
        invoice=await billing.invoice(self.buyer,offer['id'])
        query=SimpleNamespace(invoice_payload=billing.PREFIX+invoice['invoice_id'],from_user=SimpleNamespace(id=self.buyer),total_amount=100,currency='XTR',id='precheck-pg',answer=AsyncMock())
        await billing.pre_checkout(query)
        self.assertTrue(query.answer.await_args.kwargs['ok'])
        paid=SimpleNamespace(from_user=SimpleNamespace(id=self.buyer),answer=AsyncMock(),successful_payment=SimpleNamespace(invoice_payload=billing.PREFIX+invoice['invoice_id'],total_amount=100,currency='XTR',telegram_payment_charge_id='pg-charge-unique'))
        await billing.successful(paid);await billing.successful(paid)
        self.assertEqual((await db.one('select count(*) n from biz_star_receipts'))['n'],1)
        self.assertEqual((await db.one('select count(*) n from biz_perks'))['n'],1)
        metrics=(await business.dispatch(9,'/admin/dashboard'))['item']
        self.assertEqual(metrics['stars']['settled'],100)

    async def test_rental_booking_overlap_prevented_and_utc_functions(self):
        lid=await db.ex("insert into listings(owner,title,city,district,price_night) values(?,'Flat','Lutsk','Center',800)",(self.seller,))
        first=await db.book(lid,self.buyer,'2099-01-01','2099-01-04',1,2400)
        second=await db.book(lid,self.other,'2099-01-03','2099-01-05',1,1600)
        self.assertIsNotNone(first);self.assertIsNone(second)
        self.assertIsNotNone(await db.book(lid,self.other,'2099-01-04','2099-01-05',1,800))
        self.assertIsInstance((await db.one("select datetime('now','-30 day') d,date('now') today"))['d'],str)

    async def test_failed_statement_savepoint_preserves_outer_transaction(self):
        conn=market.DB
        conn.execute('begin immediate')
        conn.execute('insert into market_favorites values(?,?)',(self.buyer,self.lid))
        with self.assertRaises(sqlite3.IntegrityError):conn.execute('insert into market_favorites values(?,?)',(self.buyer,self.lid))
        conn.execute('insert or ignore into market_blocks values(?,?)',(self.buyer,self.other))
        conn.commit()
        self.assertEqual(conn.execute('select count(*) n from market_favorites').fetchone()['n'],1)
        self.assertEqual(conn.execute('select count(*) n from market_blocks').fetchone()['n'],1)


if __name__=='__main__':unittest.main()
