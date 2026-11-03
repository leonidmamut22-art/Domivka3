"""Shared marketplace service for Telegram and Mini App. Money never passes through us.

All mutations are serialized and committed on a dedicated SQLite connection, so
the existing rental connection cannot accidentally commit a market reservation.
"""
import asyncio
import io
import html
import logging
import json
import re
import sqlite3
import storage
from pathlib import Path
from urllib.parse import urlparse

import db
import delivery_service

B = None
DB = None
LOCK = asyncio.Lock()
CATEGORIES = {"furniture", "electronics", "clothes", "home", "sport", "kids", "transport", "other"}
CONDITIONS = {"new", "like_new", "used", "for_parts"}
SCHEMA = """
create table if not exists market_listings(
 id integer primary key autoincrement, seller_id integer not null,
 title text not null, descr text not null, city text not null, category text not null,
 condition text not null, price integer not null check(price>=1), photos text not null default '[]',parcel text not null default '{}',
 status text not null default 'active', created text not null default (datetime('now')));
create index if not exists market_search on market_listings(status,category,city);
create table if not exists market_favorites(user_id integer,listing_id integer,primary key(user_id,listing_id));
create table if not exists market_chats(id integer primary key autoincrement,listing_id integer not null,buyer_id integer not null,seller_id integer not null,unique(listing_id,buyer_id));
create table if not exists market_messages(id integer primary key autoincrement,chat_id integer not null,sender integer not null,body text not null,created text not null default(datetime('now')));
create table if not exists market_blocks(user_id integer,subject integer,primary key(user_id,subject));
create table if not exists market_reports(id integer primary key autoincrement,user_id integer,listing_id integer,reason text,status text default 'open',created text default(datetime('now')));
create table if not exists market_orders(
 id integer primary key autoincrement,listing_id integer not null,buyer_id integer not null,seller_id integer not null,
 request_id text not null,price integer not null,carrier text not null,payment text not null,recipient text not null,
 status text not null default 'pending',ttn text not null default '',shipping_state text not null default '',shipping_error text not null default '',created text not null default(datetime('now')),
 unique(buyer_id,request_id));
create unique index if not exists market_one_reservation on market_orders(listing_id) where status in ('pending','accepted','shipped');
create table if not exists market_reviews(id integer primary key autoincrement,order_id integer not null unique,author integer not null,subject integer not null,rating integer not null check(rating between 1 and 5),text text,created text default(datetime('now')));
create table if not exists market_delivery(seller_id integer primary key,credentials text not null);
create table if not exists market_sender_profiles(seller_id integer primary key,body text not null);
create table if not exists market_uploads(user_id integer not null,file_id text not null,primary key(user_id,file_id));
"""


class MarketError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


import cart


async def setup(bot_module):
    global B, DB
    B = bot_module
    if DB is not None:
        DB.close()
    DB = storage.connect(db.storage_target())
    DB.row_factory = sqlite3.Row
    DB.executescript(SCHEMA)
    have = {r['name'] for r in DB.execute('pragma table_info(market_orders)')}
    for col in ('shipping_state','shipping_error'):
        if col not in have:
            DB.execute('alter table market_orders add column '+col+" text not null default ''")
    if 'parcel' not in {r['name'] for r in DB.execute('pragma table_info(market_listings)')}:
        DB.execute("alter table market_listings add column parcel text not null default '{}'")
    DB.commit()


