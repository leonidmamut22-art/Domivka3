import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import market_bot as mb


class BotFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mb.STATE.clear()
        mb.LANG.clear()
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        self.app = SimpleNamespace(bot=self.bot, tell=AsyncMock(), home=AsyncMock())
        mb.setup(self.app)

    async def test_rental_commands_escape_market_form(self):
        mb.put(7, flow='sell', step='title', data={})
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(type='private'), text='/menu')
        self.assertFalse(await mb.on_message(message))
        self.assertNotIn(7, mb.STATE)

    async def test_photo_ownership_registered(self):
        mb.put(7, flow='sell', step='photos', data={'photos': []})
        message = SimpleNamespace(text=None, photo=[SimpleNamespace(file_id='telegram-photo')])
        with patch.object(mb.market, 'register_upload', AsyncMock()) as register:
            await mb.sell_step(7, message, mb.state(7))
        register.assert_awaited_once_with(7, 'telegram-photo')
        self.assertEqual(mb.state(7)['data']['photos'], ['telegram-photo'])

    async def test_order_confirmation_uses_shared_contract(self):
        mb.put(7, flow='order', step=4, data={'listing_id': 3, 'delivery': 'nova_poshta', 'idempotency_key': 'unique-request-key',
               'recipient_name': 'Buyer Person', 'recipient_phone': '+380501234567', 'city': 'Луцьк', 'branch': '№1',
               'city_ref': 'city-id', 'warehouse_ref': 'branch-id'})
        c = SimpleNamespace(data='mk:placeorder', answer=AsyncMock(), from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'call', AsyncMock(return_value={'item': {'id': 9}})) as call, patch.object(mb, 'order_card', AsyncMock()):
            self.assertTrue(await mb.on_callback(c))
        args = call.await_args.args
        self.assertEqual(args[:2], (7, '/orders'))
        self.assertEqual(args[2]['carrier'], 'nova')
        self.assertEqual(args[2]['request_id'], 'unique-request-key')
        self.assertEqual(args[2]['recipient']['warehouse_ref'], 'branch-id')
        self.assertNotIn(7, mb.STATE)

    async def test_directory_selection_preserves_official_refs(self):
        mb.put(7, flow='order', step=2, data={}, choices=[{'name': 'Луцьк', 'ref': 'official-city'}])
        c = SimpleNamespace(data='mk:directory:0', answer=AsyncMock(), from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})):
            await mb.on_callback(c)
        self.assertEqual(mb.state(7)['data']['city_ref'], 'official-city')
        self.assertEqual(mb.state(7)['step'], 3)

    async def test_unrelated_callback_not_consumed(self):
        self.assertFalse(await mb.on_callback(SimpleNamespace(data='m:s')))

    async def test_non_admin_moderation_denied(self):
        self.app.ADMINS = {1}
        with patch.object(mb, 'call', AsyncMock()) as call:
            await mb.moderation(7)
        call.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()

    async def test_non_admin_moderation_callback_denied(self):
        self.app.ADMINS = {1}
        c = SimpleNamespace(data='mk:mod:3:active', answer=AsyncMock(), from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'call', AsyncMock()) as call:
            self.assertTrue(await mb.on_callback(c))
        call.assert_not_awaited()

    async def test_admin_moderation_uses_shared_authorized_service(self):
        self.app.ADMINS = {7}
        c = SimpleNamespace(data='mk:mod:3:active', answer=AsyncMock(), from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'call', AsyncMock(return_value={'ok': True})) as call:
            self.assertTrue(await mb.on_callback(c))
        call.assert_awaited_once_with(7, '/moderate', {'listing_id': 3, 'status': 'active'}, 'POST')


    async def test_plain_start_opens_dual_service_welcome(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(type='private'), text='/start')
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'role', AsyncMock(return_value=None)):
            self.assertTrue(await mb.on_message(message))
        calls = self.bot.send_message.await_args_list
        self.assertIn('Domivka 4', calls[0].args[1])
        keyboard = calls[0].kwargs['reply_markup']
        labels = [x.text for row in keyboard.keyboard for x in row]
        self.assertIn('🏡 Нерухомість', labels)
        self.assertIn('🛍 Маркет', labels)
        self.assertTrue(keyboard.is_persistent)
        self.assertNotIn('🛡 Адмін', labels)

    async def test_legacy_rental_deep_link_is_not_swallowed(self):
        for argument in ('l_12', 'r_12', 'w_3', 'a_4', 'c_5', 'app'):
            message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(type='private'), text='/start ' + argument)
            self.assertFalse(await mb.on_message(message))
        self.bot.send_message.assert_not_awaited()

    async def test_reply_keyboard_market_opens_market(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(type='private'), text='🛍 Маркет')
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'home', AsyncMock()) as home:
            self.assertTrue(await mb.on_message(message))
        home.assert_awaited_once_with(7, '')

    async def test_market_listing_deep_link_uses_shared_listing_auth(self):
        with patch.object(mb, 'call', AsyncMock(return_value={'item': {'id': 25}})) as call, patch.object(mb, 'listing', AsyncMock()) as listing:
            await mb.start_screen(7, 'market_25')
        call.assert_awaited_once_with(7, '/listings/25')
        listing.assert_awaited_once_with(7, {'id': 25}, True)

    async def test_marketing_cannot_delegate_admin_role(self):
        with patch.object(mb, 'role', AsyncMock(return_value='marketing')), patch.object(mb, 'business_call', AsyncMock()) as business:
            await mb.begin_business_form(7, 'delegate')
        business.assert_not_awaited()
        self.assertNotIn(7, mb.STATE)

    async def test_delegate_save_rechecks_owner_permission(self):
        mb.put(7, flow='business_form', kind='delegate', step=2, data={'user_id': 8, 'role': 'moderator'})
        with patch.object(mb, 'role', AsyncMock(return_value='marketing')), patch.object(mb, 'business_call', AsyncMock()) as business:
            self.assertTrue(await mb.business_callback(7, 'formsave', ['formsave']))
        business.assert_not_awaited()
        self.assertNotIn(7, mb.STATE)

    async def test_admin_keyboard_only_for_authorized_role(self):
        with patch.object(mb, 'role', AsyncMock(return_value='moderator')):
            await mb.start_screen(7)
        labels = [x.text for row in self.bot.send_message.await_args_list[0].kwargs['reply_markup'].keyboard for x in row]
        self.assertIn('🛡 Адмін', labels)

    async def test_cart_checkout_sends_expected_total_and_one_seller(self):
        mb.put(7, flow='order', step=4, data={'listing_id': 3, 'checkout_seller_id': 8, 'expected_total': 1200,
               'delivery': 'nova_poshta', 'idempotency_key': 'cart-request-key', 'recipient_name': 'Buyer Person',
               'recipient_phone': '+380501234567', 'city': 'Луцьк', 'branch': '№1'})
        c = SimpleNamespace(data='mk:placeorder', answer=AsyncMock(), from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        with patch.object(mb, 'user', AsyncMock(return_value={'id': 7})), patch.object(mb, 'cart_call', AsyncMock(return_value={'item': {'id': 9}})) as checkout, patch.object(mb, 'order_card', AsyncMock()):
            self.assertTrue(await mb.on_callback(c))
        payload = checkout.await_args.args[2]
        self.assertEqual(payload['seller_id'], 8)
        self.assertEqual(payload['expected_total'], 1200)
        self.assertNotIn('listing_id', payload)



    async def test_market_search_keeps_market_service_selected(self):
        self.app.S = {7: {'flow': 'new'}}
        with patch.object(mb, 'call', AsyncMock(return_value={'items': []})):
            await mb.home(7, 'стіл')
        self.assertEqual(mb.state(7)['flow'], 'browse')
        self.assertEqual(mb.state(7)['query'], 'стіл')
        self.assertNotIn(7, self.app.S)



    async def test_stars_button_opens_invoice_without_granting_perk(self):
        billing = SimpleNamespace(invoice=AsyncMock(return_value={'invoice_id': 'invoice-uuid', 'url': 'https://t.me/$trusted', 'amount': 50, 'duration_days': 30}), BillingError=RuntimeError)
        with patch.dict(sys.modules, {'billing': billing}), patch.object(mb, 'business_call', AsyncMock()) as service:
            self.assertTrue(await mb.business_callback(7, 'buyperk', ['buyperk', '3']))
        billing.invoice.assert_awaited_once_with(7, 3)
        service.assert_not_awaited()
        message = self.bot.send_message.await_args
        self.assertIn('Без автоматичного продовження', message.args[1])
        self.assertEqual(message.kwargs['reply_markup'].inline_keyboard[0][0].url, 'https://t.me/$trusted')

    async def test_non_owner_cannot_start_stars_refund(self):
        with patch.object(mb, 'role', AsyncMock(return_value='admin')):
            await mb.begin_business_form(7, 'refund')
        self.assertNotIn(7, mb.STATE)


if __name__ == '__main__':
    unittest.main()
