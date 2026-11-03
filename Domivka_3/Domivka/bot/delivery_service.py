"""Server-owned delivery orchestration; sellers never supply carrier API keys.

A generic Nova Poshta account key does not establish a marketplace agreement or
identify the lawful COD recipient. Automatic COD documents are therefore disabled
until an administrator records carrier approval AND provisions each actual seller
as a sender/contact in that approved carrier account. Phone numbers alone are not
proof of entitlement. Never create every seller's COD under the platform sender.

Caller responsibilities: authenticate seller, use primary bundle order only,
atomically persist a single-attempt marker before calling auto_create, propagate
its result across all bundle rows, and never retry an uncertain attempt.
"""
import json
import os

import shipping


PENDING_MESSAGE = 'Автоматичне оформлення доставки ще не підключене оператором сервісу. Продавець може створити накладну у своєму застосунку Нової пошти та додати її номер.'


def delivery_mode():
    if not os.environ.get('NOVA_POSHTA_API_KEY', '').strip():
        return 'central_not_configured'
    if os.environ.get('NOVA_POSHTA_MARKETPLACE_ENABLED', '') != '1':
        return 'approval_required'
    return 'approved'


def central_client():
    key = os.environ.get('NOVA_POSHTA_API_KEY', '').strip()
    if not key:
        raise shipping.ShippingError('carrier_not_configured', PENDING_MESSAGE)
    return shipping.NovaPoshta(key)


def normalize_sender_profile(data):
    if not isinstance(data, dict):
        raise shipping.ShippingError('invalid_sender', 'Заповніть дані відправника.')
    # Reject credential fields rather than silently retaining old seller API keys.
    if any(k in data for k in ('api_key', 'token', 'credentials')):
        raise shipping.ShippingError('invalid_sender', 'Продавцю не потрібно вводити API-ключ перевізника.')
    name = str(data.get('name', '')).strip()
    if not 2 <= len(name.split()) <= 5 or len(name) > 100 or any(ord(c) < 32 or c in '<>' for c in name):
        raise shipping.ShippingError('invalid_sender', 'Вкажіть прізвище та ім’я відправника.')
    return {'name': name, 'phone': shipping.phone(data.get('phone')),
            'city_ref': shipping.ref(data.get('city_ref')), 'warehouse_ref': shipping.ref(data.get('warehouse_ref'))}


async def validate_sender_directory(profile, *, client=None):
    # Ask carrier for this city, not just arbitrary unverified text supplied by UI.
    client = client or central_client()
    rows = await client.warehouses(profile['city_ref'], '')
    found = next((r for r in rows if r.get('ref') == profile['warehouse_ref']), None)
    # getWarehouses is paginated/limited: permit searching by exact Ref for large
    # cities through the adapter instead of rejecting a valid branch beyond page 1.
    if not found and hasattr(client, 'warehouse_by_ref'):
        found = await client.warehouse_by_ref(profile['city_ref'], profile['warehouse_ref'])
    if not found or found.get('city_ref') not in ('', profile['city_ref']):
        raise shipping.ShippingError('invalid_sender_branch', 'Оберіть відділення Нової пошти у вказаному місті.')
    if found.get('category', '').lower() in ('postomat', 'postomatprivatbank'):
        raise shipping.ShippingError('unsupported_sender_branch', 'Для цього способу відправлення оберіть відділення, а не поштомат.')
    if found.get('status', '') not in ('', 'Working'):
        raise shipping.ShippingError('invalid_sender_branch', 'Оберіть діюче відділення Нової пошти.')
    return profile


def _seller_mapping(uid, profile):
    try:
        mappings = json.loads(os.environ.get('NOVA_POSHTA_SELLER_MAPPINGS', '{}'))
        mapping = mappings.get(str(int(uid))) if isinstance(mappings, dict) else None
        if not isinstance(mapping, dict) or mapping.get('approved') is not True or mapping.get('cod_recipient_is_sender') is not True:
            return None
        if shipping.phone(mapping.get('phone')) != profile['phone']:
            return None
        return dict(profile, sender_ref=shipping.ref(mapping.get('sender_ref')),
                    contact_ref=shipping.ref(mapping.get('contact_ref')))
    except (TypeError, ValueError, shipping.ShippingError):
        return None


def _result(state, code, message, **extra):
    return dict(state=state, code=code, message=message, **extra)


async def auto_create(uid, order, sender_profile=None, *, client=None):
    if not isinstance(order, dict) or order.get('seller_id') != uid:
        raise shipping.ShippingError('order_forbidden', 'Замовлення недоступне.')
    if order.get('carrier') != 'nova':
        return _result('manual', 'manual_carrier', 'Для цього перевізника продавець додає справжній номер накладної самостійно.')
    if order.get('payment') != 'cod':
        raise shipping.ShippingError('unsupported_payment', 'Автоматичне оформлення доступне для післяплати.')
    if order.get('ttn'):
        return _result('created', 'already_created', 'Накладну вже створено.', ttn=order['ttn'])
    if order.get('status') != 'accepted':
        return _result('awaiting_order_acceptance', 'seller_acceptance_required', 'Спочатку продавець має підтвердити замовлення.')
    if order.get('shipping_state') == 'reconciliation_required':
        return _result('reconciliation_required', 'shipping_uncertain', 'Спочатку перевірте попередню спробу в кабінеті перевізника. Повторне створення заблоковано.')
    if order.get('bundle_id') and not order.get('bundle_primary', 1):
        return _result('awaiting_primary_order', 'bundle_primary_required', 'Для групи товарів створюється одна спільна накладна.')
    mode = delivery_mode()
    if mode != 'approved':
        return _result('awaiting_carrier_setup', mode, PENDING_MESSAGE)
    if not sender_profile:
        return _result('awaiting_sender_profile', 'sender_required', 'Продавцю потрібно заповнити ім’я, телефон, місто та відділення відправлення.')
    profile = normalize_sender_profile(sender_profile)
    sender = _seller_mapping(uid, profile)
    if not sender:
        return _result('awaiting_sender_mapping', 'seller_mapping_required', 'Автоматична післяплата для цього продавця ще не активована перевізником. Можна додати власну накладну.')
    client = client or central_client()
    try:
        senders = await client.senders()
        if not any(s.get('Ref') == sender['sender_ref'] for s in senders):
            return _result('awaiting_sender_mapping', 'sender_not_registered', 'Відправника не підтверджено у підключеному акаунті перевізника.')
        contacts = await client.contacts(sender['sender_ref'])
        contact = next((c for c in contacts if c.get('Ref') == sender['contact_ref']), None)
        phones = (contact or {}).get('Phones') or (contact or {}).get('Phone') or []
        if isinstance(phones, str):
            phones = [phones]
        normalized = []
        for p in phones if isinstance(phones, list) else []:
            try:
                normalized.append(shipping.phone(p))
            except shipping.ShippingError:
                pass
        if profile['phone'] not in normalized:
            return _result('awaiting_sender_mapping', 'sender_contact_mismatch', 'Телефон відправника не збігається з підтвердженим контактом перевізника.')
        await validate_sender_directory(profile, client=client)
        parcel = dict(order.get('parcel') or {}, price=order['price'], title=order.get('title') or 'Товар')
        waybill = await client.create_waybill(sender, order['recipient'], parcel, int(order['id']))
        return _result('created', 'created', 'Накладну створено.', **waybill)
    except shipping.ShippingError as exc:
        return _result('reconciliation_required' if exc.uncertain else 'failed', exc.code, str(exc))
