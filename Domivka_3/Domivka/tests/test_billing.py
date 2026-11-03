import datetime as dt
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import db
import business
import billing


class BillingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path=Path(__file__).resolve().parent/('billing-'+uuid.uuid4().hex+'.db')
        await db.init(str(self.path))
        await db.ex("insert into users(id,name) values(101,'Buyer'),(202,'Other'),(9,'Owner')")
        self.bot=SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://t.me/$testinvoice'),refund_star_payment=AsyncMock(return_value=True))
        self.module=SimpleNamespace(ADMINS={9},bot=self.bot)
        with patch.dict(os.environ,{'OWNER_ID':'9','ADMIN_IDS':'9'}):
            await business.setup(self.module)
        await billing.setup(self.module)
        self.offer=business.DB.execute("insert into biz_offers(title,body,kind,price_stars,duration_days,point_cost,price_uah) values('VIP','No ads and extra searches','vip',100,7,0,0)").lastrowid
        business.DB.commit()

    async def asyncTearDown(self):
        billing.DB.close();billing.DB=None
        business.DB.close();business.DB=None
        await db.DB.close()
        for suffix in ('','-wal','-shm'):Path(str(self.path)+suffix).unlink(missing_ok=True)

    def query(self,invoice,uid=101,amount=100,currency='XTR',qid='precheck-1'):
        return SimpleNamespace(invoice_payload=billing.PREFIX+invoice['invoice_id'],from_user=SimpleNamespace(id=uid),
                               total_amount=amount,currency=currency,id=qid,answer=AsyncMock())

    def paid(self,invoice,uid=101,amount=100,currency='XTR',charge='telegram-charge-1'):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid),answer=AsyncMock(),successful_payment=SimpleNamespace(
             invoice_payload=billing.PREFIX+invoice['invoice_id'],total_amount=amount,currency=currency,telegram_payment_charge_id=charge))

    async def settled(self):
        result=await billing.invoice(101,self.offer)
        await billing.pre_checkout(self.query(result))
        await billing.successful(self.paid(result))
        return result

    async def test_invoice_uses_server_stars_snapshot_no_provider_or_physical_checkout(self):
        invoice=await billing.invoice(101,self.offer)
        kwargs=self.bot.create_invoice_link.await_args.kwargs
        self.assertEqual(kwargs['currency'],'XTR')
        self.assertEqual(len(kwargs['prices']),1)
        self.assertEqual(kwargs['prices'][0].amount,100)
        self.assertNotIn('provider_token',kwargs)
        self.assertNotIn('subscription_period',kwargs)
        self.assertTrue(invoice['one_time'])
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],0)
        business.DB.execute("update biz_offers set kind='plan' where id=?",(self.offer,));business.DB.commit()
        with self.assertRaises(billing.BillingError):await billing.invoice(101,self.offer)

    async def test_wrong_user_currency_and_amount_rejected_before_checkout(self):
        result=await billing.invoice(101,self.offer)
        for values in ({'uid':202},{'currency':'UAH'},{'amount':1}):
            query=self.query(result,**values)
            self.assertTrue(await billing.pre_checkout(query))
            self.assertFalse(query.answer.await_args.kwargs['ok'])
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],0)

    async def test_snapshot_survives_offer_price_and_duration_edit(self):
        result=await billing.invoice(101,self.offer)
        business.DB.execute('update biz_offers set price_stars=999,duration_days=365 where id=?',(self.offer,));business.DB.commit()
        query=self.query(result)
        await billing.pre_checkout(query)
        self.assertTrue(query.answer.await_args.kwargs['ok'])
        await billing.successful(self.paid(result))
        perk=dict(billing.DB.execute('select * from biz_perks').fetchone())
        remaining=dt.datetime.fromisoformat(perk['until'])-billing._now()
        self.assertGreater(remaining.total_seconds(),6.9*86400)
        self.assertLess(remaining.total_seconds(),7.1*86400)

    async def test_disabled_offer_rejects_precheckout_but_paid_approved_snapshot_delivers(self):
        first=await billing.invoice(101,self.offer)
        business.DB.execute('update biz_offers set active=0 where id=?',(self.offer,));business.DB.commit()
        query=self.query(first)
        await billing.pre_checkout(query)
        self.assertFalse(query.answer.await_args.kwargs['ok'])
        business.DB.execute('update biz_offers set active=1 where id=?',(self.offer,));business.DB.commit()
        await billing.pre_checkout(self.query(first))
        business.DB.execute('update biz_offers set active=0 where id=?',(self.offer,));business.DB.commit()
        await billing.successful(self.paid(first))
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],1)

    async def test_success_is_idempotent_charge_receipt_and_perk_atomic(self):
        result=await self.settled()
        await billing.successful(self.paid(result))
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],1)
        receipt=dict(billing.DB.execute('select * from biz_star_receipts').fetchone())
        self.assertEqual(receipt['status'],'settled')
        self.assertEqual(receipt['currency'],'XTR')
        self.assertEqual(receipt['amount'],100)
        self.assertEqual(billing.DB.execute('select count(*) from biz_receipts').fetchone()[0],0)

    async def test_success_before_precheckout_is_recorded_for_review_not_granted(self):
        result=await billing.invoice(101,self.offer)
        await billing.successful(self.paid(result))
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],0)
        self.assertEqual(billing.DB.execute('select status from biz_star_receipts').fetchone()[0],'reconciliation_required')

    async def test_different_charge_cannot_fulfill_same_invoice_twice(self):
        result=await self.settled()
        await billing.successful(self.paid(result,charge='telegram-charge-2'))
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],1)
        self.assertEqual(billing.DB.execute("select count(*) from biz_star_receipts where status='reconciliation_required'").fetchone()[0],1)

    async def test_different_precheckout_cannot_race_same_digital_kind(self):
        first=await billing.invoice(101,self.offer)
        second=await billing.invoice(101,self.offer)
        await billing.pre_checkout(self.query(first,qid='first'))
        query=self.query(second,qid='second')
        await billing.pre_checkout(query)
        self.assertFalse(query.answer.await_args.kwargs['ok'])

    async def test_refund_owner_only_then_revoke_after_confirmed_telegram_success(self):
        result=await self.settled()
        with self.assertRaises(business.BusinessError):await billing.refund(101,result['invoice_id'])
        refunded=await billing.refund(9,result['invoice_id'])
        self.assertEqual(refunded['status'],'refunded')
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],0)
        self.assertEqual(billing.DB.execute('select status from biz_star_receipts').fetchone()[0],'refunded')
        await billing.refund(9,result['invoice_id'])
        self.bot.refund_star_payment.assert_awaited_once()

    async def test_uncertain_refund_keeps_perk_and_blocks_blind_retries(self):
        result=await self.settled()
        self.bot.refund_star_payment.side_effect=TimeoutError()
        with self.assertRaises(billing.BillingError):await billing.refund(9,result['invoice_id'])
        with self.assertRaises(billing.BillingError):await billing.refund(9,result['invoice_id'])
        self.assertEqual(billing.DB.execute('select count(*) from biz_perks').fetchone()[0],1)
        self.bot.refund_star_payment.assert_awaited_once()

    async def test_unrelated_legacy_payments_pass_through(self):
        query=SimpleNamespace(invoice_payload='legacy-payload')
        message=SimpleNamespace(successful_payment=SimpleNamespace(invoice_payload='legacy-payload'))
        self.assertFalse(await billing.pre_checkout(query))
        self.assertFalse(await billing.successful(message))

    async def test_support_text_does_not_render_untrusted_html(self):
        with patch.dict(os.environ,{'PAYMENT_SUPPORT_URL':'https://t.me/support?x=<bad>'}):
            self.assertNotIn('<bad>',billing.support_text())


if __name__=='__main__':unittest.main()