def q(sql, args=()):
    return [dict(r) for r in DB.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def integer(value, lo=1, hi=2**63-1):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise MarketError('Вкажіть коректне число.')
    result = int(value)
    if not lo <= result <= hi:
        raise MarketError('Число поза дозволеним діапазоном.')
    return result


def text(value, lo=0, hi=2000):
    if not isinstance(value, str):
        raise MarketError('Очікується текст.')
    value = value.strip()
    if not lo <= len(value) <= hi or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise MarketError('Перевірте довжину та вміст тексту.')
    return value


def photos(value):
    if not isinstance(value, list) or len(value) > 10:
        raise MarketError('Додайте не більше 10 фото.')
    out = []
    for entry in value:
        s = text(entry, 10, 1500)
        if s.startswith('https://'):
            p = urlparse(s)
            if not p.hostname or p.username or p.password or p.port not in (None, 443):
                raise MarketError('Некоректне посилання на фото.')
        elif not re.fullmatch(r'[A-Za-z0-9_-]{20,}', s):
            raise MarketError('Фото має бути завантажене в Telegram або мати HTTPS-посилання.')
        out.append(s)
    return json.dumps(out, ensure_ascii=False)


def blocked(a, b):
    return bool(one('select 1 from market_blocks where (user_id=? and subject=?) or (user_id=? and subject=?)', (a,b,b,a)))


def listing(lid, uid, private=False):
    import business
    private = private or uid in getattr(B, 'ADMINS', set()) or business.role(uid) in ('owner','admin','moderator')
    r = one('select l.*,u.name seller_name from market_listings l join users u on u.id=l.seller_id where l.id=? and u.banned=0', (lid,))
    if not r or (r['status'] not in ('active','reserved','sold') and r['seller_id'] != uid and not private):
        raise MarketError('Оголошення недоступне.',404)
    r['photos'] = json.loads(r['photos'])
    r['parcel'] = json.loads(r.get('parcel') or '{}')
    r['favorite'] = bool(one('select 1 from market_favorites where user_id=? and listing_id=?',(uid,lid)))
    r['mine'] = r['seller_id'] == uid
    return r


def order(oid, uid):
    r = one('select o.*,l.title,l.parcel,s.name seller_name,b.name buyer_name from market_orders o join market_listings l on l.id=o.listing_id join users s on s.id=o.seller_id join users b on b.id=o.buyer_id where o.id=?',(oid,))
    if not r or uid not in (r['buyer_id'],r['seller_id']):
        raise MarketError('Замовлення недоступне.',404)
    r['recipient'] = json.loads(r['recipient'])
    r['parcel'] = json.loads(r.get('parcel') or '{}')
    r['mine_as_seller'] = uid == r['seller_id']
    return cart.enrich_order(DB, r, uid)


def chat(cid, uid):
    r = one('select * from market_chats where id=?',(cid,))
    if not r or uid not in (r['buyer_id'],r['seller_id']):
        raise MarketError('Чат недоступний.',404)
    return r


def _dispatch(uid, path, data, method):
    u = one('select * from users where id=?',(uid,))
    if not u or u['banned']:
        raise MarketError('Відкрийте бот і зареєструйтеся. Обліковий запис має бути активним.',403)
    parts = path.strip('/').split('/')
    kind = parts[0]
    ident = integer(parts[1]) if len(parts)>1 else None
    admin = uid in getattr(B,'ADMINS',set())
    if kind == 'listings':
        if method=='PATCH' and not ident:raise MarketError('Оберіть оголошення.',404)
        if ident and method == 'GET':
            return {'item':listing(ident,uid)}
        if ident and method in ('PATCH','DELETE'):
            old = listing(ident,uid,admin)
            if old['seller_id'] != uid and not admin:
                raise MarketError('Змінювати оголошення може лише автор.',403)
            if old['status'] in ('reserved','sold'):
                raise MarketError('Спочатку завершіть або скасуйте замовлення.',409)
            if method == 'DELETE':
                DB.execute("update market_listings set status='deleted' where id=?",(ident,))
                return {'ok':True}
            if admin and set(data) == {'status'} and data['status'] in ('active','blocked'):
                DB.execute('update market_listings set status=? where id=?',(data['status'],ident))
                return {'item':listing(ident,uid,True)}
            if old['status'] == 'blocked':
                raise MarketError('Оголошення заблоковане модератором.',403)
        if method in ('POST','PATCH'):
            if method == 'POST' and ident:
                raise MarketError('Невідомий маршрут.',404)
            if method == 'POST' and one("select count(*) n from market_listings where seller_id=? and created>datetime('now','-1 day')",(uid,))['n'] >= 30:
                raise MarketError('Ліміт: 30 нових оголошень на добу.',429)
            merged = {**(old if ident else {}),**data}
            title,descr,city = text(merged.get('title'),3,120),text(merged.get('descr'),20,4000),text(merged.get('city'),2,80)
            cat,cond = merged.get('category','other'),merged.get('condition','used')
            cat={'clothing':'clothes','children':'kids'}.get(cat,cat)
            if cat not in CATEGORIES or cond not in CONDITIONS:
                raise MarketError('Оберіть категорію та стан товару.')
            price,ps = integer(merged.get('price'),1,100000000),photos(merged.get('photos',[]))
            previous = old['photos'] if ident else []
            for fid in json.loads(ps):
                if not fid.startswith('https://') and fid not in previous and not one('select 1 from market_uploads where user_id=? and file_id=?',(uid,fid)):
                    raise MarketError('Спочатку завантажте власне фото.')
            status = 'pending' if getattr(B,'APPROVAL',False) else 'active'
            values=(title,descr,city,cat,cond,price,ps,status)
            if ident:
                DB.execute('update market_listings set title=?,descr=?,city=?,category=?,condition=?,price=?,photos=?,status=? where id=?',values+(ident,))
            else:
                ident=DB.execute('insert into market_listings(title,descr,city,category,condition,price,photos,status,seller_id) values(?,?,?,?,?,?,?,?,?)',values+(uid,)).lastrowid
            parcel=merged.get('parcel',{})
            if parcel:
                if not isinstance(parcel,dict):raise MarketError('Перевірте параметри посилки.')
                try:parcel={k:float(parcel[k]) for k in ('weight','length','width','height')}
                except (KeyError,ValueError,TypeError):raise MarketError('Заповніть вагу та всі розміри посилки.')
                if not all(0<v<=1000 for v in parcel.values()):raise MarketError('Перевірте параметри посилки.')
                DB.execute('update market_listings set parcel=? where id=?',(json.dumps(parcel),ident))
            return {'item':listing(ident,uid)}
        if method == 'GET':
            clauses,args=['u.banned=0'],[]
            if data.get('mine') == '1' or data.get('mine') == 1:
                clauses+=['l.seller_id=?',"l.status!='deleted'"];args.append(uid)
            else:
                clauses.append("l.status='active'")
            for field in ('city','category'):
                if data.get(field):
                    value=text(data[field],1,80)
                    if field=='category':value={'clothing':'clothes','children':'kids'}.get(value,value)
                    clauses.append('l.'+field+'=?');args.append(value)
            if data.get('q'):
                clauses.append('(l.title like ? or l.descr like ?)');v='%'+text(data['q'],1,100)+'%';args.extend([v,v])
            for k,op in (('min','>='),('max','<=')):
                if data.get(k):clauses.append('l.price'+op+'?');args.append(integer(data[k],0))
            ids=q('select l.id from market_listings l join users u on u.id=l.seller_id where '+' and '.join(clauses)+' order by l.id desc limit 100',args)
            return {'items':[listing(x['id'],uid) for x in ids]}
    if kind == 'sellers' and ident and method == 'GET':
        seller=one('select id,name,created from users where id=? and banned=0',(ident,))
        if not seller:raise MarketError('Профіль недоступний.',404)
        reviews=q('select r.*,u.name author_name from market_reviews r join users u on u.id=r.author where subject=? order by r.id desc limit 100',(ident,))
        stats=one('select count(*) review_count,avg(rating) rating from market_reviews where subject=?',(ident,))
        seller.update(stats);seller['reviews']=reviews
        seller['completed']=one("select count(*) n from market_orders where seller_id=? and status='completed' and price>0",(ident,))['n']
        seller['listings']=[listing(r['id'],uid) for r in q("select id from market_listings where seller_id=? and status='active' order by id desc limit 50",(ident,))]
        return {'item':seller}
    if kind == 'favorites':
        if method == 'POST':
            lid=integer(data.get('listing_id'));listing(lid,uid)
            exists=one('select 1 from market_favorites where user_id=? and listing_id=?',(uid,lid))
            if exists:DB.execute('delete from market_favorites where user_id=? and listing_id=?',(uid,lid))
            else:DB.execute('insert into market_favorites values(?,?)',(uid,lid))
            return {'ok':True,'favorite':not bool(exists)}
        if method == 'GET':
            return {'items':[listing(x['id'],uid) for x in q("select l.id from market_favorites f join market_listings l on l.id=f.listing_id join users u on u.id=l.seller_id where f.user_id=? and l.status in ('active','reserved','sold') and u.banned=0 order by l.id desc limit 100",(uid,))]}
    if kind == 'chats':
        if ident:
            c=chat(ident,uid)
            if len(parts)!=3 or parts[2]!='messages':raise MarketError('Невідомий маршрут.',404)
            if method=='GET':return {'items':q('select * from market_messages where chat_id=? and id>? order by id limit 200',(ident,integer(data.get('after',0),0)))}
            if method=='POST':
                if blocked(c['buyer_id'],c['seller_id']):raise MarketError('Листування заблоковано.',403)
                body=text(data.get('body'),1,2000)
                if one("select count(*) n from market_messages where sender=? and created>datetime('now','-1 minute')",(uid,))['n']>=30:raise MarketError('Зачекайте перед наступним повідомленням.',429)
                mid=DB.execute('insert into market_messages(chat_id,sender,body) values(?,?,?)',(ident,uid,body)).lastrowid
                return {'item':one('select * from market_messages where id=?',(mid,))}
        if method=='GET':
            return {'items':q('select c.*,l.title,u.name peer_name,(select body from market_messages m where m.chat_id=c.id order by m.id desc limit 1) last_message from market_chats c join market_listings l on l.id=c.listing_id join users u on u.id=case when c.buyer_id=? then c.seller_id else c.buyer_id end where buyer_id=? or c.seller_id=? order by c.id desc limit 100',(uid,uid,uid))}
        if method=='POST':
            l=listing(integer(data.get('listing_id')),uid)
            if l['seller_id']==uid:raise MarketError('Це ваше оголошення.')
            if blocked(uid,l['seller_id']):raise MarketError('Листування заблоковано.',403)
            DB.execute('insert or ignore into market_chats(listing_id,buyer_id,seller_id) values(?,?,?)',(l['id'],uid,l['seller_id']))
            return {'item':one('select * from market_chats where listing_id=? and buyer_id=?',(l['id'],uid))}
    if kind == 'orders':
        if ident:
            o=order(ident,uid)
            if len(parts)==3 and parts[2]=='review' and method=='POST':
                if not o.get('bundle_primary', True):raise MarketError('Відгук додається до основного замовлення комплекту.',409)
                if uid!=o['buyer_id'] or o['status']!='completed':raise MarketError('Відгук доступний покупцю після завершення покупки.',403)
                if one('select 1 from market_reviews where order_id=?',(ident,)):raise MarketError('Відгук уже залишено.',409)
                DB.execute('insert into market_reviews(order_id,author,subject,rating,text) values(?,?,?,?,?)',(ident,uid,o['seller_id'],integer(data.get('rating'),1,5),text(data.get('text',''),0,2000)))
                return {'ok':True}
            if method=='GET':return {'item':o}
            if method=='PATCH':
                bundled = cart.apply_order_action(DB, uid, ident, data)
                if bundled is not None:
                    return bundled
                status=data.get('status')
                if o['shipping_state'] in ('creating','reconciliation_required'):
                    raise MarketError('Статус накладної уточнюється. Перевірте кабінет Нової пошти перед зміною замовлення.',409)
                allowed = (status=='accepted' and uid==o['seller_id'] and o['status']=='pending') or (status=='shipped' and uid==o['seller_id'] and o['status']=='accepted' and o['carrier']!='pickup') or (status=='completed' and uid==o['buyer_id'] and o['status'] in ('accepted','shipped')) or (status=='cancelled' and o['status'] in ('pending','accepted'))
                if not allowed:raise MarketError('Цей перехід статусу недоступний.',409)
                ttn=o['ttn']
                if status=='shipped':
                    ttn=text(data.get('ttn',ttn),13,14)
                    if not re.fullmatch(r'\d{14}' if o['carrier']=='nova' else r'\d{13}',ttn):raise MarketError('Перевірте номер накладної.')
                DB.execute('update market_orders set status=?,ttn=? where id=?',(status,ttn,ident))
                if status in ('cancelled','completed'):
                    DB.execute("update market_listings set status=? where id=? and status='reserved'",('active' if status=='cancelled' else 'sold',o['listing_id']))
                return {'item':order(ident,uid)}
        if method=='GET':return {'items':[order(r['id'],uid) for r in q('select id from market_orders where (buyer_id=? or seller_id=?) and price>0 order by id desc limit 100',(uid,uid))]}
        if method=='POST':
            key=text(data.get('request_id'),8,100)
            lid=integer(data.get('listing_id'))
            prior=one('select * from market_orders where buyer_id=? and request_id=?',(uid,key))
            if prior:
                if prior['listing_id']!=lid:raise MarketError('Ключ запиту вже використано.',409)
                return {'item':order(prior['id'],uid)}
            l=listing(lid,uid)
            if l['seller_id']==uid:raise MarketError('Не можна купити власний товар.')
            if l['status']!='active':raise MarketError('Товар уже зарезервовано.',409)
            if blocked(uid,l['seller_id']):raise MarketError('Угода з цим користувачем недоступна.',403)
            carrier,payment=data.get('carrier'),data.get('payment','cod')
            if carrier not in ('nova','ukr','pickup') or payment!=('pickup' if carrier=='pickup' else 'cod'):raise MarketError('Оберіть післяплату або оплату при зустрічі.')
            rec=data.get('recipient')
            if not isinstance(rec,dict):raise MarketError('Заповніть дані отримувача.')
            recipient={'name':text(rec.get('name'),3,120),'phone':text(rec.get('phone'),10,16),'city':text(rec.get('city'),2,100),'branch':text(rec.get('branch',''),0 if carrier=='pickup' else 1,160),
                       'city_ref':text(rec.get('city_ref',''),0,80),'warehouse_ref':text(rec.get('warehouse_ref',''),0,80)}
            if not re.fullmatch(r'\+?380\d{9}',recipient['phone']):raise MarketError('Телефон: +380 і 9 цифр.')
            oid=DB.execute('insert into market_orders(listing_id,buyer_id,seller_id,request_id,price,carrier,payment,recipient) values(?,?,?,?,?,?,?,?)',(lid,uid,l['seller_id'],key,l['price'],carrier,payment,json.dumps(recipient,ensure_ascii=False))).lastrowid
            DB.execute("update market_listings set status='reserved' where id=?",(lid,))
            return {'item':order(oid,uid)}
    if kind=='reports' and method=='POST':
        lid=integer(data.get('listing_id'));listing(lid,uid)
        if one("select 1 from market_reports where user_id=? and listing_id=? and status='open'",(uid,lid)):return {'ok':True}
        DB.execute('insert into market_reports(user_id,listing_id,reason) values(?,?,?)',(uid,lid,text(data.get('reason'),5,2000)))
        return {'ok':True}
    if kind=='blocks' and method=='POST':
        subject=integer(data.get('user_id'))
        if subject==uid or not one('select 1 from users where id=?',(subject,)):raise MarketError('Некоректний користувач.')
        DB.execute('insert or ignore into market_blocks values(?,?)',(uid,subject))
        return {'ok':True}
    if kind=='moderate':
        if not admin:raise MarketError('Доступ лише для адміністратора.',403)
        if method=='GET':
            return {'items':q("select r.*,l.title,l.seller_id from market_reports r join market_listings l on l.id=r.listing_id where r.status='open' order by r.id desc limit 100"),
                    'pending':q("select id,title,seller_id,created from market_listings where status='pending' order by id limit 100")}
        if method=='POST':
            lid=integer(data.get('listing_id'))
            if not one('select 1 from market_listings where id=?',(lid,)):raise MarketError('Оголошення не знайдено.',404)
            status=data.get('status')
            if status not in ('active','blocked'):raise MarketError('Оберіть active або blocked.')
            current=one('select status from market_listings where id=?',(lid,))['status']
            if current in ('reserved','sold') and status=='active':raise MarketError('Товар уже в угоді.',409)
            DB.execute('update market_listings set status=? where id=?',(status,lid))
            DB.execute("update market_reports set status='resolved' where listing_id=?",(lid,))
            return {'ok':True}
    raise MarketError('Невідомий маршрут.',404)


async def dispatch(uid, path, data=None, method='GET'):
    if DB is None:raise MarketError('Маркет ще не запущено.',503)
    data=data or {}
    if not isinstance(data,dict):raise MarketError('Некоректний запит.')
    async with LOCK:
        try:
            if method!='GET':DB.execute('begin immediate')
            result=_dispatch(uid,path,data,method)
            if method!='GET':DB.commit()
        except sqlite3.IntegrityError:
            DB.rollback()
            raise MarketError('Цю дію вже виконано або товар зарезервовано.',409)
        except Exception:
            DB.rollback()
            raise
    if method == 'PATCH' and re.fullmatch(r'/orders/\d+', path) and data.get('status') == 'accepted':
        item = result.get('item', {})
        if item.get('seller_id') == uid and item.get('carrier') == 'nova':
            result = await attempt_delivery(uid, item['id'], data.get('parcel'))
    # Notify only after committing; Telegram delivery must never roll back a sale.
    tell=getattr(B,'tell',None)
    if tell:
        try:
            item=result.get('item',{})
            if method=='POST' and re.fullmatch(r'/chats/\d+/messages',path):
                async with LOCK:
                    c=chat(int(path.split('/')[2]),uid)
                peer=c['seller_id'] if uid==c['buyer_id'] else c['buyer_id']
                await tell(peer,'<b>Нове повідомлення у Маркеті</b>\n'+html.escape(item['body']),[[('Відповісти',f"mk:openchat:{c['id']}")]])
            elif method in ('POST','PATCH') and (path=='/orders' or re.fullmatch(r'/orders/\d+',path)):
                peer=item['seller_id'] if uid==item['buyer_id'] else item['buyer_id']
                await tell(peer,f"<b>Замовлення Маркету №{item['id']}</b>\n"+html.escape(item['title'])+'\nСтатус: '+html.escape(item['status']),[[('Замовлення','mk:orders')]])
        except Exception:
            logging.warning('Marketplace notification delivery failed')
    return result


async def handle(request, uid):
    from aiohttp import web
    try:
        if request.path.startswith('/api/market/photo/'):
            return await photo_response(request,uid)
        if request.path == '/api/market/upload' and request.method=='POST':
            return web.json_response(await upload(request,uid))
        data=dict(request.query) if request.method=='GET' else await request.json()
        path=request.path.removeprefix('/api/market')
        if path.startswith('/delivery/'):
            result=await delivery(uid,path,data,request.method)
        else:
            result=await dispatch(uid,path,data,request.method)
        return web.json_response(result)
    except MarketError as e:
        return web.json_response({'error':e.message,'code':'market'},status=e.status)
    except (ValueError,TypeError):
        return web.json_response({'error':'Некоректний запит.','code':'market'},status=400)


async def register_upload(uid, fid):
    """Only trusted bot upload paths may register Telegram photo IDs."""
    async with LOCK:
        DB.execute('insert or ignore into market_uploads values(?,?)',(uid,fid))
        DB.commit()


async def upload(request,uid):
    from aiogram.types import BufferedInputFile
    import webapp
    async with LOCK:
        if not one('select 1 from users where id=? and banned=0',(uid,)):
            raise MarketError('Обліковий запис недоступний.',403)
    if not request.content_type.startswith('multipart/'):
        raise MarketError('Оберіть файл зображення.')
    reader=await request.multipart()
    out=[]
    async for part in reader:
        if not part.filename:continue
        if len(out)>=10:raise MarketError('Не більше 10 фото.')
        buf=bytearray()
        while True:
            chunk=await part.read_chunk()
            if not chunk:break
            buf.extend(chunk)
            if len(buf)>8*1024*1024:raise MarketError('Фото завелике: максимум 8 МБ.')
        data=webapp.to_jpeg(bytes(buf))
        if not data:raise MarketError('Оберіть JPG, PNG або WEBP розміром від 300 пікселів.')
        message=await B.bot.send_photo(uid,BufferedInputFile(data,filename='market.jpg'))
        fid=message.photo[-1].file_id
        await register_upload(uid,fid)
        out.append(fid)
        try:await B.bot.delete_message(uid,message.message_id)
        except Exception:pass
    if not out:raise MarketError('Не знайдено зображень.')
    return {'photos':out}


async def photo_response(request,uid):
    from aiohttp import web
    import webapp
    parts=request.path.strip('/').split('/')
    if len(parts)!=5:raise MarketError('Фото не знайдено.',404)
    lid,index=integer(parts[3]),integer(parts[4],0,9)
    async with LOCK:
        l=listing(lid,uid)
        if not uid and l['status']!='active':raise MarketError('Фото недоступне.',404)
        if index>=len(l['photos']):raise MarketError('Фото не знайдено.',404)
        source=l['photos'][index]
    if source.startswith('https://'):
        image,_,_=await webapp.safe_get(source,8*1024*1024,True)
    else:
        f=await B.bot.get_file(source)
        stream=await B.bot.download_file(f.file_path)
        image=stream.read()
    data=webapp.to_jpeg(image)
    if not data:raise MarketError('Фото недоступне.',404)
    return web.Response(body=data,content_type='image/jpeg',headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})


