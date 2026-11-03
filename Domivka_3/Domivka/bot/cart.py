"""Seller-grouped carts and atomic secondhand bundles.

One bundle has one payable primary market order; component orders reserve each
unique listing and carry zero payable amount. Original prices remain immutable
in cart_group_items. Never create shipping labels for component orders.
"""
import asyncio
import html
import json
import re
import sqlite3

import db
import market

B = None
DB = None
LOCK = asyncio.Lock()
CartError = market.MarketError
SCHEMA = """
create table if not exists cart_entries(
 user_id integer not null,listing_id integer not null,
 created text not null default(datetime('now')),primary key(user_id,listing_id));
create table if not exists cart_groups(
 id integer primary key autoincrement,buyer_id integer not null,seller_id integer not null,
 request_id text not null,fingerprint text not null,primary_order_id integer,
 subtotal integer not null,total integer not null,created text not null default(datetime('now')),
 unique(buyer_id,request_id));
create table if not exists cart_group_items(
 group_id integer not null,order_id integer not null unique,listing_id integer not null,
 title text not null,price integer not null,photos text not null,city text not null,
 primary key(group_id,listing_id));
create index if not exists cart_seller_groups on cart_groups(seller_id,buyer_id);
"""


async def setup(bot_module):
    global B, DB
    B = bot_module
    rows = await db.q('pragma database_list')
    path = next((r['file'] for r in rows if r['name'] == 'main'), '')
    if not path:
        raise RuntimeError('Cart requires a file-backed database')
    if DB is not None:
        DB.close()
    DB = sqlite3.connect(path, timeout=3)
    DB.row_factory = sqlite3.Row
    DB.executescript(SCHEMA)
    have = {r['name'] for r in DB.execute('pragma table_info(market_orders)')}
    if 'bundle_id' not in have:
        DB.execute('alter table market_orders add column bundle_id integer')
    if 'bundle_primary' not in have:
        DB.execute('alter table market_orders add column bundle_primary integer not null default 1')
    DB.commit()


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _user(conn, uid):
    row = _one(conn, 'select * from users where id=? and banned=0', (uid,))
    if not row:
        raise market.MarketError('Обліковий запис недоступний.', 403)
    return row


def _blocked(conn, buyer, seller):
    return bool(_one(conn, 'select 1 from market_blocks where (user_id=? and subject=?) or (user_id=? and subject=?)',
                     (buyer, seller, seller, buyer)))


def _snap(row):
    out = dict(row)
    out['photos'] = json.loads(out.get('photos') or '[]')
    out['quantity'] = 1
    return out


def bundle_scope(conn, order_id):
    """Internal shipment scope. Caller must authorize order participants first."""
    if 'bundle_id' not in {r['name'] for r in conn.execute('pragma table_info(market_orders)')}:
        return None
    row = _one(conn, 'select bundle_id from market_orders where id=?', (order_id,))
    if not row or not row.get('bundle_id'):
        return None
    group = _one(conn, 'select * from cart_groups where id=?', (row['bundle_id'],))
    if not group:
        raise market.MarketError('Комплект замовлення недоступний.', 409)
    items = [_snap(x) for x in _rows(conn, 'select * from cart_group_items where group_id=? order by order_id', (group['id'],))]
    return {'group_id': group['id'], 'primary_order_id': group['primary_order_id'],
            'order_ids': [x['order_id'] for x in items], 'total': group['total'], 'items': items}


def enrich_order(conn, row, uid):
    """Add immutable bundle contents to a market order without network calls."""
    if uid not in (row['buyer_id'], row['seller_id']):
        raise market.MarketError('Замовлення недоступне.', 404)
    if not row.get('bundle_id'):
        return row
    scope = bundle_scope(conn, row['id'])
    if not scope:
        return row
    row = dict(row)
    row.update(bundle_id=scope['group_id'], primary_order_id=scope['primary_order_id'],
               bundle_primary=row['id'] == scope['primary_order_id'], items=scope['items'],
               bundle_total=scope['total'])
    if row['bundle_primary']:
        row['title'] = 'Комплект: ' + ', '.join(x['title'] for x in scope['items'])[:170]
        row['price'] = scope['total']
    return row


