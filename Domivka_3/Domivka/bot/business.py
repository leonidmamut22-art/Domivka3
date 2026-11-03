"""Business operations, moderation and non-cash loyalty on the existing database.

Points buy platform perks only. GMV is completed order value, never platform
revenue. Referral rewards require reconciled real orders, not invitations or
self-reported shipment/payment. Owner identity comes from environment at setup.
"""
import asyncio
import datetime as dt
import json
import os
import re
import secrets
import sqlite3
from urllib.parse import urlparse

import db

B = None
DB = None
OWNER = None
IMMUTABLE_ADMINS = frozenset()
LOCK = asyncio.Lock()
DAILY_POINTS = 5
REFERRAL_POINTS = 50
SCHEMA = """
create table if not exists biz_delegates(user_id integer primary key,role text not null,by_user integer not null,created text default(datetime('now')));
create table if not exists biz_audit(id integer primary key autoincrement,actor integer not null,action text not null,target text not null,details text not null,created text default(datetime('now')));
create table if not exists biz_points(id integer primary key autoincrement,user_id integer not null,kind text not null,ref text not null,points integer not null,created text default(datetime('now')),unique(user_id,kind,ref));
create table if not exists biz_members(user_id integer primary key,code text not null unique,referrer integer,attributed_at text,last_daily text default '',streak integer default 0,created text default(datetime('now')));
create table if not exists biz_referral_rewards(referred integer primary key,referrer integer not null,service text not null,order_id integer not null,created text default(datetime('now')));
create table if not exists biz_qualified_orders(service text not null,order_id integer not null,buyer_id integer not null,seller_id integer not null,reference text not null,by_user integer not null,created text default(datetime('now')),primary key(service,order_id));
create table if not exists biz_offers(id integer primary key autoincrement,title text not null,body text not null,kind text not null,point_cost integer not null default 0,price_uah integer not null default 0,price_stars integer not null default 0,duration_days integer not null default 7,active integer not null default 1,min_level integer not null default 0,vip_only integer not null default 0,target_user_id integer not null default 0,created text default(datetime('now')));
create table if not exists biz_perks(id integer primary key autoincrement,user_id integer not null,offer_id integer not null,kind text not null,title text not null,until text not null,request_id text not null,created text default(datetime('now')),unique(user_id,request_id));
create table if not exists biz_alerts(id integer primary key autoincrement,user_id integer not null,service text not null,q text not null default '',city text not null default '',max_price integer not null default 0,created text default(datetime('now')));
create table if not exists biz_seen_prices(user_id integer not null,service text not null,listing_id integer not null,price integer not null,seen_at text default(datetime('now')),primary key(user_id,service,listing_id));
create table if not exists biz_campaigns(id integer primary key autoincrement,title text not null,body text not null,service text not null,image_url text not null default '',target_url text not null,starts_at text not null,ends_at text not null,active integer not null default 1,max_views integer not null default 1000,daily_cap integer not null default 1,booked_amount integer not null default 0,created text default(datetime('now')));
create table if not exists biz_ad_exposures(id integer primary key autoincrement,campaign_id integer not null,user_id integer not null,event text not null,day text not null,request_id text not null,created text default(datetime('now')),unique(campaign_id,user_id,event,day,request_id));
create table if not exists biz_receipts(id integer primary key autoincrement,campaign_id integer not null,amount integer not null,reference text not null unique,by_user integer not null,created text default(datetime('now')));
create index if not exists biz_points_user on biz_points(user_id);
create index if not exists biz_campaign_events on biz_ad_exposures(campaign_id,event,day);
"""


class BusinessError(Exception):
    def __init__(self,message,status=400):
        super().__init__(message)
        self.message,self.status=message,status


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None,microsecond=0).isoformat(sep=' ')


def today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def txt(value,lo=0,hi=2000):
    if not isinstance(value,str):raise BusinessError('Очікується текст.')
    value=value.strip()
    if not lo<=len(value)<=hi or any(ord(c)<32 and c not in '\n\t' for c in value):raise BusinessError('Перевірте довжину та вміст тексту.')
    return value


def num(value,lo=1,hi=2**63-1):
    if isinstance(value,bool) or not re.fullmatch(r'\d+',str(value)):raise BusinessError('Вкажіть коректне ціле число.')
    result=int(value)
    if not lo<=result<=hi:raise BusinessError('Число поза дозволеним діапазоном.')
    return result


def flag(value):
    if not isinstance(value,bool):raise BusinessError('Вкажіть true або false.')
    return int(value)


def q(sql,args=()):
    return [dict(r) for r in DB.execute(sql,args).fetchall()]


def one(sql,args=()):
    rows=q(sql,args)
    return rows[0] if rows else None


