"""One-time digital platform perks paid with Telegram Stars, never physical goods.

Official flow: https://core.telegram.org/bots/payments-stars
Payment is authoritative only on successful_payment, not browser invoiceClosed.
Stars records are separate from UAH cash receipts. This does not change the
operator's registration/tax obligations. /paysupport must be wired by the bot.
"""
import asyncio
import datetime as dt
import html
import logging
import os
import re
import sqlite3
import storage
import uuid
from urllib.parse import urlparse

import db

B = None
DB = None
LOCK = asyncio.Lock()
PREFIX = 'domivka4:'
KINDS = frozenset(('alerts_extra','profile_accent','ad_free','vip'))
SCHEMA = """
create table if not exists biz_invoices(
 id text primary key,user_id integer not null,offer_id integer not null,title text not null,body text not null,kind text not null,
 amount integer not null,duration_days integer not null,status text not null default 'issuing',precheck_id text not null default '',
 charge_id text not null default '',url text not null default '',created text not null default(datetime('now')),paid_at text default '',refund_at text default '');
create table if not exists biz_star_receipts(
 charge_id text primary key,invoice_id text,user_id integer not null,amount integer not null,currency text not null,
 status text not null,reason text not null default '',created text not null default(datetime('now')));
create index if not exists biz_star_receipts_invoice on biz_star_receipts(invoice_id);
"""


class BillingError(Exception):
    def __init__(self,message,status=400):
        super().__init__(message)
        self.message,self.status=message,status


def _one(sql,args=()):
    row=DB.execute(sql,args).fetchone()
    return dict(row) if row else None


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None,microsecond=0)


def _id(payload):
    if not isinstance(payload,str) or not payload.startswith(PREFIX):
        return None
    value=payload[len(PREFIX):]
    try:
        return str(uuid.UUID(value)) if str(uuid.UUID(value))==value else None
    except ValueError:
        return None


def support_text():
    contact=os.environ.get('PAYMENT_SUPPORT_URL','').strip()
    parsed=urlparse(contact)
    if not (parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password):
        contact='Зверніться до адміністратора через розділ підтримки у Domivka.'
    return 'Підтримка оплат Telegram Stars: '+html.escape(contact)+'\nВкажіть номер рахунку або ідентифікатор платежу. Не надсилайте дані банківської картки.'


async def setup(module):
    global B,DB
    B=module
    if DB is not None:DB.close()
    DB = storage.connect(db.storage_target())
    DB.row_factory=sqlite3.Row
    DB.executescript(SCHEMA)
    DB.commit()