def _order(conn, oid, uid):
    row = _one(conn, 'select o.*,l.title,l.parcel,s.name seller_name,b.name buyer_name from market_orders o '
               'join market_listings l on l.id=o.listing_id join users s on s.id=o.seller_id '
               'join users b on b.id=o.buyer_id where o.id=?', (oid,))
    if not row or uid not in (row['buyer_id'], row['seller_id']):
        raise market.MarketError('Замовлення недоступне.', 404)
    row['recipient'] = json.loads(row['recipient'])
    row['parcel'] = json.loads(row.get('parcel') or '{}')
    row['mine_as_seller'] = uid == row['seller_id']
    return enrich_order(conn, row, uid)


def _group(conn, gid, uid):
    group = _one(conn, 'select * from cart_groups where id=?', (gid,))
    if not group or uid not in (group['buyer_id'], group['seller_id']):
        raise market.MarketError('Комплект недоступний.', 404)
    primary = _order(conn, group['primary_order_id'], uid)
    return {k: group[k] for k in ('id', 'buyer_id', 'seller_id', 'primary_order_id', 'subtotal', 'total', 'created')} | {
        'items': primary['items'], 'status': primary['status'], 'ttn': primary['ttn'],
        'shipping_state': primary['shipping_state']}


def apply_order_action(conn, uid, order_id, data):
    """Called INSIDE market's existing transaction; atomically moves a bundle.

    Returns None for ordinary orders. Do not begin/commit another transaction.
    The market dispatcher supplies an authenticated user and commits the result.
    """
    scope = bundle_scope(conn, order_id)
    if not scope:
        return None
    _user(conn, uid)
    o = _order(conn, scope['primary_order_id'], uid)
    if order_id != scope['primary_order_id']:
        raise market.MarketError('Керуйте комплектом через основне замовлення.', 409)
    states = _rows(conn, 'select status,shipping_state,ttn from market_orders where bundle_id=?', (scope['group_id'],))
    if len(states) != len(scope['items']) or any(s['status'] != o['status'] for s in states):
        raise market.MarketError('Статус комплекту потребує перевірки підтримкою.', 409)
    if any(s['shipping_state'] in ('creating', 'reconciliation_required') for s in states):
        raise market.MarketError('Перевірте накладну в кабінеті перевізника перед зміною замовлення.', 409)
    status = data.get('status')
    allowed = ((status == 'accepted' and uid == o['seller_id'] and o['status'] == 'pending')
               or (status == 'shipped' and uid == o['seller_id'] and o['status'] == 'accepted' and o['carrier'] != 'pickup')
               or (status == 'completed' and uid == o['buyer_id'] and o['status'] in ('accepted', 'shipped'))
               or (status == 'cancelled' and o['status'] in ('pending', 'accepted')))
    if not allowed:
        raise market.MarketError('Цей перехід статусу комплекту недоступний.', 409)
    ttn = o['ttn']
    if status == 'shipped':
        ttn = market.text(data.get('ttn', ttn), 13, 14)
        if not re.fullmatch(r'\d{14}' if o['carrier'] == 'nova' else r'\d{13}', ttn):
            raise market.MarketError('Перевірте номер накладної.')
    conn.execute('update market_orders set status=?,ttn=? where bundle_id=?', (status, ttn, scope['group_id']))
    if status in ('cancelled', 'completed'):
        conn.execute("update market_listings set status=? where status='reserved' and id in "
                     '(select listing_id from cart_group_items where group_id=?)',
                     ('active' if status == 'cancelled' else 'sold', scope['group_id']))
    return {'item': _order(conn, scope['primary_order_id'], uid), 'group': _group(conn, scope['group_id'], uid)}