def exists(table):
    return bool(one("select 1 from sqlite_master where type='table' and name=?",(table,)))


async def setup(module):
    global B,DB,OWNER,IMMUTABLE_ADMINS
    B=module
    configured=[int(x) for x in re.findall(r'\d+',os.environ.get('ADMIN_IDS',''))]
    if not configured:configured=sorted(getattr(B,'ADMINS',set()))
    owner_env=os.environ.get('OWNER_ID','').strip()
    OWNER=num(owner_env) if owner_env else (configured[0] if configured else None)
    IMMUTABLE_ADMINS=frozenset(configured+([OWNER] if OWNER else []))
    rows=await db.q('pragma database_list')
    path=next((r['file'] for r in rows if r['name']=='main'),'')
    if not path:raise RuntimeError('Business requires a file-backed database')
    if DB is not None:DB.close()
    DB=sqlite3.connect(path,timeout=3)
    DB.row_factory=sqlite3.Row
    DB.executescript(SCHEMA)
    if 'price_stars' not in {r['name'] for r in DB.execute('pragma table_info(biz_offers)')}:
        DB.execute('alter table biz_offers add column price_stars integer not null default 0')
    offer_cols={r['name'] for r in DB.execute('pragma table_info(biz_offers)')}
    for col in ('min_level','vip_only','target_user_id'):
        if col not in offer_cols:DB.execute('alter table biz_offers add column '+col+' integer not null default 0')
    if not one('select 1 from biz_offers limit 1'):
        DB.execute('insert into biz_offers(title,body,kind,point_cost,duration_days) values(?,?,?,?,?)',('Ще 5 збережених пошуків','Пʼять додаткових пошуків на 7 днів. Бали не є грошима.','alerts_extra',40,7))
        DB.execute('insert into biz_offers(title,body,kind,point_cost,duration_days) values(?,?,?,?,?)',('Акцент профілю','Клубний акцент профілю на 7 днів. Не є перевіркою особи.','profile_accent',30,7))
    DB.commit()


def role(uid):
    if uid==OWNER:return 'owner'
    if uid in IMMUTABLE_ADMINS:return 'admin'
    r=one('select role from biz_delegates where user_id=?',(uid,)) if DB else None
    return r['role'] if r else None


def require(uid,scope):
    r=role(uid)
    if r=='owner':return r
    if scope=='owner':raise BusinessError('Доступ лише для власника.',403)
    if r=='admin':return r
    if scope=='moderation' and r=='moderator':return r
    if scope=='marketing' and r=='marketing':return r
    if scope=='read_admin' and r in ('moderator','marketing'):return r
    raise BusinessError('Недостатньо прав.',403)


def audit(uid,action,target,details):
    DB.execute('insert into biz_audit(actor,action,target,details) values(?,?,?,?)',(uid,action,str(target),json.dumps(details,ensure_ascii=False,sort_keys=True)))


def member(uid):
    m=one('select * from biz_members where user_id=?',(uid,))
    if not m:
        for _ in range(10):
            try:
                DB.execute('insert into biz_members(user_id,code) values(?,?)',(uid,secrets.token_urlsafe(8)))
                break
            except sqlite3.IntegrityError:
                if one('select 1 from biz_members where user_id=?',(uid,)):break
        m=one('select * from biz_members where user_id=?',(uid,))
    return m


def balance(uid):
    return one('select coalesce(sum(points),0) n from biz_points where user_id=?',(uid,))['n']


def award(uid,kind,reference,amount):
    cur=DB.execute('insert or ignore into biz_points(user_id,kind,ref,points) values(?,?,?,?)',(uid,kind,str(reference),amount))
    return bool(cur.rowcount)


def synchronize_referrals(uid):
    candidates=q('select m.user_id,m.referrer,m.attributed_at,o.service,o.order_id,o.created order_verified from biz_members m join biz_qualified_orders o on o.buyer_id=m.user_id join users u on u.id=m.user_id join users r on r.id=m.referrer where m.referrer=? and u.banned=0 and r.banned=0 and o.seller_id!=m.referrer and o.created>=m.attributed_at and not exists(select 1 from biz_referral_rewards x where x.referred=m.user_id) order by o.created,m.user_id',(uid,))
    for c in candidates:
        if one("select count(*) n from biz_referral_rewards where referrer=? and date(created)=?",(uid,today()))['n']>=10:break
        if one('select count(*) n from biz_referral_rewards where referrer=?',(uid,))['n']>=100:break
        if one('select 1 from biz_referral_rewards where referred=?',(c['user_id'],)):continue
        DB.execute('insert into biz_referral_rewards(referred,referrer,service,order_id) values(?,?,?,?)',(c['user_id'],uid,c['service'],c['order_id']))
        award(uid,'referral',c['user_id'],REFERRAL_POINTS)


