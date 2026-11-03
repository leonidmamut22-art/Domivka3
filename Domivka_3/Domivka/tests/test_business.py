"""Authorization, idempotency, loyalty and advertising accounting invariants."""
import asyncio
import datetime as dt
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import db
import market
import business


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dbpath=Path(__file__).resolve().parent/('business-test-'+uuid.uuid4().hex+'.db')
        await db.init(str(self.dbpath))
        await db.DB.executescript(market.SCHEMA)
        await db.DB.commit()
        await db.ex("insert into users(id,name) values(1,'User'),(2,'Referrer'),(3,'Seller'),(4,'Other'),(9,'Owner'),(10,'Admin'),(11,'Moderator'),(12,'Marketing')")
        self.module=SimpleNamespace(ADMINS={9,10})
        with patch.dict(os.environ,{'ADMIN_IDS':'9,10','OWNER_ID':'9'}):
            await business.setup(self.module)
        self.lid=business.DB.execute("insert into market_listings(seller_id,title,descr,city,category,condition,price) values(3,'Wooden desk','A useful solid wooden desk','Lutsk','furniture','used',1000)").lastrowid
        self.rid=business.DB.execute("insert into listings(owner,title,city,district,price_night,photos) values(3,'Sunny apartment','Lutsk','Center',800,'[]')").lastrowid
        business.DB.commit()

    async def asyncTearDown(self):
        business.DB.close();business.DB=None
        await db.DB.close()
        for suffix in ('','-wal','-shm'):Path(str(self.dbpath)+suffix).unlink(missing_ok=True)

    async def api(self,uid,path,data=None,method='GET'):
        return await business.dispatch(uid,path,data,method)

    async def delegate(self,uid,role):
        return await self.api(9,'/admin/delegates',{'user_id':uid,'role':role},'POST')

    async def order(self,buyer=1,seller=3,price=1000,status='completed'):
        oid=business.DB.execute("insert into market_orders(listing_id,buyer_id,seller_id,request_id,price,carrier,payment,recipient,status) values(?,?,?,?,?,'pickup','pickup','{}',?)",(self.lid,buyer,seller,uuid.uuid4().hex,price,status)).lastrowid
        business.DB.commit()
        return oid

    async def campaign(self,**changes):
        data={'title':'Local sponsored collection','body':'Clearly labeled advertisement','service':'market','image_url':'','target_url':'/market',
              'starts_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=1)).isoformat(),
              'ends_at':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat(),
              'active':True,'max_views':100,'daily_cap':3,'booked_amount':500}
        data.update(changes)
        return (await self.api(9,'/admin/campaigns',data,'POST'))['item']

    async def test_owner_immutable_and_scoped_delegates(self):
        self.assertEqual(business.role(9),'owner')
        self.module.ADMINS.add(4)
        self.assertIsNone(business.role(4))
        await self.delegate(11,'moderator')
        await self.delegate(12,'marketing')
        self.assertEqual(business.role(11),'moderator')
        with self.assertRaises(business.BusinessError):await self.api(11,'/admin/campaigns')
        with self.assertRaises(business.BusinessError):await self.api(12,'/admin/moderation')
        with self.assertRaises(business.BusinessError):await self.api(10,'/admin/delegates',{'user_id':4,'role':'marketing'},'POST')
        with self.assertRaises(business.BusinessError):await self.delegate(9,'none')
        await self.delegate(11,'none')
        self.assertIsNone(business.role(11))

    async def test_dual_service_moderation_and_audit(self):
        await self.delegate(11,'moderator')
        for service,lid in (('market',self.lid),('rentals',self.rid)):
            await self.api(11,'/admin/moderate',{'service':service,'listing_id':lid,'action':'block','reason':'Needs review'},'POST')
        self.assertEqual(business.one('select status from market_listings where id=?',(self.lid,))['status'],'blocked')
        self.assertEqual(business.one('select blocked from listings where id=?',(self.rid,))['blocked'],1)
        logs=(await self.api(9,'/admin/audit'))['items']
        self.assertEqual(len([x for x in logs if x['action']=='moderate']),2)
        with self.assertRaises(business.BusinessError):await self.api(11,'/admin/audit')

    async def test_daily_reward_idempotent_and_capped(self):
        result=await asyncio.gather(*(self.api(1,'/daily',{},'POST') for _ in range(5)))
        self.assertEqual(sum(x['awarded_today'] for x in result),5)
        p=(await self.api(1,'/profile'))['item']
        self.assertEqual(p['points'],5)
        self.assertTrue(p['daily_claimed'])
        self.assertEqual(p['streak'],1)
        self.assertFalse(p['points_are_money'])

    async def test_referral_attribution_immutable_self_and_cycle_rejected(self):
        own=(await self.api(1,'/profile'))['item']['referral_code']
        ref=(await self.api(2,'/profile'))['item']['referral_code']
        other=(await self.api(4,'/profile'))['item']['referral_code']
        with self.assertRaises(business.BusinessError):await self.api(1,'/referral',{'code':own},'POST')
        await self.api(1,'/referral',{'code':ref},'POST')
        with self.assertRaises(business.BusinessError):await self.api(1,'/referral',{'code':other},'POST')
        with self.assertRaises(business.BusinessError):await self.api(2,'/referral',{'code':own},'POST')
        self.assertEqual((await self.api(2,'/profile'))['item']['points'],0)

    async def test_completed_status_alone_never_awards_referral(self):
        code=(await self.api(2,'/profile'))['item']['referral_code']
        await self.api(1,'/referral',{'code':code},'POST')
        oid=await self.order()
        self.assertEqual((await self.api(2,'/profile'))['item']['points'],0)
        await self.api(9,'/admin/qualify-order',{'service':'market','order_id':oid,'reference':'Carrier reconciled 123','confirmed':True},'POST')
        p=(await self.api(2,'/profile'))['item']
        self.assertEqual(p['points'],50)
        self.assertEqual(p['qualified_referrals'],1)
        self.assertEqual((await self.api(2,'/profile'))['item']['points'],50)

    async def test_referrer_cannot_be_counterparty_and_pending_order_not_qualified(self):
        code=(await self.api(2,'/profile'))['item']['referral_code']
        await self.api(1,'/referral',{'code':code},'POST')
        oid=await self.order(seller=2)
        await self.api(9,'/admin/qualify-order',{'service':'market','order_id':oid,'reference':'Carrier reconciled 123','confirmed':True},'POST')
        self.assertEqual((await self.api(2,'/profile'))['item']['points'],0)
        pending=await self.order(buyer=4,status='pending')
        with self.assertRaises(business.BusinessError):await self.api(9,'/admin/qualify-order',{'service':'market','order_id':pending,'reference':'Wrong status','confirmed':True},'POST')

    async def test_referral_not_attributed_after_purchase(self):
        await self.order()
        code=(await self.api(2,'/profile'))['item']['referral_code']
        with self.assertRaises(business.BusinessError):await self.api(1,'/referral',{'code':code},'POST')

    async def test_perk_redemption_atomic_and_idempotent(self):
        business.award(1,'fixture','seed',50);business.DB.commit()
        offer=(await self.api(1,'/offers'))['items'][0]
        p={'offer_id':offer['id'],'request_id':'one-redemption'}
        first=await self.api(1,'/redeem',p,'POST')
        second=await self.api(1,'/redeem',p,'POST')
        self.assertEqual(first['item']['id'],second['item']['id'])
        self.assertEqual((await self.api(1,'/profile'))['item']['points'],10)
        with self.assertRaises(business.BusinessError):await self.api(1,'/redeem',{'offer_id':offer['id'],'request_id':'second-redemption'},'POST')

    async def test_alert_ownership_limit_and_real_price_drop(self):
        for n in range(3):await self.api(1,'/alerts',{'service':'market','city':'Lutsk','q':str(n)},'POST')
        with self.assertRaises(business.BusinessError):await self.api(1,'/alerts',{'service':'market','city':'Lutsk','q':'fourth'},'POST')
        aid=(await self.api(1,'/alerts'))['items'][0]['id']
        await self.api(4,f'/alerts/{aid}',{},'DELETE')
        self.assertEqual(len((await self.api(1,'/alerts'))['items']),3)
        business.DB.execute('insert into market_favorites values(?,?)',(1,self.lid));business.DB.commit()
        first=(await self.api(1,'/feed'))['items']
        self.assertEqual(next(x for x in first if x['service']=='market')['reason'],'favorite')
        business.DB.execute('update market_listings set price=900 where id=?',(self.lid,));business.DB.commit()
        second=(await self.api(1,'/feed'))['items']
        self.assertEqual(next(x for x in second if x['service']=='market')['reason'],'favorite_price_drop')

    async def test_advertising_schedule_caps_and_receipts_separate_from_bookings(self):
        c=await self.campaign(daily_cap=1)
        banners=(await self.api(1,'/banners',{'service':'market'}))['items']
        self.assertEqual(banners[0]['label'],'Реклама')
        self.assertNotIn('booked_amount',banners[0])
        first=await self.api(1,'/banner/event',{'id':c['id'],'event':'view'},'POST')
        again=await self.api(1,'/banner/event',{'id':c['id'],'event':'view'},'POST')
        self.assertTrue(first['counted']);self.assertFalse(again['counted'])
        self.assertEqual((await self.api(1,'/banners',{'service':'market'}))['items'],[])
        metrics=(await self.api(9,'/admin/dashboard'))['item']
        self.assertEqual(metrics['advertising']['booked_amount'],500)
        self.assertEqual(metrics['advertising']['reconciled_receipts'],0)
        receipt={'campaign_id':c['id'],'amount':200,'reference':'Bank reference 123','confirmed':True}
        await self.api(9,'/admin/receipts',receipt,'POST');await self.api(9,'/admin/receipts',receipt,'POST')
        self.assertEqual((await self.api(9,'/admin/dashboard'))['item']['advertising']['reconciled_receipts'],200)
        with self.assertRaises(business.BusinessError):await self.api(10,'/admin/receipts',receipt,'POST')

    async def test_future_ad_not_delivered_and_click_requires_view(self):
        future=await self.campaign(starts_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat())
        self.assertEqual((await self.api(1,'/banners',{'service':'market'}))['items'],[])
        with self.assertRaises(business.BusinessError):await self.api(1,'/banner/event',{'id':future['id'],'event':'click'},'POST')

    async def test_plan_is_informational_not_fake_payment_or_cash_discount(self):
        plan=(await self.api(9,'/admin/offers',{'title':'Business plan','body':'Contact owner for availability','kind':'plan','point_cost':0,'price_uah':100,'duration_days':30,'active':True},'POST'))['item']
        self.assertFalse((await self.api(1,'/offers'))['payment_enabled'])
        with self.assertRaises(business.BusinessError):await self.api(1,'/redeem',{'offer_id':plan['id'],'request_id':'plan-payment-test'},'POST')
        with self.assertRaises(business.BusinessError):await self.api(9,'/admin/offers',{'title':'Fake discount','kind':'seller_discount','point_cost':1,'price_uah':0},'POST')

    async def test_gmv_is_completed_value_not_revenue_or_bundle_child_count(self):
        await self.order(price=1200)
        await self.order(buyer=4,price=0)
        m=(await self.api(9,'/admin/dashboard'))['item']
        self.assertEqual(m['market']['completed_gmv'],1200)
        self.assertEqual(m['market']['completed_orders'],1)
        self.assertFalse(m['gmv_is_revenue'])
        self.assertFalse(m['payment_enabled'])

    async def test_banned_user_and_malformed_payload_rejected(self):
        await db.ex('update users set banned=1 where id=1')
        with self.assertRaises(business.BusinessError):await self.api(1,'/daily',{},'POST')
        with self.assertRaises(business.BusinessError):await self.api(2,'/daily',[1],'POST')

    async def test_targeted_hidden_offer_cannot_be_redeemed_by_id(self):
        business.award(1,'fixture','eligible-balance',100);business.DB.commit()
        offer=(await self.api(9,'/admin/offers',{'title':'Personal search slots','body':'Only selected user','kind':'alerts_extra','point_cost':5,'price_uah':0,'price_stars':0,'duration_days':7,'active':True,'target_user_id':4,'min_level':0,'vip_only':False},'POST'))['item']
        self.assertNotIn(offer['id'],[r['id'] for r in (await self.api(1,'/offers'))['items']])
        self.assertIn(offer['id'],[r['id'] for r in (await self.api(4,'/offers'))['items']])
        with self.assertRaises(business.BusinessError):await self.api(1,'/redeem',{'offer_id':offer['id'],'request_id':'hidden-offer-redemption'},'POST')
        self.assertEqual((await self.api(1,'/profile'))['item']['points'],100)

    async def test_offer_level_and_vip_audience_enforced_server_side(self):
        offer=(await self.api(9,'/admin/offers',{'title':'VIP loyalty perk','body':'Audience rules','kind':'profile_accent','point_cost':5,'price_uah':0,'duration_days':7,'active':True,'min_level':3,'vip_only':True},'POST'))['item']
        self.assertFalse(business.eligible_offer(1,offer))
        business.award(1,'fixture','level-three',200)
        self.assertFalse(business.eligible_offer(1,offer))
        until=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).replace(tzinfo=None).isoformat(sep=' ')
        business.DB.execute('insert into biz_perks(user_id,offer_id,kind,title,until,request_id) values(?,?,?,?,?,?)',(1,1,'vip','VIP',until,'fixture-vip'))
        business.DB.commit()
        self.assertTrue(business.eligible_offer(1,offer))
        result=await self.api(1,'/redeem',{'offer_id':offer['id'],'request_id':'vip-eligible-perk'},'POST')
        self.assertEqual(result['item']['kind'],'profile_accent')
        self.assertEqual(business.alert_cap(1),25)

    async def test_stars_offer_never_free_points_redemption_and_vip_hides_ads(self):
        offer=(await self.api(9,'/admin/offers',{'title':'Digital VIP','body':'Only digital platform features','kind':'vip','point_cost':0,'price_uah':0,'price_stars':50,'duration_days':30,'active':True},'POST'))['item']
        self.assertEqual(offer['price_stars'],50)
        self.assertTrue((await self.api(1,'/offers'))['payment_enabled'])
        with self.assertRaises(business.BusinessError):await self.api(1,'/redeem',{'offer_id':offer['id'],'request_id':'never-free-vip'},'POST')
        await self.campaign()
        until=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).replace(tzinfo=None).isoformat(sep=' ')
        business.DB.execute('insert into biz_perks(user_id,offer_id,kind,title,until,request_id) values(?,?,?,?,?,?)',(1,offer['id'],'vip','VIP',until,'verified-paid-test'))
        business.DB.commit()
        self.assertEqual((await self.api(1,'/banners',{'service':'market'}))['items'],[])



if __name__=='__main__':unittest.main()
