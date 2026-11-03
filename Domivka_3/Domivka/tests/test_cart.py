"""Transactional seller bundles, authorization and shipping regression tests."""
import asyncio
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import db
import market
import cart
import shipping


class CartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path(__file__).resolve().parent / ('cart-test-' + uuid.uuid4().hex + '.db')
        await db.init(str(self.path))
        await db.ex("insert into users(id,name) values(1,'Seller'),(2,'Buyer'),(3,'Other'),(4,'Second Seller')")
        self.mod = SimpleNamespace(ADMINS=set(), APPROVAL=False)
        await market.setup(self.mod)
        await cart.setup(self.mod)
        self.payload = {'title': 'Wooden table', 'descr': 'A wooden table in good condition.', 'city': 'Lutsk',
                        'category': 'furniture', 'condition': 'used', 'price': 1500, 'photos': []}
        self.lids = []
        for title in ('First table', 'Second table'):
            self.lids.append((await market.dispatch(1, '/listings', {**self.payload, 'title': title}, 'POST'))['item']['id'])
        self.recipient = {'name': 'Buyer Name', 'phone': '+380501234567', 'city': 'Lutsk', 'branch': '1',
                          'city_ref': 'city', 'warehouse_ref': 'branch'}

    async def asyncTearDown(self):
        cart.DB.close()
        cart.DB = None
        market.DB.close()
        market.DB = None
        await db.DB.close()
        for suffix in ('', '-wal', '-shm'):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    async def add_all(self, user=2):
        for lid in self.lids:
            await cart.dispatch(user, '/items', {'listing_id': lid}, 'POST')

    def checkout_data(self, **extra):
        return {'seller_id': 1, 'request_id': 'checkout-request', 'expected_total': 3000,
                'recipient': self.recipient, 'carrier': 'nova', 'payment': 'cod', **extra}

    async def checkout(self):
        await self.add_all()
        return await cart.dispatch(2, '/checkout', self.checkout_data(), 'POST')

    async def test_grouped_cart_single_quantity_and_seller_rules(self):
        await self.add_all()
        other = (await market.dispatch(4, '/listings', self.payload, 'POST'))['item']['id']
        await cart.dispatch(2, '/items', {'listing_id': other}, 'POST')
        result = await cart.dispatch(2)
        self.assertEqual(result['count'], 3)
        self.assertEqual(len(result['groups']), 2)
        self.assertEqual(result['groups'][0]['subtotal'], 3000)
        # Repeated add is idempotent, never quantity 2 for a unique used item.
        await cart.dispatch(2, '/items', {'listing_id': self.lids[0]}, 'POST')
        self.assertEqual((await cart.dispatch(2))['count'], 3)
        with self.assertRaises(market.MarketError):
            await cart.dispatch(2, '/items', {'listing_id': self.lids[0], 'quantity': 2}, 'POST')
        with self.assertRaises(market.MarketError):
            await cart.dispatch(1, '/items', {'listing_id': self.lids[0]}, 'POST')

    async def test_atomic_checkout_preserves_snapshot_and_total_once(self):
        result = await self.checkout()
        order = result['item']
        self.assertEqual(order['price'], 3000)
        self.assertEqual(sum(i['price'] for i in order['items']), 3000)
        self.assertEqual(len(order['items']), 2)
        rows = market.q('select price,status,bundle_primary from market_orders order by id')
        self.assertEqual([r['price'] for r in rows], [3000, 0])
        self.assertEqual([r['bundle_primary'] for r in rows], [1, 0])
        self.assertTrue(all(r['status'] == 'pending' for r in rows))
        self.assertEqual((await cart.dispatch(2))['count'], 0)
        self.assertTrue(all(r['status'] == 'reserved' for r in market.q('select status from market_listings')))

    async def test_idempotency_survives_cart_clear_and_rejects_payload_change(self):
        first = await self.checkout()
        again = await cart.dispatch(2, '/checkout', self.checkout_data(), 'POST')
        self.assertEqual(first['item']['id'], again['item']['id'])
        self.assertEqual(market.one('select count(*) n from market_orders')['n'], 2)
        with self.assertRaises(market.MarketError):
            await cart.dispatch(2, '/checkout', self.checkout_data(carrier='ukr'), 'POST')

    async def test_unavailable_member_rolls_back_entire_bundle(self):
        await self.add_all()
        await market.dispatch(3, '/orders', {'listing_id': self.lids[1], 'request_id': 'direct-purchase',
                              'recipient': self.recipient, 'carrier': 'nova', 'payment': 'cod'}, 'POST')
        with self.assertRaises(market.MarketError):
            await cart.dispatch(2, '/checkout', self.checkout_data(), 'POST')
        self.assertEqual(market.one('select status from market_listings where id=?', (self.lids[0],))['status'], 'active')
        self.assertEqual(cart._one(cart.DB, 'select count(*) n from cart_groups')['n'], 0)
        self.assertEqual(market.one('select count(*) n from market_orders')['n'], 1)
        self.assertEqual((await cart.dispatch(2))['count'], 2)

    async def test_live_price_requires_buyer_reconfirmation(self):
        await self.add_all()
        await market.dispatch(1, f'/listings/{self.lids[1]}', {'price': 2000}, 'PATCH')
        with self.assertRaises(market.MarketError):
            await cart.dispatch(2, '/checkout', self.checkout_data(), 'POST')
        self.assertEqual(market.one('select count(*) n from market_orders')['n'], 0)
        self.assertEqual((await cart.dispatch(2))['groups'][0]['subtotal'], 3500)

    async def test_participant_privacy_and_unified_transitions(self):
        result = await self.checkout()
        primary, gid = result['item']['id'], result['group']['id']
        with self.assertRaises(market.MarketError):
            await cart.dispatch(3, f'/groups/{gid}')
        with self.assertRaises(market.MarketError):
            await market.dispatch(2, f'/orders/{primary}', {'status': 'accepted'}, 'PATCH')
        await market.dispatch(1, f'/orders/{primary}', {'status': 'accepted'}, 'PATCH')
        self.assertTrue(all(r['status'] == 'accepted' for r in market.q('select status from market_orders')))
        visible = (await market.dispatch(2, '/orders'))['items']
        self.assertEqual(len(visible), 1)
        self.assertEqual(len(visible[0]['items']), 2)
        child = cart.bundle_scope(market.DB, primary)['order_ids'][1]
        with self.assertRaises(market.MarketError):
            await market.dispatch(1, f'/orders/{child}', {'status': 'shipped', 'ttn': '12345678901234'}, 'PATCH')
        await market.dispatch(2, f'/orders/{primary}', {'status': 'completed'}, 'PATCH')
        self.assertTrue(all(r['status'] == 'sold' for r in market.q('select status from market_listings')))

    async def test_bundle_cancel_preserves_moderation(self):
        result = await self.checkout()
        market.DB.execute("update market_listings set status='blocked' where id=?", (self.lids[1],))
        market.DB.commit()
        await market.dispatch(2, f"/orders/{result['item']['id']}", {'status': 'cancelled'}, 'PATCH')
        rows = market.q('select status from market_listings order by id')
        self.assertEqual([r['status'] for r in rows], ['active', 'blocked'])

    async def test_one_shipping_document_uses_total_for_all_members(self):
        result = await self.checkout()
        oid = result['item']['id']
        await market.dispatch(1, f'/orders/{oid}', {'status': 'accepted'}, 'PATCH')
        market.DB.execute('insert into market_delivery values(?,?)', (1, 'encrypted'))
        market.DB.commit()
        fake = SimpleNamespace(create_waybill=AsyncMock(return_value={'ttn': '12345678901234', 'document_ref': 'ref'}))
        with patch.object(shipping, 'decrypt_credentials', return_value={'api_key': 'key', 'sender': {}}), patch.object(shipping, 'NovaPoshta', return_value=fake):
            await market.delivery(1, '/delivery/waybill', {'order_id': oid, 'parcel': {'weight': 1, 'length': 10, 'width': 10, 'height': 10}}, 'POST')
            await market.delivery(1, '/delivery/waybill', {'order_id': oid, 'parcel': {}}, 'POST')
        fake.create_waybill.assert_awaited_once()
        self.assertEqual(fake.create_waybill.await_args.args[2]['price'], 3000)
        self.assertTrue(all(r['ttn'] == '12345678901234' and r['shipping_state'] == 'created'
                            for r in market.q('select ttn,shipping_state from market_orders')))


if __name__ == '__main__':
    unittest.main()