def profile(uid):
    m=member(uid)
    synchronize_referrals(uid)
    points=balance(uid)
    earned=one('select coalesce(sum(points),0) n from biz_points where user_id=? and points>0',(uid,))['n']
    level=1+earned//100
    return {'points':points,'level':level,'level_name':('Новачок' if level==1 else 'Дослідник' if level<5 else 'Сусід'),'streak':m['streak'],
            'referral_code':m['code'],'referrer':m['referrer'],'qualified_referrals':one('select count(*) n from biz_referral_rewards where referrer=?',(uid,))['n'],
            'perks':q('select id,kind,title,until from biz_perks where user_id=? and until>? order by until',(uid,now())),
            'daily_claimed':m['last_daily']==today(),'admin_role':role(uid),'points_are_money':False,
            'referral_rule':'Бонус після першої завершеної реальної угоди, підтвердженої адміністрацією. Не за запрошення. Максимум 10 на день і 100 загалом.',
            'daily_points':DAILY_POINTS,'referral_points':REFERRAL_POINTS,'timezone':'UTC'}


def eligible_offer(uid,offer):
    """Server-side audience check; billing snapshots remain valid after purchase."""
    if DB is None or not offer:return False
    offer=dict(offer)
    if offer.get('target_user_id',0) not in (0,uid):return False
    earned=one('select coalesce(sum(points),0) n from biz_points where user_id=? and points>0',(uid,))['n']
    if 1+earned//100<offer.get('min_level',0):return False
    if offer.get('vip_only') and not one("select 1 from biz_perks where user_id=? and kind='vip' and until>?",(uid,now())):return False
    return True


def parse_time(value):
    value=txt(value,10,40)
    try:
        parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo:parsed=parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed.replace(microsecond=0).isoformat(sep=' ')
    except ValueError:raise BusinessError('Вкажіть дату й час у форматі ISO, UTC.')