def _recipient(data, carrier):
    rec = data.get('recipient')
    if not isinstance(rec, dict):
        raise market.MarketError('Заповніть дані отримувача.')
    result = {'name': market.text(rec.get('name'), 3, 120), 'phone': market.text(rec.get('phone'), 10, 16),
              'city': market.text(rec.get('city'), 2, 100),
              'branch': market.text(rec.get('branch', ''), 0 if carrier == 'pickup' else 1, 160),
              'city_ref': market.text(rec.get('city_ref', ''), 0, 80),
              'warehouse_ref': market.text(rec.get('warehouse_ref', ''), 0, 80)}
    if not re.fullmatch(r'\+?380\d{9}', result['phone']):
        raise market.MarketError('Телефон: +380 і 9 цифр.')
    return result


def _cart(conn, uid):
    rows = _rows(conn, 'select l.*,u.name seller_name,u.banned seller_banned from cart_entries c '
                 'join market_listings l on l.id=c.listing_id join users u on u.id=l.seller_id '
                 'where c.user_id=? order by l.seller_id,c.created,l.id', (uid,))
    groups = {}
    for row in rows:
        seller = row['seller_id']
        group = groups.setdefault(seller, {'seller_id': seller, 'seller_name': row['seller_name'], 'items': [], 'subtotal': 0, 'count': 0})
        item = _snap(row)
        item['available'] = row['status'] == 'active' and not row['seller_banned'] and not _blocked(conn, uid, seller)
        item.pop('seller_banned', None)
        group['items'].append(item)
        group['subtotal'] += row['price']
        group['count'] += 1
    return {'groups': list(groups.values()), 'count': len(rows)}


def _dispatch(conn, uid, path, data, method):
    _user(conn, uid)
    parts = path.strip('/').split('/') if path.strip('/') else []
    if not parts and method == 'GET':
        return _cart(conn, uid)
    if parts == ['items'] and method == 'POST':
        lid = market.integer(data.get('listing_id'))
        quantity = market.integer(data.get('quantity', 1))
        if quantity != 1:
            raise market.MarketError('Вживані речі доступні по одній одиниці.')
        listing = _one(conn, 'select l.*,u.banned seller_banned from market_listings l join users u on u.id=l.seller_id where l.id=?', (lid,))
        if not listing or listing['seller_banned'] or listing['status'] != 'active':
            raise market.MarketError('Товар недоступний.', 409)
        if listing['seller_id'] == uid:
            raise market.MarketError('Не можна додати власний товар.')
        if _blocked(conn, uid, listing['seller_id']):
            raise market.MarketError('Угода з цим користувачем недоступна.', 403)
        if not _one(conn, 'select 1 from cart_entries where user_id=? and listing_id=?', (uid, lid)):
            if _one(conn, 'select count(*) n from cart_entries where user_id=?', (uid,))['n'] >= 50:
                raise market.MarketError('Максимум 50 речей у кошику.', 409)
            conn.execute('insert into cart_entries(user_id,listing_id) values(?,?)', (uid, lid))
        return {'ok': True, **_cart(conn, uid)}
    if len(parts) == 2 and parts[0] == 'items' and method == 'DELETE':
        conn.execute('delete from cart_entries where user_id=? and listing_id=?', (uid, market.integer(parts[1])))
        return {'ok': True, **_cart(conn, uid)}
    if len(parts) == 2 and parts[0] == 'groups' and method == 'GET':
        return {'item': _group(conn, market.integer(parts[1]), uid)}
    if parts == ['checkout'] and method == 'POST':
        seller = market.integer(data.get('seller_id'))
        key = market.text(data.get('request_id'), 8, 100)
        carrier, payment = data.get('carrier'), data.get('payment', 'cod')
        if carrier not in ('nova', 'ukr', 'pickup') or payment != ('pickup' if carrier == 'pickup' else 'cod'):
            raise market.MarketError('Оберіть післяплату або оплату при зустрічі.')
        recipient = _recipient(data, carrier)
        expected = market.integer(data.get('expected_total'), 1, 2_000_000_000)
        fingerprint = json.dumps({'seller_id': seller, 'carrier': carrier, 'payment': payment,
                                  'recipient': recipient, 'expected_total': expected}, sort_keys=True, ensure_ascii=False)
        prior = _one(conn, 'select * from cart_groups where buyer_id=? and request_id=?', (uid, key))
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise market.MarketError('Ключ запиту вже використано для іншого комплекту.', 409)
            return {'item': _order(conn, prior['primary_order_id'], uid), 'group': _group(conn, prior['id'], uid)}
        if seller == uid:
            raise market.MarketError('Не можна купити власні товари.')
        _user(conn, seller)
        if _blocked(conn, uid, seller):
            raise market.MarketError('Угода з цим користувачем недоступна.', 403)
        items = _rows(conn, 'select l.* from cart_entries c join market_listings l on l.id=c.listing_id '
                      'where c.user_id=? and l.seller_id=? order by l.id', (uid, seller))
        if not 1 <= len(items) <= 20:
            raise market.MarketError('Оберіть від 1 до 20 речей одного продавця.')
        if any(item['status'] != 'active' for item in items):
            raise market.MarketError('Частина товарів уже недоступна. Оновіть кошик.', 409)
        total = sum(item['price'] for item in items)
        if total != expected:
            raise market.MarketError('Ціна в кошику змінилася. Оновіть його перед оформленням.', 409)
        gid = conn.execute('insert into cart_groups(buyer_id,seller_id,request_id,fingerprint,subtotal,total) values(?,?,?,?,?,?)',
                           (uid, seller, key, fingerprint, total, total)).lastrowid
        primary = None
        for index, item in enumerate(items):
            oid = conn.execute('insert into market_orders(listing_id,buyer_id,seller_id,request_id,price,carrier,payment,recipient,bundle_id,bundle_primary) '
                               'values(?,?,?,?,?,?,?,?,?,?)',
                               (item['id'], uid, seller, f'cart:{gid}:{index}', total if index == 0 else 0,
                                carrier, payment, json.dumps(recipient, ensure_ascii=False), gid, int(index == 0))).lastrowid
            if primary is None:
                primary = oid
            conn.execute('insert into cart_group_items(group_id,order_id,listing_id,title,price,photos,city) values(?,?,?,?,?,?,?)',
                         (gid, oid, item['id'], item['title'], item['price'], item['photos'], item['city']))
            changed = conn.execute("update market_listings set status='reserved' where id=? and status='active'", (item['id'],)).rowcount
            if changed != 1:
                raise market.MarketError('Товар уже зарезервовано. Оновіть кошик.', 409)
        conn.execute('update cart_groups set primary_order_id=? where id=?', (primary, gid))
        conn.execute('delete from cart_entries where user_id=? and listing_id in (select listing_id from cart_group_items where group_id=?)', (uid, gid))
        return {'item': _order(conn, primary, uid), 'group': _group(conn, gid, uid)}
    raise market.MarketError('Невідомий маршрут кошика.', 404)


