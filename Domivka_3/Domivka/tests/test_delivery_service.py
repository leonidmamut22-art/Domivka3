import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import delivery_service as delivery
import shipping

REF = '11111111-1111-1111-1111-111111111111'
REF2 = '22222222-2222-2222-2222-222222222222'
PROFILE = dict(name='Петренко Олена', phone='+380501234567', city_ref=REF, warehouse_ref=REF2)
ORDER = dict(id=7, seller_id=123, carrier='nova', payment='cod', status='accepted', shipping_state='', ttn='',
             price=500, title='Лампа', recipient=dict(name='Іваненко Іван', phone='380991234567', city_ref=REF, warehouse_ref=REF2),
             parcel=dict(weight=1, length=30, width=20, height=10))


def config():
    return {'NOVA_POSHTA_API_KEY': 'a'*32, 'NOVA_POSHTA_MARKETPLACE_ENABLED': '1',
            'NOVA_POSHTA_SELLER_MAPPINGS': json.dumps({'123': dict(approved=True, cod_recipient_is_sender=True,
                  phone='380501234567', sender_ref=REF, contact_ref=REF2)})}


class Carrier:
    def __init__(self, contact_phone='380501234567', uncertain=False):
        self.created = []
        self.contact_phone = contact_phone
        self.uncertain = uncertain

    async def senders(self):
        return [{'Ref': REF}]

    async def contacts(self, sender):
        return [{'Ref': REF2, 'Phones': self.contact_phone}]

    async def warehouses(self, city, query):
        return [dict(ref=REF2, city_ref=REF, status='Working', category='Branch')]

    async def create_waybill(self, sender, recipient, parcel, order_id):
        self.created.append((sender, recipient, parcel, order_id))
        if self.uncertain:
            raise shipping.ShippingError('shipping_uncertain', 'Перевірте кабінет.', uncertain=True)
        return dict(ttn='20451234567890', document_ref=REF, payment='cod')


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_global_key_keeps_pending_without_carrier_calls(self):
        carrier = Carrier()
        with patch.dict(os.environ, {'NOVA_POSHTA_API_KEY': '', 'NOVA_POSHTA_MARKETPLACE_ENABLED': '1'}):
            result = await delivery.auto_create(123, ORDER, PROFILE, client=carrier)
        self.assertEqual(result['state'], 'awaiting_carrier_setup')
        self.assertEqual(carrier.created, [])

    async def test_approval_flag_is_not_sufficient_for_arbitrary_seller(self):
        carrier = Carrier()
        with patch.dict(os.environ, dict(config(), NOVA_POSHTA_SELLER_MAPPINGS='{}')):
            result = await delivery.auto_create(123, ORDER, PROFILE, client=carrier)
        self.assertEqual(result['state'], 'awaiting_sender_mapping')
        self.assertEqual(carrier.created, [])

    async def test_central_account_never_uses_mismatching_sender_contact(self):
        carrier = Carrier(contact_phone='380991111111')
        with patch.dict(os.environ, config()):
            result = await delivery.auto_create(123, ORDER, PROFILE, client=carrier)
        self.assertEqual(result['code'], 'sender_contact_mismatch')
        self.assertEqual(carrier.created, [])

    async def test_approved_registered_seller_creates_actual_sender_document(self):
        carrier = Carrier()
        with patch.dict(os.environ, config()):
            result = await delivery.auto_create(123, ORDER, PROFILE, client=carrier)
        self.assertEqual(result['state'], 'created')
        self.assertEqual(result['ttn'], '20451234567890')
        self.assertEqual(carrier.created[0][0]['sender_ref'], REF)
        self.assertEqual(carrier.created[0][0]['phone'], '380501234567')
        self.assertEqual(carrier.created[0][2]['price'], ORDER['price'])

    async def test_uncertain_result_blocks_automatic_duplicate(self):
        carrier = Carrier(uncertain=True)
        with patch.dict(os.environ, config()):
            first = await delivery.auto_create(123, ORDER, PROFILE, client=carrier)
            second = await delivery.auto_create(123, dict(ORDER, shipping_state=first['state']), PROFILE, client=carrier)
        self.assertEqual(first['state'], 'reconciliation_required')
        self.assertEqual(second['state'], 'reconciliation_required')
        self.assertEqual(len(carrier.created), 1)

    async def test_child_bundle_never_creates_second_label(self):
        carrier = Carrier()
        with patch.dict(os.environ, config()):
            result = await delivery.auto_create(123, dict(ORDER,bundle_id='group1',bundle_primary=0), PROFILE,client=carrier)
        self.assertEqual(result['state'], 'awaiting_primary_order')
        self.assertEqual(carrier.created, [])

    async def test_buyer_cannot_create_as_seller(self):
        with self.assertRaises(shipping.ShippingError):
            await delivery.auto_create(999, ORDER, PROFILE, client=Carrier())

    async def test_pending_order_cannot_create_before_seller_acceptance(self):
        carrier = Carrier()
        with patch.dict(os.environ, config()):
            result = await delivery.auto_create(123, dict(ORDER,status='pending'), PROFILE,client=carrier)
        self.assertEqual(result['state'], 'awaiting_order_acceptance')
        self.assertEqual(carrier.created, [])

    async def test_existing_document_reused_not_created(self):
        carrier = Carrier()
        result = await delivery.auto_create(123, dict(ORDER,ttn='20451234567890'), PROFILE,client=carrier)
        self.assertEqual(result['ttn'], '20451234567890')
        self.assertEqual(carrier.created, [])

    async def test_profile_contains_no_api_key_or_sender_reference(self):
        result = delivery.normalize_sender_profile(PROFILE)
        self.assertEqual(set(result), {'name','phone','city_ref','warehouse_ref'})
        with self.assertRaises(shipping.ShippingError):
            delivery.normalize_sender_profile(dict(PROFILE,api_key='secret'))


if __name__ == '__main__':
    unittest.main()