def url(value,local=False,empty=False):
    value=txt(value,0 if empty else 1,1000)
    if not value and empty:return ''
    if local and value in ('/market','/rentals','/offers','/club'):return value
    p=urlparse(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise BusinessError('Посилання має використовувати HTTPS.')
    return value


def campaign_row(row):
    if not row:return None
    out=dict(row)
    out['active']=bool(out['active']);out['sponsored']=True;out['label']='Реклама';out['impression_id']=secrets.token_urlsafe(12)
    return out


def view_allowed(c,uid):
    if not c['active'] or not c['starts_at']<=now()<c['ends_at']:return False
    count=one("select count(*) n from biz_ad_exposures where campaign_id=? and event='view'",(c['id'],))['n']
    if count>=c['max_views']:return False
    daily=one("select count(*) n from biz_ad_exposures where campaign_id=? and user_id=? and event='view' and day=?",(c['id'],uid,today()))['n']
    return daily<c['daily_cap']


def alert_cap(uid):
    extra=bool(one("select 1 from biz_perks where user_id=? and kind='alerts_extra' and until>?",(uid,now())))
    vip=bool(one("select 1 from biz_perks where user_id=? and kind='vip' and until>?",(uid,now())))
    return 25 if vip else 3+5*extra


def feed(uid):
    alerts=q('select * from biz_alerts where user_id=? order by id',(uid,))
    cap=alert_cap(uid)
    alerts=[{**a,'active':i<cap} for i,a in enumerate(alerts)]
    items=[]
    seen=set()
    def add(service,r,reason):
        key=(service,r['id'])
        if key in seen:return
        seen.add(key)
        price=r['price']
        prior=one('select price from biz_seen_prices where user_id=? and service=? and listing_id=?',(uid,service,r['id']))
        if prior and price<prior['price']:reason='favorite_price_drop' if r.get('favorite') else 'price_drop'
        items.append({'service':service,'id':r['id'],'title':r['title'],'city':r['city'],'price':price,'photos':json.loads(r.get('photos') or '[]'),'reason':reason,'url':('/market' if service=='market' else '/rentals'),'price_unit':('item' if service=='market' else 'night'),'created':r['created']})
        DB.execute('insert into biz_seen_prices(user_id,service,listing_id,price) values(?,?,?,?) on conflict(user_id,service,listing_id) do update set price=excluded.price,seen_at=datetime(\'now\')',(uid,service,r['id'],price))
    for service in ('market','rentals'):
        if service=='market' and not exists('market_listings'):continue
        if service=='market':
            rows=q("select l.id,l.title,l.city,l.price,l.photos,l.created,(select 1 from market_favorites f where f.user_id=? and f.listing_id=l.id) favorite from market_listings l join users u on u.id=l.seller_id where l.status='active' and u.banned=0 order by l.id desc limit 200",(uid,))
        else:
            rows=q('select l.id,l.title,l.city,l.price_night price,l.photos,l.created,(select 1 from favs f where f.user=? and f.listing_id=l.id) favorite from listings l join users u on u.id=l.owner where l.active=1 and l.blocked=0 and u.banned=0 order by l.id desc limit 200',(uid,))
        for r in rows:
            if len(items)>=60:break
            matching=any(a['active'] and a['service']==service and (not a['city'] or a['city'].casefold()==r['city'].casefold()) and (not a['q'] or a['q'].casefold() in r['title'].casefold()) and (not a['max_price'] or r['price']<=a['max_price']) for a in alerts)
            if matching:add(service,r,'saved_search')
            elif r['favorite']:add(service,r,'favorite')
            elif not alerts and r['created'][:10]==today():add(service,r,'new_today')
    return {'items':items,'alerts':alerts,'generated_at':now(),'note':'Рекомендації за збереженими пошуками й обраним. Зниження ціни показуємо лише після попереднього перегляду.'}


def metrics():
    market_stats={'completed_orders':0,'completed_gmv':0,'active_listings':0}
    if exists('market_orders'):
        r=one("select count(*) n,coalesce(sum(price),0) total from market_orders where status='completed' and price>0")
        market_stats={'completed_orders':r['n'],'completed_gmv':r['total'],'active_listings':one("select count(*) n from market_listings where status='active'")['n']}
    rentals=one("select count(*) n,coalesce(sum(total),0) total from bookings where status='confirmed' and co<=date('now')")
    stars={'currency':'XTR','gross_paid':0,'settled':0,'refunded':0,'reconciliation_required':0}
    if exists('biz_star_receipts'):
        for item in q("select status,coalesce(sum(amount),0) amount from biz_star_receipts where currency='XTR' group by status"):
            if item['status'] in ('settled','refunded','reconciliation_required'):stars[item['status']]=item['amount']
        stars['gross_paid']=stars['settled']+stars['refunded']
    return {'users':one('select count(*) n from users')['n'],'market':market_stats,
            'rentals':{'completed_stays':rentals['n'],'completed_gmv':rentals['total'],'active_listings':one('select count(*) n from listings where active=1 and blocked=0')['n']},
            'advertising':{'booked_amount':one('select coalesce(sum(booked_amount),0) n from biz_campaigns')['n'],'reconciled_receipts':one('select coalesce(sum(amount),0) n from biz_receipts')['n'],'campaigns':one('select count(*) n from biz_campaigns')['n']},
            'stars':stars,'loyalty':{'daily_claims':one("select count(*) n from biz_points where kind='daily'")['n'],'qualified_referrals':one('select count(*) n from biz_referral_rewards')['n']},
            'payment_enabled':bool(one('select 1 from biz_offers where active=1 and price_stars>0')),'physical_payment_enabled':False,'gmv_is_revenue':False,'currency':'UAH','note':'GMV — вартість завершених угод за статусами, не дохід платформи й не підтвердження платежу. Надходження реклами — лише окремо звірені записи.'}


def dispatch_inner(uid,path,data,method):
    user=one('select id,banned,created from users where id=?',(uid,))
    if not user or user['banned']:raise BusinessError('Обліковий запис недоступний.',403)
    member(uid)
    if path=='/profile' and method=='GET':return {'item':profile(uid)}
    if path=='/daily' and method=='POST':
        m=member(uid);day=today();awarded=0
        if m['last_daily']!=day:
            yesterday=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat()
            streak=m['streak']+1 if m['last_daily']==yesterday else 1
            awarded=DAILY_POINTS if award(uid,'daily',day,DAILY_POINTS) else 0
            DB.execute('update biz_members set last_daily=?,streak=? where user_id=?',(day,streak,uid))
        return {'item':profile(uid),'awarded_today':awarded}
    if path=='/referral' and method=='POST':
        code=txt(data.get('code'),4,80)
        ref=one('select user_id from biz_members where code=?',(code,))
        if not ref:raise BusinessError('Код запрошення не знайдено.',404)
        if ref['user_id']==uid:raise BusinessError('Не можна запросити себе.')
        m=member(uid)
        if m['referrer'] is not None:
            if m['referrer']==ref['user_id']:return {'item':profile(uid)}
            raise BusinessError('Запрошення вже закріплено за іншим користувачем.',409)
        if user['created'] < (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=7)).replace(tzinfo=None).isoformat(sep=' ',timespec='seconds'):
            raise BusinessError('Запрошення можна прийняти лише протягом перших 7 днів після реєстрації.',409)
        if exists('market_orders') and one('select 1 from market_orders where buyer_id=? limit 1',(uid,)):
            raise BusinessError('Запрошення приймають до першої покупки.',409)
        if one('select 1 from bookings where guest=? limit 1',(uid,)):raise BusinessError('Запрошення приймають до першого бронювання.',409)
        if one('select banned from users where id=?',(ref['user_id'],))['banned']:raise BusinessError('Запрошення недоступне.')
        # Reject attribution cycles, including reciprocal invitations.
        cursor=ref['user_id'];visited={uid}
        for _ in range(100):
            if cursor in visited:raise BusinessError('Циклічне запрошення недоступне.')
            visited.add(cursor)
            parent=one('select referrer from biz_members where user_id=?',(cursor,))
            if not parent or parent['referrer'] is None:break
            cursor=parent['referrer']
        else:raise BusinessError('Ланцюжок запрошень завеликий.')
        DB.execute('update biz_members set referrer=?,attributed_at=? where user_id=? and referrer is null',(ref['user_id'],now(),uid))
        return {'item':profile(uid)}
    if path=='/offers' and method=='GET':
        rows=[r for r in q('select * from biz_offers where active=1 order by id') if eligible_offer(uid,r)]
        return {'items':[{**r,'active':bool(r['active']),'vip_only':bool(r['vip_only']),'payment_enabled':r['price_stars']>0,'cash_value':None} for r in rows],'payment_enabled':any(r['price_stars']>0 for r in rows)}
    if path=='/redeem' and method=='POST':
        oid=num(data.get('offer_id'));key=txt(data.get('request_id'),8,100)
        prior=one('select * from biz_perks where user_id=? and request_id=?',(uid,key))
        if prior:
            if prior['offer_id']!=oid:raise BusinessError('Ключ запиту вже використано.',409)
            return {'item':prior}
        offer=one('select * from biz_offers where id=? and active=1',(oid,))
        if not offer or offer['kind']=='plan' or offer['price_stars']>0 or offer['point_cost']<1:raise BusinessError('Ця пропозиція не обмінюється на бали.',409)
        if not eligible_offer(uid,offer):raise BusinessError('Ця пропозиція недоступна для вашого профілю.',403)
        if balance(uid)<offer['point_cost']:raise BusinessError('Недостатньо балів.',409)
        if one('select 1 from biz_perks where user_id=? and kind=? and until>?',(uid,offer['kind'],now())):raise BusinessError('Ця перевага вже активна.',409)
        until=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=offer['duration_days'])).replace(tzinfo=None,microsecond=0).isoformat(sep=' ')
        pid=DB.execute('insert into biz_perks(user_id,offer_id,kind,title,until,request_id) values(?,?,?,?,?,?)',(uid,oid,offer['kind'],offer['title'],until,key)).lastrowid
        award(uid,'redeem',pid,-offer['point_cost'])
        return {'item':one('select * from biz_perks where id=?',(pid,))}
    if path=='/alerts' and method=='GET':return {'items':q('select * from biz_alerts where user_id=? order by id desc',(uid,))}
    if path=='/alerts' and method=='POST':
        service=data.get('service')
        if service not in ('market','rentals'):raise BusinessError('Оберіть сервіс.')
        query,city=txt(data.get('q',''),0,100),txt(data.get('city',''),0,100)
        maximum=num(data.get('max_price',0),0,100000000)
        if not query and not city and not maximum:raise BusinessError('Додайте місто, пошуковий запит або максимальну ціну.')
        cap=alert_cap(uid)
        if one('select count(*) n from biz_alerts where user_id=?',(uid,))['n']>=cap:raise BusinessError('Досягнуто ліміт збережених пошуків.',409)
        existing=one('select * from biz_alerts where user_id=? and service=? and q=? and city=? and max_price=?',(uid,service,query,city,maximum))
        if existing:return {'item':existing}
        aid=DB.execute('insert into biz_alerts(user_id,service,q,city,max_price) values(?,?,?,?,?)',(uid,service,query,city,maximum)).lastrowid
        return {'item':one('select * from biz_alerts where id=?',(aid,))}
    if path.startswith('/alerts/') and method=='DELETE':
        aid=num(path.split('/')[-1]);DB.execute('delete from biz_alerts where id=? and user_id=?',(aid,uid));return {'ok':True}
    if path=='/feed' and method=='GET':return feed(uid)
    if path=='/banners' and method=='GET':
        if one("select 1 from biz_perks where user_id=? and kind in ('ad_free','vip') and until>?",(uid,now())):return {'items':[]}
        service=data.get('service','all')
        if service not in ('market','rentals','all'):raise BusinessError('Оберіть сервіс.')
        rows=q("select * from biz_campaigns where active=1 and (service=? or service='all') order by id desc limit 100",(service,))
        return {'items':[{k:v for k,v in campaign_row(c).items() if k not in ('booked_amount',)} for c in rows if view_allowed(c,uid)][:2]}
    if path=='/banner/event' and method=='POST':
        cid=num(data.get('id'));event=data.get('event');request_id=txt(data.get('request_id','daily-default'),8,100)
        if event not in ('view','click'):raise BusinessError('Невідомий тип події.')
        c=one('select * from biz_campaigns where id=?',(cid,))
        if not c:raise BusinessError('Кампанію не знайдено.',404)
        existing=one('select 1 from biz_ad_exposures where campaign_id=? and user_id=? and event=? and day=? and request_id=?',(cid,uid,event,today(),request_id))
        if existing:return {'ok':True,'counted':False}
        if event=='view' and not view_allowed(c,uid):return {'ok':True,'counted':False}
        if event=='click' and not one("select 1 from biz_ad_exposures where campaign_id=? and user_id=? and event='view' and day=? and request_id=?",(cid,uid,today(),request_id)):raise BusinessError('Спочатку має бути показ реклами.')
        DB.execute('insert into biz_ad_exposures(campaign_id,user_id,event,day,request_id) values(?,?,?,?,?)',(cid,uid,event,today(),request_id))
        return {'ok':True,'counted':True}
    if not path.startswith('/admin/'):raise BusinessError('Невідомий маршрут.',404)
    if path=='/admin/dashboard' and method=='GET':require(uid,'read_admin');return {'item':metrics()}
    if path=='/admin/delegates':
        require(uid,'owner')
        if method=='GET':
            rows=q('select d.user_id,u.name,d.role,0 immutable from biz_delegates d join users u on u.id=d.user_id')
            rows += [{'user_id':i,'name':(one('select name from users where id=?',(i,)) or {}).get('name',''),'role':role(i),'immutable':True} for i in sorted(IMMUTABLE_ADMINS)]
            return {'items':rows,'owner_id':OWNER}
        if method=='POST':
            target=num(data.get('user_id'));new=data.get('role')
            if new not in ('moderator','marketing','none'):raise BusinessError('Доступні ролі: moderator, marketing, none.')
            if target in IMMUTABLE_ADMINS:raise BusinessError('Права власника та адміністраторів з конфігурації змінюються лише на сервері.',403)
            if not one('select 1 from users where id=? and banned=0',(target,)):raise BusinessError('Потрібен активний зареєстрований користувач.',404)
            if new=='none':DB.execute('delete from biz_delegates where user_id=?',(target,))
            else:DB.execute('insert into biz_delegates(user_id,role,by_user) values(?,?,?) on conflict(user_id) do update set role=excluded.role,by_user=excluded.by_user',(target,new,uid))
            audit(uid,'delegate',target,{'role':new});return {'ok':True}
    if path=='/admin/users' and method=='GET':
        require(uid,'moderation');needle='%'+txt(data.get('q',''),0,80)+'%'
        return {'items':q('select id,name,username,banned,role,created from users where name like ? or cast(id as text) like ? order by id desc limit 100',(needle,needle))}
    if path=='/admin/users' and method=='POST':
        require(uid,'moderation');target=num(data.get('user_id'));banned=flag(data.get('banned'));reason=txt(data.get('reason'),3,1000)
        if role(target):raise BusinessError('Керування доступом адміністрації виконує власник через конфігурацію та делегування.',403)
        if not one('select 1 from users where id=?',(target,)):raise BusinessError('Користувача не знайдено.',404)
        DB.execute('update users set banned=? where id=?',(banned,target))
        audit(uid,'user_ban',target,{'banned':bool(banned),'reason':reason});return {'ok':True}
    if path=='/admin/moderation' and method=='GET':
        require(uid,'moderation')
        reports=q("select id,listing_id,reporter user_id,reason,status,'rentals' service from reports where status='open' order by id desc limit 100")
        pending=q("select id,title,owner seller_id,'rentals' service from listings where blocked=2 order by id limit 100")
        if exists('market_reports'):reports+=q("select id,listing_id,user_id,reason,status,'market' service from market_reports where status='open' order by id desc limit 100")
        if exists('market_listings'):pending+=q("select id,title,seller_id,'market' service from market_listings where status='pending' order by id limit 100")
        return {'items':reports,'pending':pending}
    if path=='/admin/moderate' and method=='POST':
        require(uid,'moderation');service=data.get('service');lid=num(data.get('listing_id'));action=data.get('action');reason=txt(data.get('reason'),3,1000)
        if service not in ('market','rentals') or action not in ('block','approve'):raise BusinessError('Оберіть сервіс та дію.')
        if service=='market':
            row=one('select status from market_listings where id=?',(lid,)) if exists('market_listings') else None
            if not row:raise BusinessError('Оголошення не знайдено.',404)
            if action=='approve' and row['status'] in ('reserved','sold','deleted'):raise BusinessError('Не можна повторно активувати товар в угоді або видалений товар.',409)
            DB.execute('update market_listings set status=? where id=?',('blocked' if action=='block' else 'active',lid))
            DB.execute("update market_reports set status='resolved' where listing_id=?",(lid,))
        else:
            if not one('select 1 from listings where id=?',(lid,)):raise BusinessError('Оголошення не знайдено.',404)
            DB.execute('update listings set blocked=? where id=?',(1 if action=='block' else 0,lid))
            DB.execute("update reports set status='resolved' where listing_id=?",(lid,))
        audit(uid,'moderate',service+':'+str(lid),{'action':action,'reason':reason});return {'ok':True}
    if path=='/admin/campaigns':
        require(uid,'marketing')
        if method=='GET':
            rows=q('select * from biz_campaigns order by id desc limit 200')
            return {'items':[{**campaign_row(r),'views':one("select count(*) n from biz_ad_exposures where campaign_id=? and event='view'",(r['id'],))['n'],'clicks':one("select count(*) n from biz_ad_exposures where campaign_id=? and event='click'",(r['id'],))['n']} for r in rows]}
        if method=='POST':
            cid=num(data['id']) if data.get('id') else None
            old=one('select * from biz_campaigns where id=?',(cid,)) if cid else {}
            if cid and not old:raise BusinessError('Кампанію не знайдено.',404)
            d={**old,**data};service=d.get('service','all')
            if service not in ('all','market','rentals'):raise BusinessError('Оберіть сервіс.')
            start,end=parse_time(d.get('starts_at')),parse_time(d.get('ends_at'))
            if end<=start:raise BusinessError('Кінець кампанії має бути після початку.')
            values=(txt(d.get('title'),3,100),txt(d.get('body',''),0,500),service,url(d.get('image_url',''),empty=True),url(d.get('target_url'),local=True),start,end,flag(data.get('active',bool(old.get('active',True)))),num(d.get('max_views',1000),1,1000000),num(d.get('daily_cap',1),1,3),num(d.get('booked_amount',0),0,100000000))
            if cid:DB.execute('update biz_campaigns set title=?,body=?,service=?,image_url=?,target_url=?,starts_at=?,ends_at=?,active=?,max_views=?,daily_cap=?,booked_amount=? where id=?',values+(cid,))
            else:cid=DB.execute('insert into biz_campaigns(title,body,service,image_url,target_url,starts_at,ends_at,active,max_views,daily_cap,booked_amount) values(?,?,?,?,?,?,?,?,?,?,?)',values).lastrowid
            audit(uid,'campaign',cid,{'active':bool(values[7]),'booked_amount':values[-1]});return {'item':campaign_row(one('select * from biz_campaigns where id=?',(cid,)))}
    if path=='/admin/offers':
        require(uid,'marketing')
        if method=='GET':return {'items':q('select * from biz_offers order by id desc'),'payment_enabled':bool(one('select 1 from biz_offers where active=1 and price_stars>0'))}
        if method=='POST':
            oid=num(data['id']) if data.get('id') else None;old=one('select * from biz_offers where id=?',(oid,)) if oid else {}
            if oid and not old:raise BusinessError('Пропозицію не знайдено.',404)
            d={**old,**data};kind=d.get('kind')
            kind={'extra_alert':'alerts_extra'}.get(kind,kind)
            if kind not in ('alerts_extra','profile_accent','ad_free','vip','plan'):raise BusinessError('Невідомий вид переваги.')
            cost=num(d.get('point_cost',0),0,1000000);price=num(d.get('price_uah',0),0,1000000);stars=num(d.get('price_stars',0),0,100000)
            if stars and (kind=='plan' or cost or price):raise BusinessError('Stars доступні лише для цифрових переваг без іншої ціни.')
            if not stars and kind!='plan' and (cost<1 or price):raise BusinessError('Оберіть бали або Stars для цифрової переваги.')
            if kind=='plan' and (cost or stars):raise BusinessError('Інформаційні грошові плани не обмінюються на бали чи Stars.')
            audience=(num(d.get('min_level',0),0,10000),flag(data.get('vip_only',bool(old.get('vip_only',False)))),num(d.get('target_user_id',0),0))
            if audience[2] and not one('select 1 from users where id=? and banned=0',(audience[2],)):raise BusinessError('Користувача для адресної пропозиції не знайдено.',404)
            values=(txt(d.get('title'),3,100),txt(d.get('body',''),0,2000),kind,cost,price,stars,num(d.get('duration_days',7),1,365),flag(data.get('active',bool(old.get('active',True)))))+audience
            if oid:DB.execute('update biz_offers set title=?,body=?,kind=?,point_cost=?,price_uah=?,price_stars=?,duration_days=?,active=?,min_level=?,vip_only=?,target_user_id=? where id=?',values+(oid,))
            else:oid=DB.execute('insert into biz_offers(title,body,kind,point_cost,price_uah,price_stars,duration_days,active,min_level,vip_only,target_user_id) values(?,?,?,?,?,?,?,?,?,?,?)',values).lastrowid
            audit(uid,'offer',oid,{'kind':kind,'point_cost':cost,'price_uah':price,'price_stars':stars,'min_level':audience[0],'vip_only':bool(audience[1]),'target_user_id':audience[2]});return {'item':one('select * from biz_offers where id=?',(oid,)),'payment_enabled':bool(stars)}
    if path=='/admin/receipts' and method=='POST':
        require(uid,'owner');cid=num(data.get('campaign_id'));amount=num(data.get('amount'),1,100000000);reference=txt(data.get('reference'),5,150)
        if data.get('confirmed') is not True:raise BusinessError('Підтвердьте фактичне надходження за випискою.')
        if not one('select 1 from biz_campaigns where id=?',(cid,)):raise BusinessError('Кампанію не знайдено.',404)
        prior=one('select * from biz_receipts where reference=?',(reference,))
        if prior:
            if prior['campaign_id']!=cid or prior['amount']!=amount:raise BusinessError('Посилання платежу вже використано.',409)
            return {'item':prior}
        rid=DB.execute('insert into biz_receipts(campaign_id,amount,reference,by_user) values(?,?,?,?)',(cid,amount,reference,uid)).lastrowid
        audit(uid,'receipt',rid,{'campaign_id':cid,'amount':amount,'reference':reference});return {'item':one('select * from biz_receipts where id=?',(rid,))}
    if path=='/admin/qualify-order' and method=='POST':
        require(uid,'owner');service=data.get('service');oid=num(data.get('order_id'));reference=txt(data.get('reference'),5,150)
        if data.get('confirmed') is not True:raise BusinessError('Підтвердьте перевірку реальної завершеної угоди.')
        if service=='market' and exists('market_orders'):
            o=one("select buyer_id,seller_id,price amount,created from market_orders where id=? and status='completed'",(oid,))
        elif service=='rentals':
            o=one("select b.guest buyer_id,l.owner seller_id,b.total amount,b.created from bookings b join listings l on l.id=b.listing_id where b.id=? and b.status='confirmed' and b.co<=date('now')",(oid,))
        else:raise BusinessError('Оберіть сервіс.')
        if not o or o['buyer_id']==o['seller_id'] or o['amount']<100:raise BusinessError('Потрібна завершена угода від 100 грн між різними користувачами.',409)
        m=member(o['buyer_id'])
        if m['attributed_at'] and o['created']<m['attributed_at']:raise BusinessError('Угоду створено до прийняття запрошення.',409)
        prior=one('select * from biz_qualified_orders where service=? and order_id=?',(service,oid))
        if prior:return {'item':prior}
        DB.execute('insert into biz_qualified_orders(service,order_id,buyer_id,seller_id,reference,by_user) values(?,?,?,?,?,?)',(service,oid,o['buyer_id'],o['seller_id'],reference,uid))
        audit(uid,'qualify_order',service+':'+str(oid),{'reference':reference});return {'item':one('select * from biz_qualified_orders where service=? and order_id=?',(service,oid))}
    if path=='/admin/audit' and method=='GET':
        require(uid,'owner');return {'items':q('select * from biz_audit order by id desc limit 200')}
    raise BusinessError('Невідомий маршрут.',404)


async def dispatch(uid,path,data=None,method='GET'):
    if DB is None:raise BusinessError('Бізнес-модуль ще не запущено.',503)
    if data is None:data={}
    if not isinstance(data,dict):raise BusinessError('Некоректний запит.')
    async with LOCK:
        try:
            # GET profile/feed can award reconciled rewards/update snapshots;
            # use the same transaction boundary as explicit mutations.
            DB.execute('begin immediate')
            result=dispatch_inner(num(uid),path,data,method.upper())
            DB.commit()
            return result
        except sqlite3.IntegrityError:
            DB.rollback();raise BusinessError('Цю дію вже виконано.',409)
        except Exception:
            DB.rollback();raise


async def handle(request,uid):
    from aiohttp import web
    try:
        data=dict(request.query) if request.method=='GET' else await request.json()
        result=await dispatch(uid,request.path.removeprefix('/api/business'),data,request.method)
        return web.json_response(result)
    except BusinessError as e:return web.json_response({'error':e.message,'code':'business'},status=e.status)
    except (ValueError,TypeError):return web.json_response({'error':'Некоректний запит.','code':'business'},status=400)