async def dispatch(uid, path='/', data=None, method='GET'):
    if DB is None:
        raise market.MarketError('Кошик ще не запущено.', 503)
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise market.MarketError('Некоректний запит.')
    async with LOCK:
        try:
            if method != 'GET':
                DB.execute('begin immediate')
            result = _dispatch(DB, uid, path, data, method)
            if method != 'GET':
                DB.commit()
        except sqlite3.IntegrityError:
            DB.rollback()
            raise market.MarketError('Товар уже зарезервовано. Оновіть кошик.', 409) from None
        except Exception:
            DB.rollback()
            raise
    if path.strip('/') == 'checkout' and method == 'POST' and getattr(B, 'tell', None):
        try:
            order = result['item']
            await B.tell(order['seller_id'], '<b>Замовлення комплекту №' + str(order['id']) + '</b>\n'
                         + html.escape(order['title']) + '\n' + str(order['price']) + ' грн', [[('Замовлення', 'mk:orders')]])
        except Exception:
            pass
    return result


async def handle(request, uid):
    from aiohttp import web
    try:
        data = dict(request.query) if request.method == 'GET' else ({} if request.method == 'DELETE' else await request.json())
        result = await dispatch(uid, request.path.removeprefix('/api/cart') or '/', data, request.method)
        return web.json_response(result)
    except market.MarketError as e:
        return web.json_response({'error': e.message, 'code': 'cart'}, status=e.status)
    except (ValueError, TypeError):
        return web.json_response({'error': 'Некоректний запит.', 'code': 'cart'}, status=400)