async def attempt_delivery(uid, oid, parcel=None):
    """Single persisted attempt shared by bot, Mini App, and bundled orders."""
    import shipping
    async with LOCK:
        o = order(integer(oid), uid)
        if o['seller_id'] != uid:
            raise MarketError('Лише продавець підтверджує відправлення.', 403)
        scope = cart.bundle_scope(DB, o['id'])
        if scope and scope['primary_order_id'] != o['id']:
            raise MarketError('Оберіть основне замовлення комплекту.', 409)
        ids = scope['order_ids'] if scope else [o['id']]
        if o['ttn']:
            return {'item': o}
        if o['shipping_state'] in ('creating', 'reconciliation_required'):
            raise MarketError('Перевірте попередню спробу у перевізника. Повторне створення заблоковано.', 409)
        if o['status'] != 'accepted' or o['carrier'] != 'nova':
            raise MarketError('Потрібне підтверджене замовлення Новою поштою.', 409)
        row = one('select body from market_sender_profiles where seller_id=?', (uid,))
        sender = json.loads(row['body']) if row else None
        # Parcel dimensions must describe the actual packed bundle, not one item.
        o['parcel'] = parcel if isinstance(parcel, dict) and parcel else ({} if scope else o.get('parcel', {}))
        marks = ','.join('?' for _ in ids)
        DB.execute('update market_orders set shipping_state=?,shipping_error=? where id in ('+marks+')', ('creating','',*ids))
        DB.commit()
    try:
        result = await delivery_service.auto_create(uid, o, sender)
    except BaseException as e:
        uncertain = not isinstance(e, shipping.ShippingError) or e.uncertain
        state = 'reconciliation_required' if uncertain else 'awaiting_delivery_details'
        async with LOCK:
            DB.execute('update market_orders set shipping_state=?,shipping_error=? where id in ('+marks+')',
                       (state, getattr(e, 'code', 'shipping_uncertain'), *ids)); DB.commit()
        if isinstance(e, shipping.ShippingError):
            return {'item': order(o['id'],uid), 'delivery': {'state':state,'message':str(e)}}
        raise
    async with LOCK:
        DB.execute('update market_orders set shipping_state=?,shipping_error=?,ttn=? where id in ('+marks+')',
                   (result['state'],result.get('code',''),result.get('ttn',''),*ids)); DB.commit()
        return {'item':order(o['id'],uid),'delivery':result}


