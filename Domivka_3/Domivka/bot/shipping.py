"""Nova Poshta v2 adapter. Never expose API keys to browsers or log payloads.

Official entry point: https://novaposhta.ua/for-business/cooperation/integration/
API reference: https://developers.novaposhta.ua/documentation
Only branch-to-branch COD parcels are supported here. A created document is
not evidence of dispatch, delivery or payment. Callers must persist an atomic
shipping-attempt marker before create_waybill and reconcile uncertain outcomes
in the sender's Nova Poshta account instead of retrying the request blindly.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
from datetime import datetime, timezone
from uuid import UUID

import httpx

ENDPOINT = "https://api.novaposhta.ua/v2.0/json/"


class ShippingError(Exception):
    def __init__(self, code: str, message: str, uncertain: bool = False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


def _master_key():
    try:
        value = os.environ.get("SHIPPING_MASTER_KEY", "")
        raw = base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
        if len(raw) != 32:
            raise ValueError()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(raw)
    except (ValueError, ImportError, UnicodeError):
        raise ShippingError("shipping_config", "Захищене зберігання ключів доставки не налаштовано.") from None


def encrypt_credentials(seller_id: int, credentials: dict) -> str:
    nonce = os.urandom(12)
    cipher = _master_key().encrypt(nonce, json.dumps(credentials, ensure_ascii=False).encode(),
                                   f"domivka:np:{int(seller_id)}:v1".encode())
    return "v1:" + base64.urlsafe_b64encode(nonce + cipher).decode("ascii")


def decrypt_credentials(seller_id: int, value: str) -> dict:
    aes = _master_key()
    try:
        if not value.startswith("v1:"):
            raise ValueError()
        raw = base64.urlsafe_b64decode(value[3:])
        data = aes.decrypt(raw[:12], raw[12:], f"domivka:np:{int(seller_id)}:v1".encode())
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError()
        return parsed
    except Exception:
        raise ShippingError("shipping_credentials", "Не вдалося відкрити налаштування доставки. Підключіть акаунт повторно.") from None


def ref(value) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ShippingError("invalid_delivery", "Оберіть місто, відділення та контакт зі списку Нової пошти.") from None


def phone(value) -> str:
    n = re.sub(r"[\s()+-]", "", str(value or ""))
    if re.fullmatch(r"0\d{9}", n):
        n = "38" + n
    if not re.fullmatch(r"380\d{9}", n):
        raise ShippingError("invalid_phone", "Вкажіть український номер телефону у форматі +380XXXXXXXXX.")
    return n


def _number(value, minimum, maximum, field):
    try:
        num = float(value)
        if not math.isfinite(num) or not minimum <= num <= maximum:
            raise ValueError()
        return num
    except (TypeError, ValueError):
        raise ShippingError("invalid_parcel", f"Перевірте параметр посилки: {field}.") from None


class NovaPoshta:
    def __init__(self, api_key: str, *, transport=None):
        if not isinstance(api_key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{20,128}", api_key):
            raise ShippingError("invalid_api_key", "Перевірте API-ключ Нової пошти.")
        self._key, self._transport = api_key, transport

    async def _call(self, model, method, properties, *, mutation=False):
        # No retries: InternetDocument/save can succeed even if the response is lost.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=7),
                                         follow_redirects=False, transport=self._transport) as client:
                response = await client.post(ENDPOINT, json={"apiKey": self._key,
                    "modelName": model, "calledMethod": method, "methodProperties": properties})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShippingError("shipping_uncertain" if mutation else "shipping_unavailable",
                "Відповідь перевізника не отримано. Перевірте накладні в акаунті Нової пошти перед повторною спробою."
                if mutation else "Нова пошта тимчасово недоступна. Спробуйте пізніше.", uncertain=mutation) from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            # Upstream messages may include private data, so do not return/log them.
            raise ShippingError("shipping_rejected", "Нова пошта відхилила запит. Перевірте дані та права API-ключа.")
        if not isinstance(payload.get("data"), list):
            raise ShippingError("shipping_uncertain" if mutation else "shipping_response",
                                "Нова пошта повернула неповну відповідь. Перевірте акаунт перевізника.", uncertain=mutation)
        return payload["data"]

    async def cities(self, query: str = ""):
        if not isinstance(query, str) or len(query) > 80:
            raise ShippingError("invalid_search", "Перевірте назву міста.")
        rows = await self._call("Address", "getCities", {"FindByString": query.strip(), "Limit": 50})
        return [{"ref": r.get("Ref", ""), "name": r.get("Description", ""),
                 "area": r.get("AreaDescription", "")} for r in rows]

    async def warehouses(self, city_ref: str, query: str = ""):
        if not isinstance(query, str) or len(query) > 80:
            raise ShippingError("invalid_search", "Перевірте назву відділення.")
        rows = await self._call("Address", "getWarehouses", {"CityRef": ref(city_ref),
                     "FindByString": query.strip(), "Language": "UA", "Limit": 100})
        return [{"ref": r.get("Ref", ""), "name": r.get("Description", ""),
                 "number": r.get("Number", ""), "city_ref": r.get("CityRef", ""),
                 "status": r.get("WarehouseStatus", ""), "category": r.get("CategoryOfWarehouse", ""),
                 "max_weight": r.get("TotalMaxWeightAllowed", "")} for r in rows]

    async def senders(self):
        return await self._call("Counterparty", "getCounterparties", {"CounterpartyProperty": "Sender", "Page": "1"})

    async def contacts(self, sender_ref: str):
        return await self._call("Counterparty", "getCounterpartyContactPersons", {"Ref": ref(sender_ref)})

    async def addresses(self, sender_ref: str):
        return await self._call("Counterparty", "getCounterpartyAddresses", {"Ref": ref(sender_ref), "CounterpartyProperty": "Sender"})

    async def create_waybill(self, sender: dict, recipient: dict, parcel: dict, order_id: int):
        if not all(isinstance(v, dict) for v in (sender, recipient, parcel)):
            raise ShippingError("invalid_delivery", "Заповніть дані відправлення.")
        sr = {k: ref(sender.get(k)) for k in ("city_ref", "sender_ref", "warehouse_ref", "contact_ref")}
        rr = {k: ref(recipient.get(k)) for k in ("city_ref", "warehouse_ref")}
        sp, rp = phone(sender.get("phone")), phone(recipient.get("phone"))
        name = str(recipient.get("name", "")).strip()
        parts = name.split()
        if not 2 <= len(parts) <= 5 or len(name) > 100 or any(c in name for c in "<>\r\n"):
            raise ShippingError("invalid_recipient", "Вкажіть прізвище та ім’я отримувача.")
        weight = _number(parcel.get("weight"), .1, 1000, "вага, кг")
        dims = {k: _number(parcel.get(k), 1, 500, k) for k in ("length", "width", "height")}
        price = _number(parcel.get("price"), 1, 150000, "оголошена вартість")
        if int(price) != price:
            raise ShippingError("invalid_parcel", "Ціна має бути цілим числом гривень.")
        title = str(parcel.get("title", "Товар"))[:100].strip()
        if not title or any(c in title for c in "\r\n"):
            raise ShippingError("invalid_parcel", "Вкажіть опис вмісту посилки.")
        # Create a private recipient in this sender's account; API controls ownership.
        rec = await self._call("Counterparty", "save", {"FirstName": parts[1], "LastName": parts[0],
             "MiddleName": " ".join(parts[2:]), "Phone": rp, "Email": "", "CounterpartyType": "PrivatePerson",
             "CounterpartyProperty": "Recipient", "CityRef": rr["city_ref"]}, mutation=True)
        try:
            recipient_ref = ref(rec[0]["Ref"])
            contact_ref = ref(rec[0]["ContactPerson"]["data"][0]["Ref"])
        except (KeyError, IndexError, TypeError, ShippingError):
            raise ShippingError("shipping_recipient", "Перевізник не повернув контакт отримувача. Перевірте його дані в кабінеті.") from None
        props = {"PayerType": "Recipient", "PaymentMethod": "Cash", "DateTime": datetime.now(timezone.utc).strftime("%d.%m.%Y"),
            "CargoType": "Parcel" if weight <= 30 else "Cargo", "Weight": str(weight), "ServiceType": "WarehouseWarehouse",
            "SeatsAmount": "1", "Description": title, "Cost": str(int(price)),
            "CitySender": sr["city_ref"], "Sender": sr["sender_ref"], "SenderAddress": sr["warehouse_ref"],
            "ContactSender": sr["contact_ref"], "SendersPhone": sp,
            "CityRecipient": rr["city_ref"], "Recipient": recipient_ref, "RecipientAddress": rr["warehouse_ref"],
            "ContactRecipient": contact_ref, "RecipientsPhone": rp,
            "OptionsSeat": [{"weight": str(weight), "volumetricLength": str(dims["length"]),
                "volumetricWidth": str(dims["width"]), "volumetricHeight": str(dims["height"])}],
            "BackwardDeliveryData": [{"PayerType": "Recipient", "CargoType": "Money", "RedeliveryString": str(int(price))}],
            "InfoRegClientBarcodes": f"DOMIVKA-{int(order_id)}"}
        data = await self._call("InternetDocument", "save", props, mutation=True)
        try:
            row = data[0]
            ttn = str(row["IntDocNumber"])
            if not re.fullmatch(r"\d{14}", ttn):
                raise ValueError()
            document_ref = ref(row["Ref"])
        except (KeyError, IndexError, ValueError, ShippingError):
            raise ShippingError("shipping_uncertain", "Номер накладної не підтверджено. Перевірте кабінет Нової пошти; не створюйте дубль.", uncertain=True) from None
        return {"ttn": ttn, "document_ref": document_ref, "delivery_cost": row.get("CostOnSite"),
                "estimated_delivery": row.get("EstimatedDeliveryDate", ""), "payment": "cod"}

    async def track(self, ttn: str, recipient_phone: str = ""):
        if not re.fullmatch(r"\d{14}", str(ttn)):
            raise ShippingError("invalid_ttn", "Перевірте 14-значний номер накладної.")
        properties = {"Documents": [{"DocumentNumber": str(ttn), "Phone": phone(recipient_phone) if recipient_phone else ""}]}
        rows = await self._call("TrackingDocument", "getStatusDocuments", properties)
        if not rows:
            raise ShippingError("tracking_not_found", "Накладну поки не знайдено.")
        row = rows[0]
        # Carrier text/code are displayed; do not infer payment from delivery.
        return {"ttn": str(ttn), "status": str(row.get("Status", "")), "status_code": str(row.get("StatusCode", "")),
                "actual_delivery_date": row.get("ActualDeliveryDate", ""), "scheduled_delivery_date": row.get("ScheduledDeliveryDate", "")}