async def invoice(uid,offer_id):
    if DB is None:raise BillingError('Оплата тимчасово недоступна.',503)
    if isinstance(offer_id,bool) or not re.fullmatch(r'[1-9][0-9]*',str(offer_id)):
        raise BillingError('Оберіть пропозицію.')
    async with LOCK:
        user=_one('select id from users where id=? and banned=0',(uid,))
        columns={r['name'] for r in DB.execute('pragma table_info(biz_offers)')}
        if not user:raise BillingError('Обліковий запис недоступний.',403)
        if 'price_stars' not in columns:raise BillingError('Оплату цієї пропозиції ще не підключено.',409)
        offer=_one('select * from biz_offers where id=? and active=1',(int(offer_id),))
        import business
        if offer and hasattr(business,'eligible_offer') and not business.eligible_offer(uid,offer):
            raise BillingError('Ця пропозиція недоступна для вашого профілю.',403)
        if not offer or offer['kind'] not in KINDS or not isinstance(offer['price_stars'],int) or not 1<=offer['price_stars']<=1000000:
            raise BillingError('Пропозиція не доступна для оплати Stars.',409)
        if offer.get('price_uah',0) or offer.get('point_cost',0):
            raise BillingError('Спосіб оплати пропозиції налаштований некоректно.',409)
        if not 1<=offer['duration_days']<=365:raise BillingError('Некоректний строк дії пропозиції.',409)
        if _one('select 1 from biz_perks where user_id=? and kind=? and until>?',(uid,offer['kind'],_now().isoformat(sep=' '))):
            raise BillingError('Ця перевага вже активна. Нову покупку можна зробити після завершення строку.',409)
        iid=str(uuid.uuid4())
        title=str(offer['title'])[:32]
        body=(str(offer.get('body') or '')+' Одноразова покупка на '+str(offer['duration_days'])+' дн. Без автопродовження.')[:255]
        DB.execute('insert into biz_invoices(id,user_id,offer_id,title,body,kind,amount,duration_days) values(?,?,?,?,?,?,?,?)',
                   (iid,uid,offer['id'],title,body,offer['kind'],offer['price_stars'],offer['duration_days']))
        DB.commit()
    from aiogram.types import LabeledPrice
    try:
        # Stars has exactly one price component. Provider token is not needed.
        link=await B.bot.create_invoice_link(title=title,description=body,payload=PREFIX+iid,
                 currency='XTR',prices=[LabeledPrice(label=title,amount=offer['price_stars'])])
        parsed=urlparse(link)
        if parsed.scheme!='https' or parsed.hostname!='t.me':raise ValueError('invalid invoice link')
    except Exception:
        async with LOCK:
            DB.execute("update biz_invoices set status='issue_failed' where id=? and status='issuing'",(iid,));DB.commit()
        raise BillingError('Telegram не створив рахунок. Спробуйте пізніше.',503) from None
    async with LOCK:
        DB.execute("update biz_invoices set status='pending',url=? where id=?",(link,iid));DB.commit()
    return {'invoice_id':iid,'url':link,'currency':'XTR','amount':offer['price_stars'],'one_time':True,'duration_days':offer['duration_days']}


async def pre_checkout(query):
    payload=getattr(query,'invoice_payload','')
    if not isinstance(payload,str) or not payload.startswith(PREFIX):return False
    iid=_id(payload)
    error='Рахунок недійсний або вже використаний. Створіть новий рахунок.'
    okay=False
    if DB is not None and iid:
        async with LOCK:
            row=_one('select * from biz_invoices where id=?',(iid,))
            uid=getattr(getattr(query,'from_user',None),'id',None)
            if row and row['user_id']==uid and getattr(query,'currency',None)=='XTR' and getattr(query,'total_amount',None)==row['amount']:
                active=_one('select * from biz_offers where id=? and active=1',(row['offer_id'],))
                import business
                if active and hasattr(business,'eligible_offer') and not business.eligible_offer(uid,active):
                    active=None
                user=_one('select 1 from users where id=? and banned=0',(uid,))
                fresh=row['created']>=(_now()-dt.timedelta(hours=24)).isoformat(sep=' ')
                same=row['status']=='approved' and row['precheck_id']==str(query.id)
                has_perk=_one('select 1 from biz_perks where user_id=? and kind=? and until>?',(uid,row['kind'],_now().isoformat(sep=' ')))
                other_approved=_one("select 1 from biz_invoices where user_id=? and kind=? and status='approved' and id!=? and created>?",(uid,row['kind'],iid,(_now()-dt.timedelta(hours=24)).isoformat(sep=' ')))
                if active and user and fresh and not has_perk and not other_approved and (row['status']=='pending' or same):
                    DB.execute("update biz_invoices set status='approved',precheck_id=? where id=?",(str(query.id),iid));DB.commit()
                    okay=True
    await query.answer(ok=okay,**({} if okay else {'error_message':error}))
    return True


async def _notify(message,text):
    try:
        await message.answer(text)
    except Exception:
        logging.warning('Stars payment notification could not be delivered')


