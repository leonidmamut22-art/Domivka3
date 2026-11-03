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


if __name__ == '__main__':
    unittest.main()