async def delivery(uid, path, data=None, method='GET'):
    import shipping
    data = data or {}
    if not isinstance(data, dict):
        raise MarketError('Некоректний запит.')
    async with LOCK:
        if not one('select 1 from users where id=? and banned=0',(uid,)):
            raise MarketError('Обліковий запис недоступний.',403)
    try:
        if path == '/delivery/settings':
            if method == 'GET':
                async with LOCK:
                    row = one('select body from market_sender_profiles where seller_id=?',(uid,))
                return {'item':{'sender':json.loads(row['body']) if row else {},'connected':bool(row),'mode':delivery_service.delivery_mode()}}
            if method == 'DELETE':
                async with LOCK:
                    DB.execute('delete from market_sender_profiles where seller_id=?',(uid,)); DB.commit()
                return {'ok':True}
            if method == 'POST':
                if data.get('api_key') or data.get('token'):
                    raise MarketError('Продавцю не потрібно вводити API-ключ.')
                profile = delivery_service.normalize_sender_profile(data.get('sender',data))
                if delivery_service.delivery_mode() != 'central_not_configured':
                    await delivery_service.validate_sender_directory(profile)
                async with LOCK:
                    DB.execute('insert into market_sender_profiles values(?,?) on conflict(seller_id) do update set body=excluded.body',
                               (uid,json.dumps(profile,ensure_ascii=False))); DB.commit()
                return {'ok':True,'mode':delivery_service.delivery_mode()}
        if path in ('/delivery/cities','/delivery/warehouses') and method in ('GET','POST'):
            if data.get('api_key'):
                raise MarketError('Ключ перевізника налаштовує лише оператор сервісу.')
            client = delivery_service.central_client()
            rows = await client.cities(text(data.get('q',''),0,80)) if path.endswith('/cities') else await client.warehouses(text(data.get('city_ref'),1,80),text(data.get('q',''),0,80))
            return {'items':rows}
        if path == '/delivery/waybill' and method == 'POST':
            return await attempt_delivery(uid,integer(data.get('order_id')),data.get('parcel'))
        if path == '/delivery/track' and method == 'GET':
            async with LOCK:
                o = order(integer(data.get('order_id')),uid)
            if not o['ttn'] or o['carrier'] != 'nova':
                raise MarketError('Накладну Нової пошти ще не додано.',409)
            return {'item':await delivery_service.central_client().track(o['ttn'],o['recipient']['phone'])}
        if path == '/delivery/reconcile' and method == 'POST':
            ttn = text(data.get('ttn'),14,14)
            if not re.fullmatch(r'\d{14}',ttn) or data.get('confirmed_in_carrier_account') is not True:
                raise MarketError('Перевірте накладну у перевізника та підтвердьте її номер.')
            async with LOCK:
                o = order(integer(data.get('order_id')),uid)
                if o['seller_id'] != uid or o['carrier'] != 'nova' or o['shipping_state'] not in ('creating','reconciliation_required'):
                    raise MarketError('Уточнення накладної недоступне.',409)
                scope = cart.bundle_scope(DB,o['id'])
                if scope and scope['primary_order_id'] != o['id']:
                    raise MarketError('Оберіть основне замовлення комплекту.',409)
                ids = scope['order_ids'] if scope else [o['id']]
            tracking = await delivery_service.central_client().track(ttn,o['recipient']['phone'])
            if str(tracking.get('status_code')) in ('','2','3'):
                raise MarketError('Перевізник не підтвердив чинну накладну.',409)
            async with LOCK:
                marks = ','.join('?' for _ in ids)
                DB.execute('update market_orders set ttn=?,shipping_state=?,shipping_error=? where id in ('+marks+')',(ttn,'created','',*ids)); DB.commit()
                return {'item':order(o['id'],uid),'tracking':tracking}
        raise MarketError('Невідомий маршрут доставки.',404)
    except shipping.ShippingError as e:
        raise MarketError(str(e),503 if e.uncertain else 400)