async def successful(message):
    payment=getattr(message,'successful_payment',None)
    payload=getattr(payment,'invoice_payload','')
    if not isinstance(payload,str) or not payload.startswith(PREFIX):return False
    if DB is None:
        logging.error('Stars payment received while billing storage is not initialized')
        await _notify(message,'Оплата потребує перевірки адміністрацією. /paysupport')
        return True
    iid=_id(payload)
    uid=getattr(getattr(message,'from_user',None),'id',None)
    currency=getattr(payment,'currency','')
    amount=getattr(payment,'total_amount',0)
    charge=getattr(payment,'telegram_payment_charge_id','')
    if not isinstance(charge,str) or not 1<=len(charge)<=300 or not isinstance(uid,int) or not isinstance(amount,int):
        logging.error('Malformed trusted Stars payment update requires review')
        await _notify(message,'Платіж потребує перевірки. /paysupport')
        return True
    granted=False
    async with LOCK:
        try:
            DB.execute('begin immediate')
            existing=_one('select * from biz_star_receipts where charge_id=?',(charge,))
            if existing:
                DB.commit()
                return True
            row=_one('select * from biz_invoices where id=?',(iid,)) if iid else None
            reason=''
            if not row:reason='invoice_not_found'
            elif row['user_id']!=uid:reason='user_mismatch'
            elif currency!='XTR' or amount!=row['amount']:reason='amount_mismatch'
            elif row['status']!='approved' or not row['precheck_id']:reason='invoice_not_approved'
            if reason:
                DB.execute('insert into biz_star_receipts(charge_id,invoice_id,user_id,amount,currency,status,reason) values(?,?,?,?,?,?,?)',
                           (charge,iid,uid,amount,currency,'reconciliation_required',reason))
            else:
                # Independent connection, same transaction: receipt and entitlement
                # cannot diverge on restart, duplicate updates or concurrent delivery.
                until=(_now()+dt.timedelta(days=row['duration_days'])).isoformat(sep=' ')
                DB.execute('insert into biz_perks(user_id,offer_id,kind,title,until,request_id) values(?,?,?,?,?,?)',
                           (uid,row['offer_id'],row['kind'],row['title'],until,'invoice:'+row['id']))
                DB.execute("update biz_invoices set status='paid',charge_id=?,paid_at=? where id=?",(charge,_now().isoformat(sep=' '),iid))
                DB.execute('insert into biz_star_receipts(charge_id,invoice_id,user_id,amount,currency,status) values(?,?,?,?,?,?)',
                           (charge,iid,uid,amount,currency,'settled'))
                granted=True
            DB.commit()
        except BaseException:
            DB.rollback()
            raise
    await _notify(message,('Оплату Stars отримано. Перевагу активовано. Рахунок: '+iid) if granted else 'Оплату зафіксовано для перевірки. Перевагу ще не активовано. /paysupport')
    return True


async def refund(actor,invoice_id):
    import business
    business.require(actor,'owner')
    if DB is None:raise BillingError('Оплата тимчасово недоступна.',503)
    async with LOCK:
        row=_one('select * from biz_invoices where id=?',(str(invoice_id),))
        if not row:raise BillingError('Рахунок не знайдено.',404)
        if row['status']=='refunded':return {'ok':True,'status':'refunded'}
        if row['status']!='paid':raise BillingError('Повернення недоступне або попередня спроба потребує звірки.',409)
        DB.execute("update biz_invoices set status='refund_pending' where id=?",(row['id'],));DB.commit()
    try:
        confirmed=await B.bot.refund_star_payment(user_id=row['user_id'],telegram_payment_charge_id=row['charge_id'])
        if confirmed is not True:raise ValueError('Unconfirmed refund')
    except Exception:
        async with LOCK:
            DB.execute("update biz_invoices set status='refund_uncertain' where id=?",(row['id'],));DB.commit()
        raise BillingError('Результат повернення невідомий. Звірте транзакцію Telegram; автоматичний повтор заблоковано.',409) from None
    async with LOCK:
        try:
            DB.execute('begin immediate')
            DB.execute("update biz_invoices set status='refunded',refund_at=? where id=?",(_now().isoformat(sep=' '),row['id']))
            DB.execute("update biz_star_receipts set status='refunded' where charge_id=?",(row['charge_id'],))
            DB.execute('delete from biz_perks where user_id=? and request_id=?',(row['user_id'],'invoice:'+row['id']))
            DB.commit()
        except BaseException:
            DB.rollback()
            raise
    return {'ok':True,'status':'refunded'}
