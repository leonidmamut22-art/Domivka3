import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import shipping

REF = "11111111-1111-1111-1111-111111111111"
REF2 = "22222222-2222-2222-2222-222222222222"
KEY = "a" * 32


class EncryptionTests(unittest.TestCase):
    def test_documented_token_urlsafe_key(self):
        import secrets
        with patch.dict(os.environ, {"SHIPPING_MASTER_KEY": secrets.token_urlsafe(32)}):
            encrypted = shipping.encrypt_credentials(123, {"api_key": KEY})
            self.assertEqual(shipping.decrypt_credentials(123, encrypted), {"api_key": KEY})

    def test_roundtrip_and_account_binding(self):
        with patch.dict(os.environ, {"SHIPPING_MASTER_KEY": base64.urlsafe_b64encode(b"s" * 32).decode()}):
            encrypted = shipping.encrypt_credentials(123, {"api_key": KEY})
            self.assertNotIn(KEY, encrypted)
            self.assertEqual(shipping.decrypt_credentials(123, encrypted), {"api_key": KEY})
            with self.assertRaises(shipping.ShippingError):
                shipping.decrypt_credentials(456, encrypted)
            tampered = encrypted[:-8] + "AAAAAAAA"
            with self.assertRaises(shipping.ShippingError):
                shipping.decrypt_credentials(123, tampered)

    def test_missing_key_fails_closed(self):
        with patch.dict(os.environ, {"SHIPPING_MASTER_KEY": ""}):
            with self.assertRaises(shipping.ShippingError):
                shipping.encrypt_credentials(123, {"api_key": KEY})


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        sender = dict(city_ref=REF, sender_ref=REF, warehouse_ref=REF, contact_ref=REF, phone="+380991234567")
        recipient = dict(city_ref=REF2, warehouse_ref=REF2, name="Петренко Олена Іванівна", phone="0501234567")
        parcel = dict(weight=2, length=30, width=20, height=10, price=1234, title="Настільна лампа")
        return sender, recipient, parcel

    async def test_real_wire_format_cod_and_no_invented_number(self):
        calls = []
        def handler(request):
            data = json.loads(request.content)
            calls.append(data)
            self.assertEqual(str(request.url), shipping.ENDPOINT)
            if data["modelName"] == "Counterparty":
                return httpx.Response(200, json={"success": True, "data": [{"Ref": REF2,
                    "ContactPerson": {"data": [{"Ref": REF2}]}}]})
            return httpx.Response(200, json={"success": True, "data": [{"IntDocNumber": "20451234567890", "Ref": REF,
                  "CostOnSite": 99, "EstimatedDeliveryDate": "05.11.2026"}]})
        result = await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).create_waybill(*self.fixture(), 7)
        self.assertEqual(result["ttn"], "20451234567890")
        props = calls[-1]["methodProperties"]
        self.assertEqual(props["RecipientsPhone"], "380501234567")
        self.assertEqual(props["InfoRegClientBarcodes"], "DOMIVKA-7")
        self.assertEqual(props["BackwardDeliveryData"], [{"PayerType": "Recipient", "CargoType": "Money", "RedeliveryString": "1234"}])
        self.assertEqual(props["OptionsSeat"][0]["volumetricWidth"], "20.0")
        self.assertEqual(props["RecipientAddress"], REF2)

    async def test_save_timeout_is_uncertain_and_never_retried(self):
        calls = []
        def handler(request):
            data = json.loads(request.content)
            calls.append(data)
            if data["modelName"] == "Counterparty":
                return httpx.Response(200, json={"success": True, "data": [{"Ref": REF2,
                    "ContactPerson": {"data": [{"Ref": REF2}]}}]})
            raise httpx.ReadTimeout("private API key " + KEY, request=request)
        with self.assertRaises(shipping.ShippingError) as caught:
            await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).create_waybill(*self.fixture(), 7)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(len(calls), 2)

    async def test_malformed_success_cannot_fabricate_ttn(self):
        def handler(request):
            data = json.loads(request.content)
            if data["modelName"] == "Counterparty":
                return httpx.Response(200, json={"success": True, "data": [{"Ref": REF2,
                    "ContactPerson": {"data": [{"Ref": REF2}]}}]})
            return httpx.Response(200, json={"success": True, "data": [{"IntDocNumber": "TEST-123", "Ref": REF}]})
        with self.assertRaises(shipping.ShippingError) as caught:
            await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).create_waybill(*self.fixture(), 7)
        self.assertTrue(caught.exception.uncertain)

    async def test_validation_happens_before_any_network_mutation(self):
        called = []
        def handler(request):
            called.append(request)
            return httpx.Response(500)
        sender, recipient, parcel = self.fixture()
        parcel["weight"] = float("nan")
        with self.assertRaises(shipping.ShippingError):
            await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).create_waybill(sender, recipient, parcel, 7)
        self.assertEqual(called, [])

    async def test_sanitized_provider_error(self):
        def handler(request):
            return httpx.Response(200, json={"success": False, "errors": [KEY, "private recipient name"]})
        with self.assertRaises(shipping.ShippingError) as caught:
            await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).cities("Луцьк")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    async def test_tracking_does_not_infer_payment_or_completion(self):
        def handler(request):
            return httpx.Response(200, json={"success": True, "data": [{"Status": "Відправлення отримано", "StatusCode": "9"}]})
        result = await shipping.NovaPoshta(KEY, transport=httpx.MockTransport(handler)).track("20451234567890")
        self.assertEqual(result["status_code"], "9")
        self.assertNotIn("paid", result)
        self.assertNotIn("completed", result)


if __name__ == "__main__":
    unittest.main()
