"""Домівка — Telegram-бот оренди житла. Працює сам по собі: власна база SQLite, без сайту."""
import asyncio
import csv
import datetime as dt
import io
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo,
                           InputMediaPhoto, KeyboardButton, LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardMarkup, ReplyKeyboardRemove)

import db
import feat
import growth
import tunnel
import webapp
import market
import market_bot
import business
import billing
import deployment
import cart
from version import VERSION

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_env():
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ[k.strip()] = v.strip().strip('"')
    if not os.environ.get("BOT_TOKEN"):
        if not sys.stdin.isatty():
            sys.exit("Немає BOT_TOKEN у файлі .env")
        print("\nПерший запуск. Потрібні два значення (інструкція у файлі ИНСТРУКЦИЯ.md).")
        tok = input("1) Вставте токен бота від @BotFather і натисніть Enter: ").strip()
        adm = input("2) Ваш Telegram ID (число від @userinfobot), або Enter, щоб пропустити: ").strip()
        with ENV.open("a", encoding="utf-8") as f:
            f.write(f"\nBOT_TOKEN={tok}\n" + (f"ADMIN_IDS={adm}\n" if adm else ""))
        os.environ["BOT_TOKEN"] = tok
        if adm:
            os.environ["ADMIN_IDS"] = adm


bot: Bot
dp = Dispatcher()
S: dict[int, dict] = {}            # тимчасовий стан діалогів (майстри, календар)
ADMINS: set[int] = set()
SITE = ""
APPROVAL = False
PAYMODE = "off"          # off | manual | telegram
DEPOSIT, COMMISSION, PAY_HOURS, HOST_HOURS, FREE_CANCEL_H = 30, 0, 12, 24, 48
BOOST_PRICE, BOOST_FREE_DAYS, PAY_TOKEN, PAY_REQ = 0, 3, "", ""
PRO = {"free": 3, "max": 50, "stars": {30: 400, 90: 1000}, "boost_off": 20}   # тариф Pro: ліміти, ціна в Stars, знижка на «Топ» у %
BOT_USERNAME = ""
WEBAPP_URL = ""


def app_url(**q) -> str:
    """Посилання на Mini App з параметрами (?l=id, ?t=tab, ?c=chat). Порожнє, якщо Mini App ще не готовий."""
    if not WEBAPP_URL:
        return ""
    return WEBAPP_URL + ("/?" + "&".join(f"{k}={v}" for k, v in q.items()) if q else "")
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
LINK_RE = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+|@\w{4,})", re.I)
VIS = "active=1 and blocked=0"     # умова «оголошення видиме всім»
MN = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]


# ───────────────────────── утиліти ─────────────────────────
def today() -> str:
    return dt.date.today().isoformat()


def addd(s: str, n: int) -> str:
    return (dt.date.fromisoformat(s) + dt.timedelta(days=n)).isoformat()


def uah(n) -> str:
    return f"{round(n):,}".replace(",", " ") + " ₴"


def esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def eff(l: dict, month: bool) -> float:
    base = (l["price_month"] or l["price_night"] * 25) if month else l["price_night"]
    return base * (100 - l["discount"]) / 100


def club_total(l: dict, n: int, member: bool) -> int:
    """Ціна з урахуванням знижки Клубу (її господар вмикає в оголошенні, club_pct)."""
    t = total(l, n)
    return round(t * (100 - l["club_pct"]) / 100) if member and l.get("club_pct") else t


async def notify_price_drop(lid: int, old: float, new: float):
    """Сповіщення тим, хто додав оголошення в обране, коли ціна знизилась."""
    if new >= old:
        return
    l = await db.one("select title, owner from listings where id=?", (lid,))
    for f in await db.q("select user from favs where listing_id=? and user!=? limit 300", (lid, l["owner"] if l else 0)):
        await tell(f["user"], f"📉 Ціна впала: <b>{esc(l['title'])}</b>\n{uah(old)} → <b>{uah(new)}</b>/ніч",
                   [[("👁 Переглянути", f"lc:{lid}")] + ([("📱 У додатку", "webapp:" + app_url(l=lid))] if WEBAPP_URL else [])])


def total(l: dict, n: int) -> int:
    if n >= 28 and l["price_month"]:
        return round(l["price_month"] * n / 30 * (100 - l["discount"]) / 100)
    return round(n * l["price_night"] * (100 - l["discount"]) / 100)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t, web_app=WebAppInfo(url=d[7:])) if d.startswith("webapp:") else InlineKeyboardButton(text=t, url=d) if d.startswith("http") else InlineKeyboardButton(text=t, callback_data=d)
        for t, d in r] for r in rows])


async def say(chat: int, text: str, rows=None, **kw):
    return await bot.send_message(chat, text, reply_markup=kb(rows) if rows else None, disable_web_page_preview=True, **kw)


async def tell(uid: int, text: str, rows=None) -> bool:
    try:
        await say(uid, text, rows)
        return True
    except Exception:
        return False


async def rating(lid=None, host=None, guest=None):
    if lid:
        w, v = "listing_id=? and kind='guest'", lid
    elif host:
        w, v = "subject=? and kind='guest'", host
    else:
        w, v = "subject=? and kind='host'", guest
    r = await db.one("select avg(rating) a, count(*) n from reviews where " + w, (v,))
    return (round(r["a"], 1) if r["a"] else None), r["n"]


def stars(a, n) -> str:
    return f"⭐ {a} ({n})" if n else "ще без відгуків"


async def is_super(host: int) -> bool:
    a, n = await rating(host=host)
    return bool(a and a >= 4.7 and n >= 3)


def menu_rows(u: dict):
    host = u["role"] == "host"
    r = [[("📱 Відкрити Домівку", "webapp:" + WEBAPP_URL)]] if WEBAPP_URL else []
    return r + [[("Маркет / сервіси", "mk:services")], [("🔍 Пошук", "m:s"), ("📅 Бронювання", "m:t")], [("❤️ Обране", "m:f"), ("💬 Чати", "m:c")],
                [("🏠 Кабінет господаря" if host else "🏠 Здавати житло", "m:host" if host else "m:h"), ("⋯ Ще", "m:more")]]


def more_rows(u: dict):
    r = [[("🎁 Клуб і запрошення", "cl:m")], [("🔥 Топ", "m:top"), ("🔔 Підписки", "m:al")], [("🤖 AI-помічник", "z:ai"), ("⚖️ Порівняння", "z:cp:s")],
         [("📋 Угоди й акти", "z:my"), ("🛡 Верифікація ✅" if u.get("verified") == 2 else "🛡 Верифікація", "z:vm")],
         [("ℹ️ Допомога", "m:?")] + ([("🌐 Сайт", SITE)] if SITE else [])]
    if u["id"] in ADMINS:
        r.append([("🛡 Адмін-панель", "a:m")])
    return r + [[("⬅ Назад", "m:m")]]


async def host_menu(chat: int, u: dict, text: str = ""):
    uid = u["id"]
    n = (await db.one("select count(*) n from bookings b join listings l on l.id=b.listing_id where l.owner=? and (b.status='pending' or (b.status='awaiting_payment' and b.method='claimed'))", (uid,)))["n"]
    m = (await db.one("select count(*) n from listings where owner=?", (uid,)))["n"]
    await say(chat, (text + "\n\n" if text else "") + f"🏠 <b>Кабінет господаря</b>\nОголошень: {m} · нових заявок: {n}",
              [[(f"📥 Заявки ({n})" if n else "📥 Заявки", "m:r"), ("🏠 Оголошення", "m:o")], [("➕ Нове оголошення", "m:n"), ("📊 Статистика", "m:i")],
               [("💳 Як мені платити", "m:pr"), ("🛡 Верифікація", "z:vm")]] + ([[("📱 Кабінет у додатку", "webapp:" + app_url(t="me"))]] if WEBAPP_URL else []) + [[("⬅ Назад", "m:m")]])


async def home(chat: int, u: dict, text: str = "Що шукаємо? Натисніть кнопку або напишіть як людині: «2 кімнати на місяць у центрі до 15000»."):
    S.pop(u["id"], None)
    return await say(chat, text, menu_rows(u))


async def ask_phone(chat: int):
    await bot.send_message(chat, "Щоб господарі й гості могли зв'язатися, поділіться номером (Telegram підтверджує його сам).",
                           reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Поділитися номером", request_contact=True)], [KeyboardButton(text="Пропустити")]],
                                                            resize_keyboard=True, one_time_keyboard=True))


async def gemini(prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')}:generateContent",
                             params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}]})
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        logging.warning("Gemini недоступний")
        return ""


async def make_descr(d: dict) -> str:
    t = (await gemini("Напиши привабливий і чесний опис оголошення про оренду житла українською, 70-110 слів, без вигаданих фактів, "
                      f"без емодзі і списків. Дані: {json.dumps(d, ensure_ascii=False)[:800]}")).strip()
    return t or (f"{d.get('title', 'Житло')}. {d.get('rooms', 1)}-кімнатне житло в районі {d.get('district', '')}, {d.get('city', '')}, "
                 f"розраховане на {d.get('guests', 2)} гостей. Охайно, зручно, підходить для короткої та довгої оренди. Деталі уточнюйте в чаті.")


def parse_query(text: str, cities, districts) -> dict:
    t = text.lower()
    f = {"city": next((c for c in cities if c.lower() in t), ""), "district": next((d for d in districts if d.lower() in t), ""),
         "month": bool(re.search(r"місяц|месяц|довго", t)), "max": 0, "rooms": 0, "guests": 0}
    m = re.search(r"(\d{3,6})", t.replace(" ", ""))
    if m:
        f["max"] = int(m.group(1))
    m = re.search(r"(\d)\s*-?\s*(кімн|комн)", t)
    if m:
        f["rooms"] = int(m.group(1))
    return f


async def ai_filters(text: str) -> dict:
    cities = [r["city"] for r in await db.q(f"select distinct city from listings where {VIS}")]
    districts = [r["district"] for r in await db.q(f"select distinct district from listings where {VIS}")]
    f = parse_query(text, cities, districts)
    raw = await gemini(f'Користувач шукає житло: "{text[:300]}". Міста: {json.dumps(cities, ensure_ascii=False)}. Райони: {json.dumps(districts, ensure_ascii=False)}. '
                       'Поверни ТІЛЬКИ JSON: {"city":рядок зі списку або null,"district":рядок зі списку або null,"max":число грн або null,"rooms":число або null,"guests":число або null,"month":true/false}')
    try:
        j = json.loads(raw.replace("```json", "").replace("```", "").strip())
        f = {"city": j.get("city") if j.get("city") in cities else f["city"], "district": j.get("district") if j.get("district") in districts else f["district"],
             "max": j.get("max") or f["max"], "rooms": j.get("rooms") or f["rooms"], "guests": j.get("guests") or 0, "month": bool(j.get("month")) or f["month"]}
    except Exception:
        pass
    f["amen"] = feat.amen_from_text(text)
    return f



# ───────────────────────── оплата, виплати, просування ─────────────────────────
def now_s(h: int = 0) -> str:
    return (dt.datetime.now() + dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")


def is_boosted(l: dict) -> bool:
    return bool(l.get("boost_until")) and l["boost_until"] >= today()


def deposit_of(total_: int) -> int:
    return round(total_ * DEPOSIT / 100)


async def guest_line(gid: int) -> str:
    a, n = await rating(guest=gid)
    done = (await db.one("select count(*) n from bookings where guest=? and status='confirmed' and co<=date('now')", (gid,)))["n"]
    g = await db.one("select phone, verified, strikes from users where id=?", (gid,))
    return f"{stars(a, n)} · проживань: {done}{' · ✅ номер' if g and g['phone'] else ''}{' · 🛡 верифікований' if g and g['verified'] == 2 else ''}{' · ⚠️ претензій: ' + str(g['strikes']) if g and g['strikes'] else ''}"


async def ask_payment(b: dict, l: dict):
    if PAYMODE == "direct":
        return await feat.ask_direct(b)
    dep = deposit_of(b["total"])
    txt = (f"💳 <b>Оплата застави</b>\n{esc(l['title'])}\n📅 {b['ci']} → {b['co']}\nДо сплати зараз: <b>{uah(dep)}</b>"
           + (f" з {uah(b['total'])} (решту {uah(b['total'] - dep)} сплатите господарю при заселенні)" if dep < b["total"] else "")
           + f"\nОплатіть до {b['due_by']}, інакше бронювання скасується.\nБезкоштовне скасування: не пізніше ніж за {FREE_CANCEL_H} год до заїзду.")
    if PAYMODE == "telegram":
        rows = [[(f"💳 Оплатити {uah(dep)}", f"pay:{b['id']}")]]
    else:
        txt += f"\n\n<b>Реквізити для оплати:</b>\n{esc(PAY_REQ)}\nУ призначенні платежу вкажіть код <b>#{b['id']}</b>. Після оплати натисніть кнопку й надішліть скріншот."
        rows = [[("✅ Я оплатив(ла)", f"paid:{b['id']}")]]
    rows.append([("✖ Скасувати бронювання", f"bc:{b['id']}")])
    await tell(b["guest"], txt, rows)


async def mark_paid(bid: int, method: str, charge: str = "") -> bool:
    b = await db.one("select b.*, l.title, l.owner, l.address from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    if not b or b["status"] != "awaiting_payment":
        return False
    dep = deposit_of(b["total"])
    com = min(dep, round(b["total"] * COMMISSION / 100))
    await db.ex("update bookings set status='confirmed', paid=1, deposit=?, commission=?, payout_status='hold', method=?, charge_id=?, due_by='' where id=?", (dep, com, method, charge, bid))
    await db.ex("insert into payments(kind,ref,user,amount,method,charge_id) values('deposit',?,?,?,?,?)", (bid, b["guest"], dep, method, charge))
    h = await db.one("select name, phone, username from users where id=?", (b["owner"],))
    g = await db.one("select name, phone from users where id=?", (b["guest"],))
    info = ("🔑 Адреса та інструкції:\n" + esc(b["address"])) if b["address"] else "Адресу й інструкції заселення надішле господар у чаті."
    handle = "@" + h["username"] if h["username"] else ""
    await tell(b["guest"], f"✅ Оплату отримано, бронювання підтверджено!\n<b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']}\nГосподар: {esc(h['name'])} {esc(h['phone'])} {handle}\n{info}",
               [[("💬 Написати господарю", f"ask:{b['listing_id']}")]])
    await tell(b["owner"], f"💰 Гість сплатив заставу {uah(dep)}: {esc(b['title'])}, {b['ci']} → {b['co']}\n👤 {esc(g['name'])} {esc(g['phone'])}\nКошти утримуються платформою й надійдуть вам після заселення гостя.",
               [[("💬 Написати гостю", f"ask:{b['listing_id']}")]])
    await feat.on_confirmed(bid)
    return True


async def release(bid: int):
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    if not b or b["payout_status"] != "hold":
        return
    await db.ex("update bookings set payout_status='due' where id=?", (bid,))
    amt = b["deposit"] - b["commission"]
    h = await db.one("select name, payout from users where id=?", (b["owner"],))
    warn = "" if h and h["payout"] else "\n⚠️ Вкажіть реквізити для виплат: меню → 💳 Реквізити виплат."
    await tell(b["owner"], f"✅ Заселення підтверджено. Виплату {uah(amt)} за «{esc(b['title'])}» передано в обробку.{warn}")
    for a in ADMINS:
        await tell(a, f"💸 До виплати #{bid}: {uah(amt)} господарю {esc(h['name'] if h else '')}", [[("💰 Фінанси", "a:f")]])


async def apply_boost(lid: int, days: int) -> str:
    l = await db.one("select boost_until from listings where id=?", (lid,))
    until = addd(max(today(), l["boost_until"] or today()), days)
    await db.ex("update listings set boost_until=? where id=?", (until, lid))
    return until


def paid_boost() -> bool:
    return feat.stars_on() or (BOOST_PRICE > 0 and PAYMODE == "telegram" and bool(PAY_TOKEN))


def boost_label(d: int) -> str:
    return f"{feat.boost_price(d)} ⭐" if feat.stars_on() else uah(BOOST_PRICE * d)


async def boost_invoice(chat: int, l: dict, d: int):
    """Просування оплачується Telegram Stars (без ФОП/еквайрингу) або, якщо налаштовано, карткою через провайдера."""
    if feat.stars_on():
        return await bot.send_invoice(chat, title="Просування оголошення", description=f"{l['title'][:60]}: {d} дн.", payload=f"bo:{l['id']}:{d}", provider_token="",
                                      currency="XTR", prices=[LabeledPrice(label=f"{d} дн.", amount=feat.boost_price(d))])
    return await bot.send_invoice(chat, title="Просування оголошення", description=f"{l['title'][:60]}: {d} дн.", payload=f"bo:{l['id']}:{d}", provider_token=PAY_TOKEN,
                                  currency="UAH", prices=[LabeledPrice(label=f"{d} дн.", amount=BOOST_PRICE * d * 100)])


async def guest_profile(chat: int, gid: int):
    g = await db.one("select * from users where id=?", (gid,))
    if not g:
        return
    rv = await db.q("select r.*, u.name from reviews r join users u on u.id=r.author where r.subject=? and r.kind='host' order by r.id desc limit 3", (gid,))
    await say(chat, f"👤 <b>{esc(g['name'])}</b>\n{await guest_line(gid)}\nНа платформі з {g['created'][:10]}\n\n" + ("\n\n".join(f"{'⭐' * r['rating']} {esc(r['name'])}: {esc(r['text'])}" for r in rv) or "Відгуків від господарів поки немає."))


async def notify_alerts(lid: int):
    l = await db.one(f"select * from listings where id=? and {VIS}", (lid,))
    if not l:
        return
    for a in await db.q("select * from alerts where user!=? and (city='' or city=?) and (district='' or district=?)", (l["owner"], l["city"], l["district"])):
        if not a["max"] or eff(l, bool(a["month"])) <= a["max"]:
            await tell(a["user"], f"🔔 Нове оголошення за вашою підпискою:\n<b>{esc(l['title'])}</b>\n{esc(l['city'])}, {esc(l['district'])} · {uah(eff(l, bool(a['month'])))}", [[("👁 Переглянути", f"lc:{lid}")]])


async def clean_text(c: dict, text: str):
    """До підтвердження бронювання контакти й посилання в чаті приховуються (захист від шахрайства та обходу платформи)."""
    if await db.one("select 1 from bookings where listing_id=? and guest=? and status='confirmed'", (c["listing_id"], c["guest"])):
        return text, False
    t2 = LINK_RE.sub("[посилання приховано]", PHONE_RE.sub("[контакт приховано]", text))
    return t2, t2 != text


# ───────────────────────── картка оголошення ─────────────────────────
async def card(chat: int, uid: int, lid: int, nav=None, month=False, avg=0.0):
    l = await db.one("select * from listings where id=?", (lid,))
    if not l or ((l["blocked"] or not l["active"]) and l["owner"] != uid and uid not in ADMINS):
        return await say(chat, "Це оголошення недоступне.")
    if l["owner"] != uid:
        await db.ex("update listings set views=views+1 where id=?", (lid,))
    if not avg:
        rows = await db.q(f"select * from listings where city=? and {VIS}", (l["city"],))
        avg = sum(eff(x, month) for x in rows) / len(rows) if rows else 0
    host = await db.one("select * from users where id=?", (l["owner"],))
    ra, rn = await rating(lid=lid)
    pr = eff(l, month)
    diff = round((pr - avg) / avg * 100) if avg else 0
    sup = " 🏅" if await is_super(l["owner"]) else ""
    disc = " (−%d%%)" % l["discount"] if l["discount"] else ""
    ex_txt, _ = await feat.card_extra(l, host, S.get(uid, {}).get("f"), diff)
    lines = [f"<b>{esc(l['title'])}</b>{' 🔥' if is_boosted(l) else ''}{' ⚡' if l['instant'] else ''}",
             f"📍 {esc(l['city'])}, {esc(l['district'])} · 🛏 {l['rooms']} · 👥 {l['guests']}" + (f" · від {l['min_nights']} ноч." if l["min_nights"] > 1 else ""),
             f"💰 <b>{uah(pr)}</b>/{'міс' if month else 'ніч'}{disc}" + (f" · на {abs(diff)}% {'дешевше' if diff < 0 else 'дорожче'} за середнє" if abs(diff) >= 10 else ""),
             f"{stars(ra, rn)} · 👤 {esc(host['name']) if host else 'Господар'}{sup}"]
    if ex_txt:
        lines.append(ex_txt)
    if l["blocked"] == 1:
        lines.append("⛔ Заблоковано адміністрацією")
    elif l["blocked"] == 2:
        lines.append("⏳ На модерації")
    if l["descr"]:
        lines.append("\n" + esc(l["descr"][:220]))
    cap = "\n".join(lines)
    fav = await db.one("select 1 from favs where user=? and listing_id=?", (uid, lid))
    rows = []
    if nav:
        i, n = nav
        rows.append([("◀", f"r:{i - 1}"), (f"{i + 1}/{n}", "x"), ("▶", f"r:{i + 1}")])
    rows += [[("📅 Забронювати", f"bk:{lid}"), ("💬 Запитати", f"ask:{lid}")],
             [("💔" if fav else "❤️", f"fv:{lid}"), ("⭐ Відгуки", f"rl:{lid}:0"), ("⋯ Ще", f"mo:{lid}")],
             ([("🎛 Фільтри", "sf")] if nav else []) + [("🏠 Меню", "m:m")]]
    photos = json.loads(l["photos"])
    try:
        if photos:
            return await bot.send_photo(chat, photos[0], caption=cap[:1024], reply_markup=kb(rows))
    except Exception:
        pass
    return await say(chat, cap, rows)


async def more_listing(chat: int, lid: int, uid: int = 0):
    l = await db.one("select owner, photos, lat from listings where id=?", (lid,))
    if not l:
        return
    n = len(json.loads(l["photos"]))
    rows = [[("👤 Господар", f"hp:{l['owner']}")] + ([(f"📷 Фото ({n})", f"ph:{lid}")] if n > 1 else []),
            [("⚖️ Порівняти", f"z:cp:a:{lid}"), ("🤖 Спитати AI", f"z:qa:{lid}")]]
    if l["lat"] is not None:
        rows.append([("🗺 На мапі", f"z:mp:{lid}")])
    if WEBAPP_URL:
        rows.insert(0, [("📱 Відкрити в додатку", "webapp:" + app_url(l=lid))])
    rows += [[("🔗 Поділитись", f"https://t.me/share/url?url={quote(f'https://t.me/{BOT_USERNAME}?start=l_{lid}' + (f'__r_{uid}' if uid else ''))}"), ("🚩 Скарга", f"rp:{lid}")], [("✕ Закрити", "cx")]]
    await say(chat, "⋯ <b>Ще про це житло</b>", rows)


# ───────────────────────── пошук ─────────────────────────
def frows(f):
    lab = {"price": "↕ Дешеві", "rating": "↕ З рейтингом", "new": "↕ Нові", "dist": "↕ Ближчі"}
    return [[("📆 За місяць" if f["month"] else "🌙 За ніч", "fm"), (lab[f.get("sort") or ("dist" if f.get("near") else "price")], "fs")],
            [(("✓" if f.get("max", 0) == v else "") + (f"до {v}" if v else "∞"), f"fx:{v}") for v in (1000, 2000, 3000, 0)],
            *feat.search_rows(f),
            [("🔔 Стежити", "al"), ("🔍 Місто", "m:s"), ("🏠", "m:m")]]


async def run_search(chat: int, uid: int, f: dict):
    sql = f"select l.*, (select avg(rating) from reviews r where r.listing_id=l.id) ra from listings l where {VIS}"
    a: tuple = ()
    if f.get("city"):
        sql += " and city=?"
        a = (f["city"],)
    peers = await db.q(sql, a)
    avg = sum(eff(x, f["month"]) for x in peers) / len(peers) if peers else 0
    L = [x for x in peers if (not f.get("district") or x["district"] == f["district"]) and (not f.get("rooms") or x["rooms"] == f["rooms"])
         and (not f.get("guests") or x["guests"] >= f["guests"]) and (not f.get("max") or eff(x, f["month"]) <= f["max"]) and (not f.get("top") or is_boosted(x))]
    L = await feat.filter_extra(L, f)
    srt = f.get("sort") or ("dist" if f.get("near") else "price")
    L.sort(key=(lambda x: x.get("_km", 1e9)) if srt == "dist" else (lambda x: eff(x, f["month"])) if srt == "price" else (lambda x: -(x["ra"] or 0)) if srt == "rating" else (lambda x: -x["id"]))
    L.sort(key=lambda x: not is_boosted(x))
    S[uid] = {"flow": "search", "f": f, "ids": [x["id"] for x in L[:40]], "avg": avg}
    if not L:
        return await say(chat, "Нічого не знайдено. Змініть фільтри або опишіть запит інакше.", frows(f))
    await card(chat, uid, L[0]["id"], (0, len(S[uid]["ids"])), f["month"], avg)


# ───────────────────────── календар ─────────────────────────
async def calendar(chat: int, lid: int, ym: str, ci=None, mode="book", msg: Message | None = None):
    occ = await db.occupied(lid)
    blk = {r["d"] for r in await db.q("select d from blocks where listing_id=?", (lid,))}
    y, mo = map(int, ym.split("-"))
    first = dt.date(y, mo, 1)
    dim = (dt.date(y + (mo == 12), mo % 12 + 1, 1) - first).days
    limit = min((d for d in occ if ci and d >= ci), default="9999-12-31")
    prev = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    nxt = (first + dt.timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    rows = [[("◀", f"cal:{mode}:{lid}:{prev}"), (f"{MN[mo - 1]} {y}", "x"), ("▶", f"cal:{mode}:{lid}:{nxt}")], [(d, "x") for d in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")]]
    row = [(" ", "x")] * first.weekday()
    for day in range(1, dim + 1):
        d = f"{ym}-{day:02d}"
        if mode == "block":
            b = ("·", "x") if d < today() else ("🔒", f"hb:{lid}:{d}") if d in blk else ("❌", "x") if d in occ else (str(day), f"hb:{lid}:{d}")
        elif d < today():
            b = ("·", "x")
        elif not ci:
            b = ("❌", "x") if d in occ else (str(day), f"d:{lid}:{d}")
        elif d == ci:
            b = ("🟢", "x")
        elif d < ci:
            b = ("·", "x")
        else:
            b = (str(day), f"d:{lid}:{d}") if d <= limit else ("❌", "x")
        row.append(b)
        if len(row) == 7:
            rows.append(row)
            row = []
    if row:
        rows.append(row + [(" ", "x")] * (7 - len(row)))
    rows.append([("🏠 Меню", "m:m")] if mode == "book" else [("⬅ Назад", f"ml:{lid}")])
    text = ("Натисніть дату, щоб закрити її (🔒) або відкрити. ❌ — заброньовано." if mode == "block"
            else f"Заїзд: <b>{ci}</b>. Оберіть дату виїзду (❌ — зайнято):" if ci else "Оберіть дату заїзду (❌ — вже зайнято):")
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=kb(rows))
        except Exception:
            return
    return await say(chat, text, rows)


async def confirm_booking(chat: int, uid: int):
    s = S[uid]
    l = await db.one("select * from listings where id=?", (s["lid"],))
    n = (dt.date.fromisoformat(s["co"]) - dt.date.fromisoformat(s["ci"])).days
    g = s.get("guests", 1)
    gb = [(f"✓{i}" if i == g else str(i), f"bg:{i}") for i in range(1, min(l["guests"], 6) + 1)]
    me = await db.one("select points from users where id=?", (uid,))
    mem = growth.is_member(me["points"] if me else 0)
    club = f" (знижка Клубу −{l['club_pct']}% 🎁)" if mem and l["club_pct"] else ""
    await say(chat, f"<b>{esc(l['title'])}</b>\n📅 {s['ci']} → {s['co']} ({n} дн.)\n👥 Гостей: {g}\n💰 Разом: <b>{uah(club_total(l, n, mem))}</b>{club}\n\nОберіть кількість гостей і надішліть заявку:",
              [gb, [("✅ Надіслати заявку", "bf")], [("📅 Змінити дати", f"bk:{l['id']}"), ("🏠 Меню", "m:m")]])


async def confirm_off(bid: int):
    """Режим без оплати: підтвердження й показ контактів."""
    b = await db.one("select b.*, l.title, l.owner, l.address from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    await db.ex("update bookings set status='confirmed', due_by='' where id=?", (bid,))
    h = await db.one("select name, phone, username from users where id=?", (b["owner"],))
    handle = "@" + h["username"] if h["username"] else ""
    info = ("🔑 Адреса та інструкції:\n" + esc(b["address"])) if b["address"] else ""
    await tell(b["guest"], f"✅ Бронювання підтверджено!\n<b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']}\nКонтакти господаря: {esc(h['name'])} {esc(h['phone'])} {handle}\n{info}", [[("💬 Написати", f"ask:{b['listing_id']}")]])
    await feat.on_confirmed(bid)


async def make_booking(u: dict, lid: int, ci: str, co: str, guests: int) -> dict:
    """Спільна логіка бронювання для бота й Mini App. Повертає {'err': ...} або результат."""
    if not u["phone"]:
        return {"err": "phone"}
    try:
        n = (dt.date.fromisoformat(co) - dt.date.fromisoformat(ci)).days
    except Exception:
        return {"err": "Невірні дати."}
    if n < 1 or ci < today() or n > 365:
        return {"err": "Невірні дати."}
    l = await db.one(f"select * from listings where id=? and {VIS}", (lid,))
    if not l or l["owner"] == u["id"]:
        return {"err": "Це оголошення недоступне для бронювання."}
    if not 1 <= guests <= l["guests"]:
        return {"err": f"Максимум гостей: {l['guests']}."}
    if n < l["min_nights"]:
        return {"err": f"Мінімальний термін проживання: {l['min_nights']} ноч.", "retry": True}
    tot = club_total(l, n, growth.is_member(u.get("points", 0)))
    need = await feat.pay_needed(l, u["id"])
    ready = PAYMODE != "direct" or not need or (await feat.host_ready(l["owner"]))[0]
    if l["instant"] and ready:
        status, due = ("confirmed", "") if not need else ("awaiting_payment", now_s(PAY_HOURS))
    else:
        status, due = "pending", now_s(HOST_HOURS)
    bid = await db.book(l["id"], u["id"], ci, co, guests, tot, status, due)
    if not bid:
        return {"err": "На ці дати житло щойно зайняли. Оберіть інші.", "retry": True}
    b = await db.one("select * from bookings where id=?", (bid,))
    b["title"] = l["title"]
    info = f"{esc(l['title'])}\n📅 {ci} → {co} · 👥 {guests} · 💰 {uah(tot)}\n👤 {esc(u['name'])} {esc(u['phone'])}\n{await guest_line(u['id'])}"
    if status == "pending":
        await tell(l["owner"], f"🏠 <b>Нова заявка</b>\n{info}\n⏳ Відповідь потрібна до {due}, інакше заявка скасується.",
                   [[("✅ Підтвердити", f"bs:{bid}:c"), ("❌ Відхилити", f"bs:{bid}:x")], [("👤 Профіль гостя", f"gp:{u['id']}"), ("💬 Написати", f"ask:{l['id']}")]])
    else:
        await tell(l["owner"], f"⚡ <b>Миттєве бронювання</b>\n{info}" + ("" if status == "confirmed" else f"\nОчікуємо оплату гостя до {due}."))
        if status == "confirmed":
            await confirm_off(bid)
        else:
            await ask_payment(b, b)
    return {"bid": bid, "status": status, "due": due, "total": tot, "title": l["title"], "need": need, "lid": l["id"]}


async def submit_booking(chat: int, u: dict):
    s = S.get(u["id"], {})
    if s.get("flow") != "book" or not s.get("co"):
        return
    r = await make_booking(u, s["lid"], s["ci"], s["co"], s.get("guests", 1))
    if r.get("err") == "phone":
        return await ask_phone(chat)
    if "err" in r:
        return await say(chat, r["err"], [[("📅 Інші дати", f"bk:{s['lid']}")]] if r.get("retry") else None)
    S.pop(u["id"], None)
    if r["status"] == "pending":
        return await say(chat, f"✅ Заявку надіслано · {uah(r['total'])}\nГосподар відповість до {r['due']}.", [[("💬 Написати господарю", f"ask:{r['lid']}")], [("🏠 Меню", "m:m")]])
    if r["status"] == "confirmed":
        return await say(chat, "✅ Бронювання підтверджено", [[("🏠 Меню", "m:m")]])
    await say(chat, "Майже готово: сплатіть заставу за інструкцією вище, і бронювання стане остаточним.")


async def decide_booking(uid: int, bid: int, ok: bool):
    """Господар підтверджує або відхиляє заявку. Повертає (успіх, текст, кнопки)."""
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    if not b or b["owner"] != uid or b["status"] != "pending":
        return False, "Заявку вже оброблено.", None
    if ok:
        lx = await db.one("select * from listings where id=?", (b["listing_id"],))
        if not await feat.pay_needed(lx, b["guest"]):
            await confirm_off(b["id"])
            return True, "Підтверджено ✓", None
        if PAYMODE == "direct":
            okh, why = await feat.host_ready(uid)
            if not okh:
                return False, "ℹ️ " + why, [[("🛡 Верифікація", "z:vm"), ("💳 Реквізити", "m:pr")], [("📥 Заявки", "m:r")]]
        due = now_s(PAY_HOURS)
        await db.ex("update bookings set status='awaiting_payment', due_by=? where id=?", (due, b["id"]))
        b["due_by"] = due
        await ask_payment(b, b)
        return True, f"Підтверджено ✓ Очікуємо оплату гостя до {due}.", None
    await db.ex("update bookings set status='cancelled', due_by='' where id=?", (b["id"],))
    await tell(b["guest"], f"❌ На жаль, господар відхилив заявку: {esc(b['title'])}.", [[("🔍 Шукати інше", "m:s")]])
    return True, "Відхилено.", None


async def cancel_booking(uid: int, bid: int):
    """Скасування гостем. Повертає примітку (рядок) або None, якщо бронювання не знайдено."""
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.status!='cancelled'", (bid, uid))
    if not b:
        return None
    hrs = (dt.datetime.fromisoformat(b["ci"] + "T14:00") - dt.datetime.now()).total_seconds() / 3600
    await db.ex("update bookings set status='cancelled', due_by='' where id=?", (b["id"],))
    note = ""
    if b["paid"] and b["payout_status"] == "hold":
        early = hrs >= FREE_CANCEL_H
        await db.ex("update bookings set payout_status=? where id=?", ("refund_due" if early else "due", b["id"]))
        note = " Заставу буде повернено вам." if early else f" Скасування пізніше ніж за {FREE_CANCEL_H} год до заїзду: застава залишається господарю."
        for ad in ADMINS:
            await tell(ad, f"{'↩️ Потрібне повернення' if early else '💸 До виплати (пізнє скасування)'} #{b['id']}", [[("💰 Фінанси", "a:f")]])
    elif b["payout_status"] == "direct":
        note = await feat.cancel_direct(b, "guest")
    await tell(b["owner"], f"Гість скасував бронювання: {esc(b['title'])}, {b['ci']} → {b['co']}.")
    return note


async def leave_review(uid: int, bid: int, rating_: int, text: str = ""):
    """Відгук гостя про проживання. Повертає id відгуку або None."""
    b = await db.one("select b.*, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.status='confirmed' and b.co<=date('now')", (bid, uid))
    if not b or not 1 <= rating_ <= 5 or await db.one("select 1 from reviews where booking_id=? and kind='guest'", (b["id"],)):
        return None
    rid = await db.ex("insert into reviews(booking_id,kind,listing_id,author,subject,rating,text) values(?,'guest',?,?,?,?,?)", (b["id"], b["listing_id"], uid, b["owner"], rating_, text[:800]))
    await tell(b["owner"], f"⭐ Новий відгук на ваше житло: {'⭐' * rating_}")
    if await growth.award(uid, "review", b["id"]):
        await tell(uid, f"🎁 +{growth.PTS['review']} балів у Клубі за відгук!")
    return rid


async def host_cancel(uid: int, bid: int):
    """Господар скасовує підтверджене бронювання. Повертає примітку або None."""
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and l.owner=? and b.status='confirmed'", (bid, uid))
    if not b:
        return None
    await db.ex("update bookings set status='cancelled' where id=?", (b["id"],))
    extra = ""
    if b["paid"] and b["payout_status"] == "hold":
        await db.ex("update bookings set payout_status='refund_due' where id=?", (b["id"],))
        extra = " Заставу буде повернено вам."
        for ad in ADMINS:
            await tell(ad, f"↩️ Господар скасував оплачене бронювання #{b['id']}: потрібне повернення.", [[("💰 Фінанси", "a:f")]])
    elif b["payout_status"] == "direct":
        extra = await feat.cancel_direct(b, "host")
    await tell(b["guest"], f"❌ Господар скасував бронювання: {esc(b['title'])}, {b['ci']} → {b['co']}.{extra}", [[("🔍 Шукати інше", "m:s")]])
    return extra


async def ask_host(chat: int, uid: int, lid: int):
    l = await db.one("select id,owner from listings where id=?", (lid,))
    if not l:
        return
    if l["owner"] == uid:
        return await say(chat, "Це ваше оголошення.")
    await db.ex("insert or ignore into convs(listing_id,guest) values(?,?)", (l["id"], uid))
    cv = await db.one("select id from convs where listing_id=? and guest=?", (l["id"], uid))
    return await open_chat(chat, uid, cv["id"])


# ───────────────────────── чат ─────────────────────────
async def open_chat(chat: int, uid: int, cid: int):
    c = await db.one("select c.*, l.title, l.owner from convs c join listings l on l.id=c.listing_id where c.id=?", (cid,))
    if not c or uid not in (c["guest"], c["owner"]):
        return
    S[uid] = {"flow": "chat", "cid": cid}
    last = list(reversed(await db.q("select sender, body from msgs where conv_id=? order by id desc limit 5", (cid,))))
    hist = "\n".join(f"{'Ви' if m['sender'] == uid else 'Співрозмовник'}: {esc(m['body'])}" for m in last)
    await say(chat, f"💬 <b>{esc(c['title'])}</b>\n{chr(10) + hist + chr(10) if hist else ''}\nНапишіть повідомлення. Вийти: /menu", [[("🏠 Меню", "m:m")]])


async def open_wishlist(chat: int, owner: int):
    o = await db.one("select name from users where id=?", (owner,))
    ls = await db.q(f"select l.id, l.title from favs f join listings l on l.id=f.listing_id where f.user=? and l.{VIS.replace(' and ', ' and l.')} limit 6", (owner,))
    if not o or not ls:
        return await say(chat, "Список бажань порожній або недоступний.", [[("🔍 Знайти житло", "m:s")]])
    rows = [[("📱 Відкрити список у додатку", "webapp:" + app_url(w=owner))]] if WEBAPP_URL else []
    await say(chat, f"❤️ <b>Список бажань: {esc(o['name'])}</b>\nОголошень: {len(ls)}. Подивіться й обирайте разом.", rows + [[(x["title"][:40], f"lc:{x['id']}")] for x in ls] + [[("🏠 Меню", "m:m")]])


async def club_screen(chat: int, u: dict):
    sn = await growth.snapshot(u["id"], BOT_USERNAME)
    lv = sn["level"]
    nxt = f"\n{'▓' * (lv['pct'] // 10)}{'░' * (10 - lv['pct'] // 10)} ще {lv['next'] - sn['points']} до «{lv['next_name']}»" if lv["next"] else "\n🏆 Максимальний рівень"
    perk = "✅ Знижки Клубу в оголошеннях партнерів активні" if sn["member"] else "🔒 Знижки Клубу відкриваються з рівня «Мандрівник» (100 балів)"
    st = sn["steps"]
    text = (f"🎁 <b>Клуб Домівки</b>\n{lv['emoji']} <b>{lv['name']}</b> · {sn['points']} балів{nxt}\n🔥 Серія візитів: {sn['streak']} дн.\n👥 Запрошено: {sn['invited']} (з номером: {sn['active']})\n\n{perk}\n\n"
            f"<b>Як заробити бали</b>\n• друг приєднався: +{st['friend']}\n• ваше проживання: +{st['stay']}\n• відгук: +{st['review']}\n• оголошення: +{st['listing']}\n• щоденний візит у додаток: +2")
    share = f"https://t.me/share/url?url={quote(sn['link'])}&text={quote('Знайшов зручний сервіс оренди житла в Telegram. Приєднуйся до Домівки!')}"
    rows = [[("📤 Запросити друзів", share)], [("🎟 Обміняти бали", "cl:rw")]]
    if WEBAPP_URL:
        rows.append([("📱 Відкрити Клуб у додатку", "webapp:" + app_url(t="me"))])
    await say(chat, text, rows + [[("⬅ Назад", "m:more")]])


async def open_app(chat: int, u: dict):
    if not WEBAPP_URL:
        return await say(chat, "⏳ Додаток ще запускається (1–2 хвилини після старту бота). Спробуйте /app трохи пізніше.")
    await say(chat, "📱 <b>Домівка</b>: пошук, бронювання, чати й кабінет в одному додатку.", [[("📱 Відкрити додаток", "webapp:" + WEBAPP_URL)]])


async def set_menu_button():
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Домівка", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception:
        logging.warning("Не вдалося встановити кнопку меню Mini App")


async def on_tunnel_url(url: str):
    """Нова публічна адреса Mini App: запам'ятовуємо й оновлюємо кнопку меню."""
    global WEBAPP_URL
    await deployment.verify_public_app(url, bot.id)
    WEBAPP_URL = url.rstrip("/")
    await set_menu_button()
    logging.info("📱 Mini App готовий: %s (кнопка «Домівка» біля поля введення в чаті з ботом)", WEBAPP_URL)


async def send_chat(uid: int, cid: int, text: str):
    """Надсилання повідомлення в чат (використовує Mini App). Повертає (msg_id, приховано_контакти) або None."""
    c = await db.one("select c.*, l.title, l.owner from convs c join listings l on l.id=c.listing_id where c.id=?", (cid,))
    text = text.strip()
    if not c or uid not in (c["guest"], c["owner"]) or not text:
        return None
    me = await db.one("select name from users where id=?", (uid,))
    to = c["guest"] if uid == c["owner"] else c["owner"]
    text, hidden = await clean_text(c, text)
    mid = await db.ex("insert into msgs(conv_id,sender,body) values(?,?,?)", (c["id"], uid, text[:2000]))
    await tell(to, f"💬 <b>{esc(me['name'])}</b> · {esc(c['title'])}\n\n{esc(text[:2000])}",
               [[("↩️ Відповісти", f"ch:{c['id']}")] + ([("📱 У додатку", "webapp:" + app_url(t="chats", c=c["id"]))] if WEBAPP_URL else [])])
    return mid, hidden


async def relay(chat: int, u: dict, text: str):
    c = await db.one("select c.*, l.title, l.owner from convs c join listings l on l.id=c.listing_id where c.id=?", (S[u["id"]]["cid"],))
    if not c:
        return await home(chat, u)
    to = c["guest"] if u["id"] == c["owner"] else c["owner"]
    text, hidden = await clean_text(c, text)
    await db.ex("insert into msgs(conv_id,sender,body) values(?,?,?)", (c["id"], u["id"], text[:2000]))
    ok = await tell(to, f"💬 <b>{esc(u['name'])}</b> · {esc(c['title'])}\n\n{esc(text[:2000])}",
                    [[("↩️ Відповісти", f"ch:{c['id']}")] + ([("📱 У додатку", "webapp:" + app_url(t="chats", c=c["id"]))] if WEBAPP_URL else [])])
    await say(chat, ("Надіслано ✓" if ok else "Збережено, але співрозмовник зараз не отримує повідомлення від бота.")
              + ("\n🔒 Контакти й посилання приховано до підтвердження бронювання: так безпечніше для обох сторін." if hidden else ""))


# ───────────────────────── майстер нового оголошення ─────────────────────────
STEPS = ["title", "property_type", "city", "district", "price_night", "price_month", "rooms", "guests", "descr", "photos"]
PROPERTY_TYPES = {"1": "apartment", "2": "house", "3": "room", "4": "commercial", "5": "garage", "6": "land", "7": "other"}
PROMPT = {"title": "Заголовок оголошення (5–120 символів):", "city": "Місто:", "district": "Район:", "price_night": "Ціна за ніч, ₴:",
          "price_month": "Ціна за місяць, ₴ (або «-», якщо на місяць не здаєте):", "rooms": "Кількість кімнат:", "guests": "Максимум гостей:",
          "descr": "Опишіть житло або натисніть кнопку, і AI напише опис за вас:", "photos": "Надішліть фото (до 6). Потім натисніть «Опублікувати»."}


async def ask_step(chat: int, step: str):
    if step == "property_type":
        return await say(chat, "Тип нерухомості — надішліть номер:\n1. Квартира\n2. Будинок\n3. Кімната\n4. Комерційна нерухомість\n5. Гараж\n6. Ділянка\n7. Інше")
    rows = {"descr": [[("✨ Написати AI", "nw:ai")]], "photos": [[("✅ Опублікувати", "nw:pub")]]}.get(step, [])
    await say(chat, PROMPT[step], rows + [[("✖ Скасувати", "m:m")]])


async def new_step(chat: int, u: dict, m: Message):
    s = S[u["id"]]
    step, t = s["step"], (m.text or "").strip()
    if step == "photos":
        if not m.photo:
            return await say(chat, "Надішліть фото або натисніть «Опублікувати».", [[("✅ Опублікувати", "nw:pub")]])
        if len(s["d"].setdefault("photos", [])) >= 6:
            return await say(chat, "Максимум 6 фото.")
        s["d"]["photos"].append(m.photo[-1].file_id)
        return await say(chat, f"Фото додано ({len(s['d']['photos'])}/6).", [[("✅ Опублікувати", "nw:pub")]])
    if not t:
        return
    if step == "property_type":
        value = PROPERTY_TYPES.get(t, t.lower())
        if value not in PROPERTY_TYPES.values():
            return await ask_step(chat, step)
        s["d"][step] = value
        s["step"] = "city"
        return await ask_step(chat, "city")
    if step in ("price_night", "price_month", "rooms", "guests"):
        if not (step == "price_month" and t == "-"):
            if not (t.isdigit() and int(t) > 0):
                return await say(chat, "Введіть ціле число більше нуля.")
            s["d"][step] = int(t)
    elif step == "title" and not 5 <= len(t) <= 120:
        return await say(chat, "Заголовок: від 5 до 120 символів.")
    else:
        s["d"][step] = t[:2000]
    s["step"] = STEPS[STEPS.index(step) + 1]
    if s["step"] == "price_night":
        peers = await db.q(f"select price_night from listings where city=? and {VIS}", (s["d"].get("city", ""),))
        if len(peers) >= 3:
            await say(chat, f"💡 Середня ціна за ніч у місті «{esc(s['d']['city'])}»: {uah(sum(r['price_night'] for r in peers) / len(peers))}. Конкурентна ціна дає більше бронювань.")
    await ask_step(chat, s["step"])


async def is_pro(uid: int) -> bool:
    if uid in ADMINS:
        return True
    return bool(await db.one("select 1 x from subs where user=? and until>=date('now')", (uid,)))


async def pro_until(uid: int) -> str:
    r = await db.one("select until from subs where user=? and until>=date('now')", (uid,))
    return r["until"] if r else ""


async def grant_pro(uid: int, days: int) -> str:
    """Продовжує Pro від найпізнішої з дат: сьогодні або поточний кінець підписки."""
    base = max(today(), await pro_until(uid) or today())
    until = addd(base, days)
    await db.ex("insert into subs(user,plan,until) values(?,?,?) on conflict(user) do update set until=excluded.until", (uid, "pro", until))
    return until


async def create_listing(u: dict, d: dict):
    """Створює оголошення (бот і Mini App). Повертає (lid, blocked) або (None, текст помилки)."""
    lim = PRO["max"] if await is_pro(u["id"]) else PRO["free"]
    if (await db.one("select count(*) n from listings where owner=? and blocked!=1", (u["id"],)))["n"] >= lim:
        return None, (f"Ліміт: до {lim} оголошень." if lim == PRO["max"] else f"Безкоштовно можна мати до {lim} оголошень. Для рієлторів і власників кількох квартир є тариф Pro (Кабінет → Домівка Pro).")
    blocked = 2 if APPROVAL and u["id"] not in ADMINS else 0
    lid = await db.ex("insert into listings(owner,title,city,district,price_night,price_month,rooms,guests,descr,photos,blocked) values(?,?,?,?,?,?,?,?,?,?,?)",
                      (u["id"], d["title"], d["city"], d["district"], d["price_night"], d.get("price_month"), d["rooms"], d["guests"], d.get("descr", ""), json.dumps(d.get("photos", [])), blocked))
    extra = {}
    extra["property_type"] = d.get("property_type") if d.get("property_type") in PROPERTY_TYPES.values() else "apartment"
    if d.get("amen"):
        extra["amen"] = ",".join(k for k in d["amen"] if k in feat.AMEN)
    if d.get("min_nights"):
        extra["min_nights"] = max(1, min(30, int(d["min_nights"])))
    if d.get("instant"):
        extra["instant"] = 1
    if d.get("dep_mode") in ("cash", "card", "trust"):
        extra["dep_mode"] = d["dep_mode"]
    if d.get("address"):
        extra["address"] = str(d["address"])[:500]
    for k, v in extra.items():
        await db.ex(f"update listings set {k}=? where id=?", (v, lid))
    for a in ADMINS:
        await tell(a, f"{'⏳ На модерацію' if blocked else '🆕 Нове оголошення'} #{lid}: {esc(d['title'])}\n{esc(d['city'])}, {esc(d['district'])} · {uah(d['price_night'])}",
                   [[("✅ Схвалити", f"ap:{lid}"), ("⛔ Заблокувати", f"ab:{lid}")], [("👁 Переглянути", f"lc:{lid}")]])
    if not blocked:
        await notify_alerts(lid)
        await growth.on_listing(u["id"], lid)
    asyncio.create_task(feat.check_photos(lid))
    return lid, blocked


async def publish(chat: int, u: dict):
    d = S[u["id"]]["d"]
    lid, blocked = await create_listing(u, d)
    if lid is None:
        return await say(chat, blocked)
    await home(chat, u, "✅ Оголошення опубліковано" if not blocked else "⏳ Оголошення на модерації: з'явиться в пошуку після схвалення.")
    await say(chat, "💡 Щоб виділитися: додайте зручності, геолокацію й спосіб оплати.",
              [[("🏷 Зручності", f"z:am:{lid}"), ("📍 Геолокація", f"z:gl:{lid}")], [("💳 Оплата й залог", f"z:dm:{lid}"), ("🛡 Верифікація", "z:vm")]])


async def listing_menu(chat: int, lid: int, owner: int, page: str = "main"):
    l = await db.one("select * from listings where id=? and owner=?", (lid, owner))
    if not l:
        return
    ra, rn = await rating(lid=lid)
    nb = (await db.one("select count(*) n from bookings where listing_id=? and status!='cancelled'", (lid,)))["n"]
    state = "⛔ заблоковано" if l["blocked"] == 1 else "⏳ на модерації" if l["blocked"] == 2 else "🟢 активне" if l["active"] else "⚪ сховане"
    head = (f"<b>{esc(l['title'])}</b> · {state}\n{uah(l['price_night'])}/ніч" + (f" · {uah(l['price_month'])}/міс" if l["price_month"] else "") + (f" · знижка {l['discount']}%" if l["discount"] else "")
            + f"\n👁 {l['views']} · 📅 {nb} · {stars(ra, rn)}" + (f" · 🔥 до {l['boost_until']}" if is_boosted(l) else ""))
    if page == "edit":
        rows = [[("💰 Ціна/ніч", f"ed:p:{lid}"), ("📆 Ціна/міс", f"ed:m:{lid}"), ("🏷 Знижка", f"ed:d:{lid}")],
                [("✏️ Назва", f"ed:t:{lid}"), ("📝 Опис", f"ed:s:{lid}"), ("✨ AI-опис", f"ed:a:{lid}")],
                [("📷 + Фото", f"ed:f:{lid}"), ("🧹 Прибрати фото", f"ed:c:{lid}")],
                [("🌙 Мін. ночей", f"ed:n:{lid}"), ("🔑 Адреса", f"ed:w:{lid}")],
                [("🏷 Зручності", f"z:am:{lid}"), ("📍 Геолокація", f"z:gl:{lid}"), ("🧾 Тарифи", f"z:tf:{lid}")],
                [(f"⚡ Миттєве: {'так' if l['instant'] else 'ні'}", f"ed:i:{lid}"), ("⬅ Назад", f"ml:{lid}")]]
    else:
        rows = [[("👁 Як бачать гості", f"lc:{lid}"), ("🔥 Просунути", f"bo:{lid}")],
                [("✏️ Редагувати", f"lm:edit:{lid}"), ("📅 Закрити дати", f"cal:block:{lid}:{today()[:7]}")],
                [("💳 Оплата й залог", f"z:dm:{lid}"), ("🙈 Сховати" if l["active"] else "👁 Показати", f"ed:v:{lid}")],
                [("🗑 Видалити", f"ed:x:{lid}"), ("⬅ До списку", "m:o")]]
    await say(chat, head, rows)


async def edit_step(chat: int, u: dict, m: Message):
    s = S[u["id"]]
    f, lid, t = s["field"], s["id"], (m.text or "").strip()
    own = await db.one("select * from listings where id=? and owner=?", (lid, u["id"]))
    if not own:
        return await home(chat, u)
    if f == "f":
        if not m.photo:
            return await say(chat, "Надішліть фото.")
        ph = (json.loads(own["photos"]) + [m.photo[-1].file_id])[:6]
        await db.ex("update listings set photos=? where id=?", (json.dumps(ph), lid))
        asyncio.create_task(feat.check_photos(lid))
        return await say(chat, f"Фото додано ({len(ph)}/6). Можна надіслати ще.", [[("⬅ До оголошення", f"ml:{lid}")]])
    col = {"p": "price_night", "m": "price_month", "d": "discount", "t": "title", "s": "descr", "n": "min_nights", "w": "address"}[f]
    if f in ("p", "m", "d", "n"):
        lo, hi = {"d": (0, 70), "n": (1, 30)}.get(f, (1, 10 ** 7))
        if not (t.isdigit() and lo <= int(t) <= hi):
            return await say(chat, f"Введіть число від {lo} до {hi}.")
        v = int(t)
    else:
        if f == "t" and not 5 <= len(t) <= 120:
            return await say(chat, "Назва: від 5 до 120 символів.")
        v = t[:2000]
    old = eff(own, False) if f in ("p", "d") else None
    await db.ex(f"update listings set {col}=? where id=?", (v, lid))
    if old is not None:
        await notify_price_drop(lid, old, eff(await db.one("select * from listings where id=?", (lid,)), False))
    S.pop(u["id"], None)
    await say(chat, "Збережено ✓", [[("⬅ До оголошення", f"ml:{lid}")]])


# ───────────────────────── відгуки, профіль господаря ─────────────────────────
async def reviews_page(chat: int, lid: int, page: int):
    l = await db.one("select title from listings where id=?", (lid,))
    rows = await db.q("select r.*, u.name from reviews r join users u on u.id=r.author where listing_id=? and r.kind='guest' order by r.id desc limit 6 offset ?", (lid, page * 5))
    ra, rn = await rating(lid=lid)
    txt = f"<b>{esc(l['title'] if l else '')}</b>\n{stars(ra, rn)}\n\n" + ("\n\n".join(f"{'⭐' * r['rating']} <b>{esc(r['name'])}</b>, {r['created'][:10]}\n{esc(r['text'])}" for r in rows[:5]) or "Відгуків поки немає. Їх можуть залишити лише гості після завершеного проживання.")
    nav = []
    if page:
        nav.append(("◀", f"rl:{lid}:{page - 1}"))
    if len(rows) > 5:
        nav.append(("▶", f"rl:{lid}:{page + 1}"))
    await say(chat, txt, [nav] if nav else [[("⬅ До оголошення", f"lc:{lid}")]])


async def host_profile(chat: int, hid: int):
    h = await db.one("select * from users where id=?", (hid,))
    if not h:
        return
    ha, hn = await rating(host=hid)
    ls = await db.q(f"select id,title from listings where owner=? and {VIS}", (hid,))
    done = (await db.one("select count(*) n from bookings b join listings l on l.id=b.listing_id where l.owner=? and b.status='confirmed' and b.co<=date('now')", (hid,)))["n"]
    rv = await db.q("select r.*, u.name from reviews r join users u on u.id=r.author where r.subject=? and r.kind='guest' order by r.id desc limit 3", (hid,))
    await say(chat, f"👤 <b>{esc(h['name'])}</b>{' 🏅 Супергосподар' if await is_super(hid) else ''}{' ✅ Верифікований' if h['verified'] == 2 else ''}\n{stars(ha, hn)}\n{'✅ Номер телефону підтверджено' if h['phone'] else 'Номер не підтверджено'}\n"
                    f"На платформі з {h['created'][:10]} · завершених проживань: {done}\n\n" + ("\n\n".join(f"{'⭐' * r['rating']} {esc(r['name'])}: {esc(r['text'])}" for r in rv) or "Відгуків поки немає."),
              [[(x["title"][:40], f"lc:{x['id']}")] for x in ls[:6]] + [[("🏠 Меню", "m:m")]])


# ───────────────────────── адмін ─────────────────────────
async def export(chat: int, name: str, sql: str):
    rows = await db.q(sql)
    if rows:
        b = io.StringIO()
        w = csv.DictWriter(b, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        await bot.send_document(chat, BufferedInputFile(("\ufeff" + b.getvalue()).encode("utf-8"), filename=f"{name}_{today()}.csv"))


async def admin_cb(c: CallbackQuery, uid: int, k: str, a: list):
    chat = c.message.chat.id
    if k == "a" and a[0] == "f":
        r = await db.one("select coalesce(sum(case when payout_status='hold' then deposit end),0) h, coalesce(sum(case when payout_status='due' then deposit-commission end),0) d,"
                         " coalesce(sum(case when payout_status='paid' then deposit-commission end),0) p, coalesce(sum(case when payout_status in ('paid','due','hold') then commission end),0) c,"
                         " sum(payout_status='refund_due') rf, sum(payout_status='frozen') fz from bookings")
        return await say(chat, f"💰 Фінанси (режим оплати: {PAYMODE})\nУтримується до заселення: {uah(r['h'])}\nДо виплати господарям: {uah(r['d'])}\nВиплачено: {uah(r['p'])}\n"
                               f"Комісія платформи: {uah(r['c'])}\nПовернень очікує: {r['rf'] or 0}\nСпорів: {r['fz'] or 0}",
                         [[("💸 До виплати", "a:pd"), ("↩️ Повернення", "a:pr")], [("⚠️ Спори", "a:ps"), ("⬅ Назад", "a:m")]])
    if k == "a" and a[0] == "pd":
        rows = await db.q("select b.id,b.deposit,b.commission,l.title,u.name,u.payout from bookings b join listings l on l.id=b.listing_id join users u on u.id=l.owner where b.payout_status='due'")
        if not rows:
            return await say(chat, "Немає виплат у черзі ✓", [[("⬅ Назад", "a:f")]])
        for r in rows:
            await say(chat, f"💸 #{r['id']} {esc(r['title'])}\nГосподар: {esc(r['name'])}\nСума до виплати: <b>{uah(r['deposit'] - r['commission'])}</b>\nРеквізити: {esc(r['payout']) or 'НЕ ВКАЗАНО'}", [[("✔ Виплачено", f"pp:{r['id']}")]])
        return
    if k == "a" and a[0] == "pr":
        rows = await db.q("select b.id,b.deposit,b.method,b.charge_id,l.title,u.name,u.phone from bookings b join listings l on l.id=b.listing_id join users u on u.id=b.guest where b.payout_status='refund_due'")
        if not rows:
            return await say(chat, "Немає повернень у черзі ✓", [[("⬅ Назад", "a:f")]])
        for r in rows:
            await say(chat, f"↩️ #{r['id']} {esc(r['title'])}\nГість: {esc(r['name'])} {esc(r['phone'])}\nПовернути: <b>{uah(r['deposit'])}</b> (спосіб оплати: {esc(r['method'])}, charge: {esc(r['charge_id'])})\nПовернення робиться в кабінеті платіжного провайдера або переказом.", [[("✔ Повернено", f"pf:{r['id']}")]])
        return
    if k == "a" and a[0] == "ps":
        rows = await db.q("select b.id,b.deposit,l.title from bookings b join listings l on l.id=b.listing_id where b.payout_status='frozen'")
        if not rows:
            return await say(chat, "Спорів немає ✓", [[("⬅ Назад", "a:f")]])
        for r in rows:
            await say(chat, f"⚠️ Спір #{r['id']} {esc(r['title'])}, застава {uah(r['deposit'])}", [[("💸 Виплатити господарю", f"pq:{r['id']}:r"), ("↩️ Повернути гостю", f"pq:{r['id']}:f")]])
        return
    if k == "pm":
        if a[1] == "1":
            ok = await mark_paid(int(a[0]), "manual")
            return await say(chat, "Оплату підтверджено, бронювання активне ✓" if ok else "Бронювання вже недоступне (час на оплату минув або воно скасоване).")
        bk = await db.one("select guest from bookings where id=?", (int(a[0]),))
        if bk:
            await tell(bk["guest"], f"Не вдалося знайти вашу оплату за бронювання #{a[0]}. Перевірте реквізити й надішліть чек ще раз.", [[("✅ Я оплатив(ла)", f"paid:{a[0]}")]])
        return await say(chat, "Гостю повідомлено.")
    if k in ("pp", "pf", "pq"):
        bid = int(a[0])
        b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
        if not b:
            return
        if k == "pp" and b["payout_status"] == "due":
            await db.ex("update bookings set payout_status='paid' where id=?", (bid,))
            await db.ex("insert into payments(kind,ref,user,amount,method) values('payout',?,?,?,'admin')", (bid, b["owner"], b["deposit"] - b["commission"]))
            await tell(b["owner"], f"💸 Виплату {uah(b['deposit'] - b['commission'])} за «{esc(b['title'])}» надіслано на ваші реквізити.")
            return await say(chat, "Позначено як виплачено ✓")
        if k == "pf" and b["payout_status"] == "refund_due":
            await db.ex("update bookings set payout_status='refunded' where id=?", (bid,))
            await tell(b["guest"], f"↩️ Заставу {uah(b['deposit'])} за «{esc(b['title'])}» повернено.")
            return await say(chat, "Позначено як повернено ✓")
        if k == "pq" and b["payout_status"] == "frozen":
            await db.ex("update bookings set payout_status=? where id=?", ("due" if a[1] == "r" else "refund_due", bid))
            return await say(chat, "Рішення зафіксовано. Виплата/повернення у відповідному списку в «Фінанси».")
        return await say(chat, "Дія вже виконана.")
    if k == "a" and a[0] == "m":
        return await say(chat, "🛡 Адмін-панель", [[("📊 Статистика", "a:s"), ("🚩 Скарги", "a:r")], [("⏳ На модерації", "a:p"), ("👥 Користувачі", "a:u")], [("💰 Фінанси", "a:f"), ("📣 Розсилка", "a:b")], [("🛡 Верифікації", "z:avl"), ("🧾 Претензії", "z:acl")], [("📤 Експорт CSV", "a:e"), ("🏠 Меню", "m:m")]])
    if k == "a":
        x = a[0]
        if x == "s":
            n = lambda t, w="1": db.one(f"select count(*) n from {t} where {w}")
            us, ho, li, bl, bk, pe, rv = (await n("users"))["n"], (await n("users", "role='host'"))["n"], (await n("listings"))["n"], (await n("listings", "blocked=1"))["n"], (await n("bookings"))["n"], (await n("bookings", "status='pending'"))["n"], (await n("reviews"))["n"]
            inc = (await db.one("select coalesce(sum(total),0) s from bookings where status='confirmed'"))["s"]
            pro_n = (await db.one("select count(*) n from subs where until>=date('now')"))["n"]
            stars = (await db.one("select coalesce(sum(amount),0) s from payments where method='stars'"))["s"]
            stars30 = (await db.one("select coalesce(sum(amount),0) s from payments where method='stars' and created>=datetime('now','-30 day')"))["s"]
            return await say(chat, f"📊 Користувачів: {us} (господарів {ho})\nОголошень: {li} (заблоковано {bl})\nБронювань: {bk} (очікують {pe})\nВідгуків: {rv}\nПідтверджено на суму: {uah(inc)}\n💎 Pro-підписок активних: {pro_n}\n⭐ Stars зароблено: {stars} (за 30 днів: {stars30})", [[("⬅ Назад", "a:m")]])
        if x == "r":
            rs = await db.q("select r.*, l.title, l.owner from reports r join listings l on l.id=r.listing_id where r.status='open' order by r.id limit 10")
            if not rs:
                return await say(chat, "Відкритих скарг немає ✓", [[("⬅ Назад", "a:m")]])
            for r in rs:
                await say(chat, f"🚩 #{r['id']} {esc(r['title'])}\nПричина: {esc(r['reason'])}", [[("⛔ Блокувати оголошення", f"ab:{r['listing_id']}"), ("👁", f"lc:{r['listing_id']}")], [("🚫 Забанити господаря", f"ub:{r['owner']}:1"), ("✖ Відхилити", f"rd:{r['id']}")]])
            return
        if x == "p":
            ps = await db.q("select id,title from listings where blocked=2 limit 10")
            if not ps:
                return await say(chat, "Немає оголошень на модерації.", [[("⬅ Назад", "a:m")]])
            for p in ps:
                await say(chat, f"⏳ #{p['id']} {esc(p['title'])}", [[("✅ Схвалити", f"ap:{p['id']}"), ("⛔ Блок", f"ab:{p['id']}"), ("👁", f"lc:{p['id']}")]])
            return
        if x == "u":
            us = await db.q("select * from users order by created desc limit 12")
            return await say(chat, "Останні користувачі:", [[(f"{'🚫' if u['banned'] else '✅'} {u['name'][:25]} ({u['role']})", f"ub:{u['id']}:{0 if u['banned'] else 1}")] for u in us] + [[("⬅ Назад", "a:m")]])
        if x == "b":
            S[uid] = {"flow": "bcast"}
            return await say(chat, "Надішліть текст розсилки для всіх користувачів (/menu — скасувати):")
        if x == "e":
            await export(chat, "users", "select id,name,username,phone,role,banned,created from users")
            await export(chat, "listings", "select id,owner,title,city,district,price_night,price_month,discount,active,blocked,views,created from listings")
            await export(chat, "bookings", "select * from bookings")
            await export(chat, "reviews", "select * from reviews")
            await export(chat, "payments", "select * from payments")
            return
    if k == "ab":
        await db.ex("update listings set blocked=1 where id=?", (int(a[0]),))
        await db.ex("update reports set status='done' where listing_id=?", (int(a[0]),))
        l = await db.one("select owner,title from listings where id=?", (int(a[0]),))
        if l:
            await tell(l["owner"], f"⛔ Ваше оголошення «{esc(l['title'])}» заблоковано адміністрацією.")
        return await say(chat, "Оголошення заблоковано ✓")
    if k == "ap":
        await db.ex("update listings set blocked=0 where id=?", (int(a[0]),))
        await notify_alerts(int(a[0]))
        l = await db.one("select owner,title from listings where id=?", (int(a[0]),))
        if l:
            await growth.on_listing(l["owner"], int(a[0]))
            await tell(l["owner"], f"✅ Оголошення «{esc(l['title'])}» схвалено й доступне гостям.")
        return await say(chat, "Схвалено ✓")
    if k == "rd":
        await db.ex("update reports set status='dismissed' where id=?", (int(a[0]),))
        return await say(chat, "Скаргу відхилено.")
    if k == "ub":
        t, v = int(a[0]), int(a[1])
        if t in ADMINS:
            return
        await db.ex("update users set banned=? where id=?", (v, t))
        if v:
            await db.ex("update listings set blocked=1 where owner=?", (t,))
        return await say(chat, "Користувача забанено, оголошення заблоковано ✓" if v else "Користувача розбанено ✓ (оголошення розблокуйте окремо).")


# ───────────────────────── обробка повідомлень ─────────────────────────
async def register(c: CallbackQuery, role: str):
    f = c.from_user
    await db.ex("insert or ignore into users(id,name,username,role) values(?,?,?,?)", (f.id, f.full_name, f.username, role))
    if S.get(f.id, {}).get("ref") and await growth.attach(f.id, S[f.id]["ref"]):
        await tell(f.id, f"🎁 Вас запросив друг: +{growth.PTS['welcome']} балів у Клубі вже нараховано!")
    await ask_phone(c.message.chat.id)


@dp.message()
async def on_message(m: Message):
    if m.chat.type != "private" or not m.from_user:
        return
    if await billing.successful(m):
        return
    if (m.text or "").split("@", 1)[0].strip() == "/paysupport":
        return await m.answer(billing.support_text())
    if (m.text or "").split("@", 1)[0].strip() == "/version":
        return await m.answer(f"Domivka {VERSION} · два маркети · /start")
    if await market_bot.on_message(m):
        return
    uid, chat, text = m.from_user.id, m.chat.id, (m.text or "").strip()
    try:
        u = await db.one("select * from users where id=?", (uid,))
        if text.startswith("/start"):
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            parts = arg.split("__")
            arg, ref = parts[0], None
            for p_ in [arg] + parts[1:]:
                if p_.startswith("r_") and p_[2:].isdigit():
                    ref = int(p_[2:])
            if arg.startswith("r_"):
                arg = ""
            if not u:
                S[uid] = {"deeplink": arg, "ref": ref}
                return await say(chat, "Вітаю в <b>Домівці</b>! Знайдіть квартиру на ніч чи на місяць або здайте свою, не виходячи з Telegram.\n\nХто ви?",
                                 [[("🔍 Шукаю житло", "role:guest")], [("🏠 Здаю житло", "role:host")]])
            if arg.startswith("l_") and arg[2:].isdigit():
                return await card(chat, uid, int(arg[2:]))
            if arg.startswith("w_") and arg[2:].isdigit():
                return await open_wishlist(chat, int(arg[2:]))
            if arg == "club" and u:
                return await club_screen(chat, u)
            if arg == "app" and u:
                return await open_app(chat, u)
            if arg == "vm" and u:
                return await feat.verify_menu(chat, u)
            if arg == "pr" and u:
                return await menu_cb(chat, u, "pr")
            if arg == "deals" and u:
                return await feat.my_deals(chat, uid)
            if arg.startswith("a_") and arg[2:].isdigit() and u:
                return await ask_host(chat, uid, int(arg[2:]))
            if arg.startswith("c_") and arg[2:].isdigit() and u:
                return await open_chat(chat, uid, int(arg[2:]))
            if arg.startswith("b_") and arg[2:].isdigit() and u:
                return await say(chat, "🔥 Просування оголошення: оберіть пакет «Топ».", [[("🔥 Обрати пакет", f"bo:{arg[2:]}")]])
        if not u:
            return await say(chat, "Натисніть /start, щоб почати.")
        if u["banned"]:
            return await say(chat, "Ваш акаунт заблоковано адміністрацією.")
        if m.contact and m.contact.user_id == uid:
            await db.ex("update users set phone=? where id=?", (m.contact.phone_number, uid))
            u["phone"] = m.contact.phone_number
            refu = await growth.on_phone(uid)
            if refu:
                await tell(refu, f"🎉 Ваш друг приєднався до Домівки: +{growth.PTS['ref_phone']} балів у Клубі!", [[("🎁 Мій Клуб", "cl:m")]])
            await bot.send_message(chat, "Номер збережено ✓", reply_markup=ReplyKeyboardRemove())
            dl = S.get(uid, {}).get("deeplink", "")
            if dl.startswith("w_") and dl[2:].isdigit():
                return await open_wishlist(chat, int(dl[2:]))
            if dl.startswith("l_") and dl[2:].isdigit():
                return await card(chat, uid, int(dl[2:]))
            return await home(chat, u)
        if text == "Пропустити":
            await bot.send_message(chat, "Добре, номер можна додати пізніше (він потрібен для бронювання й оголошень).", reply_markup=ReplyKeyboardRemove())
            return await home(chat, u)
        if m.successful_payment:
            sp = m.successful_payment
            pl = sp.invoice_payload
            ch = sp.provider_payment_charge_id or sp.telegram_payment_charge_id
            if pl.startswith("bk:"):
                await mark_paid(int(pl[3:]), "telegram", ch)
            elif pl.startswith("pro:"):
                days = int(pl[4:])
                until = await grant_pro(uid, days)
                await db.ex("insert into payments(kind,ref,user,amount,method,charge_id) values('pro',?,?,?,?,?)", (days, uid, sp.total_amount, "stars", ch))
                await say(chat, f"💎 Домівка Pro активовано до {until}. Дякуємо! Тепер доступно до {PRO['max']} оголошень, значок Pro та знижка на «Топ».")
                for ad in ADMINS:
                    await tell(ad, f"💎 Нова підписка Pro: {esc(u['name'])} · {days} дн. · {sp.total_amount} ⭐")
            elif pl.startswith("bo:"):
                _, lid, days = pl.split(":")
                until = await apply_boost(int(lid), int(days))
                xtr = sp.currency == "XTR"
                await db.ex("insert into payments(kind,ref,user,amount,method,charge_id) values('boost',?,?,?,?,?)", (int(lid), uid, sp.total_amount if xtr else sp.total_amount // 100, "stars" if xtr else "telegram", ch))
                await say(chat, f"🔥 Оплачено! Оголошення просувається до {until}.")
            return
        if text.startswith(("/menu", "/start")):
            return await home(chat, u)
        if text == "/app":
            return await open_app(chat, u)
        if text == "/help":
            return await help_(chat)
        s = S.get(uid, {})
        fl = s.get("flow")
        if await feat.on_msg(m, u, s, text):
            return
        if fl == "new":
            return await new_step(chat, u, m)
        if fl == "edit":
            return await edit_step(chat, u, m)
        if fl == "proof" and (m.photo or m.document):
            bid = s["bid"]
            S.pop(uid, None)
            if not ADMINS:
                return await say(chat, "Адміністратор не налаштований (ADMIN_IDS). Зверніться до організатора сервісу.")
            cap = f"💳 Чек про оплату бронювання #{bid} від {esc(u['name'])} {esc(u['phone'])}"
            for ad in ADMINS:
                try:
                    if m.photo:
                        await bot.send_photo(ad, m.photo[-1].file_id, caption=cap, reply_markup=kb([[("✅ Оплату отримано", f"pm:{bid}:1"), ("✖ Не знайшов", f"pm:{bid}:0")]]))
                    else:
                        await bot.send_document(ad, m.document.file_id, caption=cap, reply_markup=kb([[("✅ Оплату отримано", f"pm:{bid}:1"), ("✖ Не знайшов", f"pm:{bid}:0")]]))
                except Exception:
                    logging.warning("не вдалося надіслати чек адміну %s", ad)
            return await say(chat, "Чек надіслано на перевірку ✓ Я повідомлю, щойно оплату буде підтверджено.")
        if fl == "payout" and text:
            if not 8 <= len(text) <= 60:
                return await say(chat, "Надішліть номер картки або IBAN (8–60 символів).")
            await db.ex("update users set payout=? where id=?", (text, uid))
            return await home(chat, u, "Реквізити для виплат збережено ✓")
        if fl == "chat" and text:
            return await relay(chat, u, text)
        if fl == "revtext" and text:
            if text != "/skip":
                await db.ex("update reviews set text=? where id=?", (text[:800], s["rid"]))
            S.pop(uid, None)
            return await home(chat, u, "Дякуємо за відгук! ⭐")
        if fl == "bcast" and uid in ADMINS and text:
            S.pop(uid, None)
            n = 0
            for r in await db.q("select id from users where banned=0"):
                n += await tell(r["id"], text)
                await asyncio.sleep(0.05)
            return await say(chat, f"Розіслано: {n}")
        if text and not text.startswith("/"):
            return await run_search(chat, uid, await ai_filters(text))
    except Exception:
        logging.exception("помилка в on_message")
        await say(chat, "Сталася помилка. Спробуйте ще раз або натисніть /menu.")


async def help_(chat: int):
    await say(chat, "<b>Як користуватися</b>\n• Напишіть, що шукаєте, і я підберу варіанти.\n• Обирайте дати в календарі: зайняті дні відмічені ❌.\n• Після підтвердження господар надішле контакти.\n"
                    "• Після проживання ви зможете залишити відгук.\n• Господарям: ➕ Нове, ціни, знижки, фото, закриття дат.\n• Меню завжди тут: /menu", [[("🏠 Меню", "m:m")]])


@dp.pre_checkout_query()
async def on_pre(q: PreCheckoutQuery):
    if await billing.pre_checkout(q):
        return
    ok = False
    if q.invoice_payload.startswith("bk:"):
        b = await db.one("select * from bookings where id=?", (int(q.invoice_payload[3:]),))
        ok = bool(b and b["status"] == "awaiting_payment" and b["guest"] == q.from_user.id)
    elif q.invoice_payload.startswith("bo:"):
        ok = True
    elif q.invoice_payload.startswith("pro:"):
        ok = int(q.invoice_payload[4:] or 0) in PRO["stars"]
    await q.answer(ok=ok, error_message=None if ok else "Бронювання вже недоступне або час на оплату минув.")


@dp.callback_query()
async def on_cb(c: CallbackQuery):
    if await market_bot.on_callback(c):
        return
    await c.answer()
    chat, uid = c.message.chat.id, c.from_user.id
    try:
        k, *a = c.data.split(":")
        if k == "x":
            return
        if k == "role":
            return await register(c, a[0])
        u = await db.one("select * from users where id=?", (uid,))
        if not u:
            return await say(chat, "Натисніть /start.")
        if u["banned"]:
            return await say(chat, "Ваш акаунт заблоковано адміністрацією.")
        s = S.get(uid, {})
        if k == "z":
            return await feat.cb(c, u, a)
        if k in ("a", "ab", "ap", "rd", "ub", "pp", "pf", "pq", "pm"):
            if uid not in ADMINS:
                return
            return await admin_cb(c, uid, k, a)
        if k == "m":
            return await menu_cb(chat, u, a[0])
        if k == "sc":
            city = "" if a[0] == "a" else s.get("cities", [])[int(a[0])]
            ds = [r["district"] for r in await db.q(f"select distinct district from listings where {VIS}" + (" and city=?" if city else "") + " order by 1", (city,) if city else ())]
            S[uid] = {"flow": "pick", "city": city, "districts": ds}
            return await say(chat, "Оберіть район:", [[(d, f"sd:{i}")] for i, d in enumerate(ds)] + [[("Усі райони", "sd:a")]])
        if k == "sd":
            return await run_search(chat, uid, {"city": s.get("city", ""), "district": "" if a[0] == "a" else s["districts"][int(a[0])], "month": False, "max": 0, "rooms": 0, "guests": 0})
        if k in ("fm", "fs", "fx") and s.get("f"):
            f = s["f"]
            if k == "fm":
                f["month"] = not f["month"]
            elif k == "fs":
                f["sort"] = {"price": "rating", "rating": "new", "new": "price", "dist": "price"}[f.get("sort") or ("dist" if f.get("near") else "price")]
            else:
                f["max"] = int(a[0])
            return await run_search(chat, uid, f)
        if k == "r" and s.get("ids"):
            i = int(a[0])
            if 0 <= i < len(s["ids"]):
                try:
                    await c.message.delete()
                except Exception:
                    pass
                return await card(chat, uid, s["ids"][i], (i, len(s["ids"])), s["f"]["month"], s["avg"])
            return
        if k == "lc":
            return await card(chat, uid, int(a[0]))
        if k == "ph":
            l = await db.one("select photos from listings where id=?", (int(a[0]),))
            if l:
                await bot.send_media_group(chat, [InputMediaPhoto(media=p) for p in json.loads(l["photos"])[:10]])
            return
        if k == "fv":
            lid = int(a[0])
            if await db.one("select 1 from favs where user=? and listing_id=?", (uid, lid)):
                await db.ex("delete from favs where user=? and listing_id=?", (uid, lid))
            else:
                await db.ex("insert into favs values(?,?)", (uid, lid))
            return await say(chat, "Обране оновлено ✓")
        if k == "rl":
            return await reviews_page(chat, int(a[0]), int(a[1]))
        if k == "hp":
            return await host_profile(chat, int(a[0]))
        if k == "rp":
            return await say(chat, "Що не так з оголошенням?", [[("Шахрайство", f"rr:{a[0]}:0")], [("Фото не відповідають", f"rr:{a[0]}:1")], [("Невірна ціна/умови", f"rr:{a[0]}:2")], [("Інше", f"rr:{a[0]}:3")]])
        if k == "rr":
            await db.ex("insert into reports(listing_id,reporter,reason) values(?,?,?)", (int(a[0]), uid, ["Шахрайство", "Фото не відповідають", "Невірна ціна/умови", "Інше"][int(a[1])]))
            for ad in ADMINS:
                await tell(ad, f"🚩 Нова скарга на оголошення #{a[0]}", [[("Відкрити скарги", "a:r")]])
            return await say(chat, "Дякуємо! Адміністрація перевірить оголошення.")
        if k == "cl":
            if a[0] == "m":
                return await club_screen(chat, u)
            if a[0] == "rw":
                return await say(chat, "🎟 <b>Обмін балів</b>: просування вашого оголошення.", [[(f"{t} · {c} балів", f"cl:pk:{rid}")] for rid, (t, c) in growth.REWARDS.items()] + [[("⬅ Клуб", "cl:m")]])
            if a[0] == "pk":
                mine = await db.q("select id,title from listings where owner=? and active=1 and blocked=0 limit 8", (uid,))
                if not mine:
                    return await say(chat, "Бали обмінюються на просування вашого оголошення. Спершу додайте його: кабінет господаря → ➕.", [[("⬅ Клуб", "cl:m")]])
                return await say(chat, "Для якого оголошення?", [[(x["title"][:40], f"cl:go:{a[1]}:{x['id']}")] for x in mine] + [[("⬅ Клуб", "cl:m")]])
            if a[0] == "go":
                _, msg = await growth.redeem(uid, a[1], int(a[2]), apply_boost)
                return await say(chat, msg, [[("🎁 Клуб", "cl:m")]])
            return
        if k == "mo":
            return await more_listing(chat, int(a[0]), uid)
        if k == "cx":
            try:
                await c.message.delete()
            except Exception:
                pass
            return
        if k == "sf" and s.get("f"):
            return await say(chat, "🎛 <b>Фільтри пошуку</b>", frows(s["f"]))
        if k == "lm":
            return await listing_menu(chat, int(a[1]), uid, a[0])
        if k == "hw":
            bx = await db.one("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.status='awaiting_payment'", (int(a[0]), uid))
            if not bx:
                return await say(chat, "Бронювання вже не очікує оплати.")
            return await ask_payment(bx, bx)
        if k == "ask":
            return await ask_host(chat, uid, int(a[0]))
        if k == "ch":
            return await open_chat(chat, uid, int(a[0]))
        if k == "bk":
            if not u["phone"]:
                S[uid] = {"deeplink": f"l_{a[0]}"}
                return await ask_phone(chat)
            S[uid] = {"flow": "book", "lid": int(a[0])}
            return await calendar(chat, int(a[0]), today()[:7])
        if k == "cal":
            mode, lid, ym = a[0], int(a[1]), a[2]
            if mode == "block" and not await db.one("select 1 from listings where id=? and owner=?", (lid, uid)):
                return
            return await calendar(chat, lid, ym, s.get("ci") if mode == "book" and s.get("flow") == "book" else None, mode, c.message)
        if k == "d" and s.get("flow") == "book" and s["lid"] == int(a[0]):
            if not s.get("ci"):
                s["ci"] = a[1]
                return await calendar(chat, s["lid"], a[1][:7], a[1], "book", c.message)
            l = await db.one("select min_nights from listings where id=?", (s["lid"],))
            if (dt.date.fromisoformat(a[1]) - dt.date.fromisoformat(s["ci"])).days < l["min_nights"]:
                return await say(chat, f"Мінімальний термін проживання: {l['min_nights']} ноч. Оберіть пізнішу дату виїзду.")
            s["co"] = a[1]
            return await confirm_booking(chat, uid)
        if k == "bg" and s.get("co"):
            s["guests"] = int(a[0])
            return await confirm_booking(chat, uid)
        if k == "bf":
            return await submit_booking(chat, u)
        if k == "hb":
            lid, d = int(a[0]), a[1]
            if not await db.one("select 1 from listings where id=? and owner=?", (lid, uid)):
                return
            if await db.one("select 1 from blocks where listing_id=? and d=?", (lid, d)):
                await db.ex("delete from blocks where listing_id=? and d=?", (lid, d))
            elif d not in await db.occupied(lid):
                await db.ex("insert into blocks values(?,?)", (lid, d))
            return await calendar(chat, lid, d[:7], None, "block", c.message)
        if k == "bs":
            _, msg, rows = await decide_booking(uid, int(a[0]), a[1] == "c")
            return await say(chat, msg, rows)
        if k == "pay":
            b = await db.one("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.id=?", (int(a[0]),))
            if not b or b["guest"] != uid or b["status"] != "awaiting_payment":
                return await say(chat, "Бронювання недоступне або час на оплату минув.")
            return await bot.send_invoice(chat, title=b["title"][:32], description=f"Застава за бронювання #{b['id']}: {b['ci']} — {b['co']}", payload=f"bk:{b['id']}",
                                          provider_token=PAY_TOKEN, currency="UAH", prices=[LabeledPrice(label="Застава", amount=deposit_of(b["total"]) * 100)])
        if k == "paid":
            b = await db.one("select * from bookings where id=? and guest=? and status='awaiting_payment'", (int(a[0]), uid))
            if not b:
                return await say(chat, "Бронювання недоступне або час на оплату минув.")
            S[uid] = {"flow": "proof", "bid": b["id"]}
            return await say(chat, "Надішліть скріншот або фото квитанції про оплату (одним повідомленням).", [[("✖ Скасувати", "m:m")]])
        if k == "ci":
            b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.paid=1 and b.payout_status='hold'", (int(a[0]), uid))
            if not b:
                return
            if a[1] == "1":
                await release(b["id"])
                return await say(chat, "Дякуємо! Гарного проживання 🏡")
            await db.ex("update bookings set payout_status='frozen' where id=?", (b["id"],))
            await tell(b["owner"], f"⚠️ Гість повідомив про проблему із заселенням: {esc(b['title'])}. Виплата призупинена, адміністрація зв'яжеться з вами.")
            for ad in ADMINS:
                await tell(ad, f"⚠️ Спір #{b['id']}: {esc(b['title'])}", [[("⚠️ Спори", "a:ps")]])
            return await say(chat, "Дякуємо, адміністрація розбереться й зв'яжеться з вами найближчим часом.")
        if k == "gp" and (u["role"] == "host" or uid in ADMINS):
            return await guest_profile(chat, int(a[0]))
        if k == "hc":
            if await host_cancel(uid, int(a[0])) is None:
                return
            return await say(chat, "Бронювання скасовано.")
        if k == "bc":
            note = await cancel_booking(uid, int(a[0]))
            if note is None:
                return
            return await say(chat, "Бронювання скасовано." + note, [[("🏠 Меню", "m:m")]])
        if k == "rv":
            rid = await leave_review(uid, int(a[0]), int(a[1]))
            if not rid:
                return await say(chat, "Відгук залишити неможливо (або вже залишено).")
            S[uid] = {"flow": "revtext", "rid": rid}
            return await say(chat, "Дякуємо! Додайте кілька слів про проживання (або /skip):")
        if k == "hv":
            b = await db.one("select b.*, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and l.owner=? and b.status='confirmed' and b.co<=date('now')", (int(a[0]), uid))
            if not b or await db.one("select 1 from reviews where booking_id=? and kind='host'", (b["id"],)):
                return await say(chat, "Відгук залишити неможливо (або вже залишено).")
            rid = await db.ex("insert into reviews(booking_id,kind,listing_id,author,subject,rating) values(?,'host',?,?,?,?)", (b["id"], b["listing_id"], uid, b["guest"], int(a[1])))
            S[uid] = {"flow": "revtext", "rid": rid}
            await tell(b["guest"], f"⭐ Господар залишив відгук про вас: {'⭐' * int(a[1])}")
            return await say(chat, "Дякуємо! Додайте кілька слів про гостя (або /skip):")
        if k == "bo":
            lid = int(a[0])
            if not await db.one("select 1 from listings where id=? and owner=?", (lid, uid)):
                return
            rows = [[(f"{d} дн. · {boost_label(d)}", f"bo2:{lid}:{d}")] for d in (3, 7, 30)] if paid_boost() else [[(f"🎁 Безкоштовно на {BOOST_FREE_DAYS} дн.", f"bo2:{lid}:0")]]
            return await say(chat, "🔥 <b>Просування</b>: оголошення піднімається вгору видачі, отримує значок «Топ» і потрапляє в розділ «🔥 Топ».", rows + [[("⬅ Назад", f"ml:{lid}")]])
        if k == "bo2":
            lid, d = int(a[0]), int(a[1])
            l = await db.one("select * from listings where id=? and owner=?", (lid, uid))
            if not l:
                return
            if d == 0 and not paid_boost():
                if l["free_boost_at"] and addd(l["free_boost_at"], 7) > today():
                    return await say(chat, f"Наступне безкоштовне просування буде доступне {addd(l['free_boost_at'], 7)}.")
                until = await apply_boost(lid, BOOST_FREE_DAYS)
                await db.ex("update listings set free_boost_at=? where id=?", (today(), lid))
                return await say(chat, f"🔥 Готово! Оголошення просувається до {until}.", [[("⬅ До оголошення", f"ml:{lid}")]])
            if d in (3, 7, 30) and paid_boost():
                return await boost_invoice(chat, l, d)
            return
        if k == "al" and s.get("f"):
            f = s["f"]
            if (await db.one("select count(*) n from alerts where user=?", (uid,)))["n"] >= 5:
                return await say(chat, "Максимум 5 підписок. Видаліть зайву: меню → 🔔 Підписки.")
            await db.ex("insert into alerts(user,city,district,max,month) values(?,?,?,?,?)", (uid, f.get("city", ""), f.get("district", ""), f.get("max") or 0, int(bool(f.get("month")))))
            return await say(chat, "🔔 Готово! Повідомлю, щойно з'явиться підходяще оголошення.")
        if k == "ad":
            await db.ex("delete from alerts where id=? and user=?", (int(a[0]), uid))
            return await say(chat, "Підписку видалено.")
        if k == "ml":
            return await listing_menu(chat, int(a[0]), uid)
        if k == "ed":
            f, lid = a[0], int(a[1])
            l = await db.one("select * from listings where id=? and owner=?", (lid, uid))
            if not l:
                return
            if f == "i":
                await db.ex("update listings set instant=? where id=?", (0 if l["instant"] else 1, lid))
                return await listing_menu(chat, lid, uid)
            if f == "v":
                await db.ex("update listings set active=? where id=?", (0 if l["active"] else 1, lid))
                return await listing_menu(chat, lid, uid)
            if f == "x":
                return await say(chat, "Видалити оголошення назавжди?", [[("Так, видалити", f"ed:y:{lid}"), ("Ні", f"ml:{lid}")]])
            if f == "y":
                await db.ex("delete from listings where id=?", (lid,))
                return await home(chat, u, "Оголошення видалено.")
            if f == "c":
                await db.ex("update listings set photos='[]' where id=?", (lid,))
                return await listing_menu(chat, lid, uid)
            if f == "a":
                t = await make_descr(l)
                await db.ex("update listings set descr=? where id=?", (t, lid))
                return await say(chat, f"Новий опис:\n\n{esc(t)}", [[("⬅ До оголошення", f"ml:{lid}")]])
            S[uid] = {"flow": "edit", "field": f, "id": lid}
            return await say(chat, {"p": "Нова ціна за ніч, ₴:", "m": "Нова ціна за місяць, ₴:", "d": "Знижка у відсотках (0–70):", "t": "Нова назва:", "s": "Надішліть новий опис:", "f": "Надішліть фото:", "n": "Мінімальна кількість ночей (1–30):",
                                    "w": "Надішліть адресу та інструкції заселення (код замка, поверх тощо). Гість побачить їх лише після підтвердження бронювання:"}[f], [[("✖ Скасувати", f"ml:{lid}")]])
        if k == "nw" and s.get("flow") == "new":
            if a[0] == "ai":
                s["d"]["descr"] = await make_descr(s["d"])
                s["step"] = "photos"
                await say(chat, f"Опис:\n\n{esc(s['d']['descr'])}")
                return await ask_step(chat, "photos")
            if a[0] == "pub":
                return await publish(chat, u)
    except Exception:
        logging.exception("помилка в on_cb")
        await say(chat, "Сталася помилка. Спробуйте ще раз або натисніть /menu.")


async def menu_cb(chat: int, u: dict, x: str):
    uid = u["id"]
    if x == "m":
        return await home(chat, u)
    if x == "?":
        return await help_(chat)
    if x == "more":
        return await say(chat, "⋯ <b>Ще</b>", more_rows(u))
    if x == "host":
        if u["role"] != "host":
            return await menu_cb(chat, u, "h")
        return await host_menu(chat, u)
    if x == "h":
        await db.ex("update users set role='host' where id=?", (uid,))
        u["role"] = "host"
        return await host_menu(chat, u, "Тепер ви господар ✓ Додайте перше оголошення.")
    if x == "s":
        cs = [r["city"] for r in await db.q(f"select distinct city from listings where {VIS} order by 1")]
        S[uid] = {"flow": "pick", "cities": cs}
        return await say(chat, "Оберіть місто (або просто напишіть запит):", [[(c, f"sc:{i}")] for i, c in enumerate(cs)] + [[("🌍 Усі міста", "sc:a")]])
    if x == "top":
        return await run_search(chat, uid, {"city": "", "district": "", "month": False, "max": 0, "rooms": 0, "guests": 0, "top": True})
    if x == "al":
        al = await db.q("select * from alerts where user=?", (uid,))
        lab = lambda r: f"🗑 {r['city'] or 'усі міста'}, {r['district'] or 'усі райони'}" + (f", до {r['max']}" if r["max"] else "")
        return await say(chat, "Ваші підписки. Я повідомлю про нові оголошення:" if al else "Підписок немає. У пошуку натисніть «🔔 Стежити за новими».", [[(lab(r), f"ad:{r['id']}")] for r in al] + [[("🏠 Меню", "m:m")]])
    if x == "f":
        ls = await db.q(f"select l.id,l.title from favs f join listings l on l.id=f.listing_id where f.user=? and l.{VIS.replace(' and ', ' and l.')}", (uid,))
        return await say(chat, "Ваше обране:" if ls else "В обраному поки порожньо.", [[(l["title"][:50], f"lc:{l['id']}")] for l in ls] + [[("🏠 Меню", "m:m")]])
    if x == "t":
        bs = await db.q("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.guest=? order by b.id desc limit 10", (uid,))
        if not bs:
            return await say(chat, "Бронювань ще немає.", [[("🔍 Знайти житло", "m:s")]])
        st = {"pending": "⏳ очікує відповіді господаря", "awaiting_payment": "💳 очікує оплати", "confirmed": "✅ підтверджено", "cancelled": "❌ скасовано"}
        for b in bs:
            stt = st.get(b["status"], b["status"])
            r = []
            if b["status"] == "awaiting_payment":
                if b.get("method") == "claimed":
                    stt = "🔎 господар перевіряє оплату"
                else:
                    r.append(("💳 Як сплатити", f"hw:{b['id']}"))
                r.append(("✖ Скасувати", f"bc:{b['id']}"))
            elif b["status"] == "confirmed" and b["co"] <= today() and not await db.one("select 1 from reviews where booking_id=? and kind='guest'", (b["id"],)):
                r = [("⭐" * i, f"rv:{b['id']}:{i}") for i in (1, 3, 5)]
            elif b["status"] != "cancelled":
                r.append(("💬 Господарю", f"ask:{b['listing_id']}"))
                if b["co"] > today():
                    r.append(("✖ Скасувати", f"bc:{b['id']}"))
            await say(chat, f"<b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']} · {uah(b['total'])}\n{stt}", [r] if r else None)
        return
    if x == "c":
        g = await db.q("select c.id, l.title, 'g' k from convs c join listings l on l.id=c.listing_id where c.guest=?", (uid,))
        h = await db.q("select c.id, l.title, 'h' k from convs c join listings l on l.id=c.listing_id where l.owner=?", (uid,))
        return await say(chat, "Ваші чати:" if g or h else "Чатів ще немає. Натисніть «Запитати» в оголошенні.", [[(f"{'💬' if c['k'] == 'g' else '📥'} {c['title']}"[:55], f"ch:{c['id']}")] for c in g + h] + [[("🏠 Меню", "m:m")]])
    if u["role"] != "host":
        return
    if x == "o":
        ls = await db.q("select id,title,active,blocked from listings where owner=? order by id desc", (uid,))
        return await say(chat, "Ваші оголошення:" if ls else "Оголошень ще немає.", [[(f"{'⛔' if l['blocked'] == 1 else '⏳' if l['blocked'] == 2 else '🟢' if l['active'] else '⚪'} {l['title']}"[:55], f"ml:{l['id']}")] for l in ls] + [[("➕ Нове", "m:n")]])
    if x == "n":
        if not u["phone"]:
            return await ask_phone(chat)
        S[uid] = {"flow": "new", "step": "title", "d": {}}
        return await ask_step(chat, "title")
    if x == "pr":
        S[uid] = {"flow": "payout"}
        cur = u.get("payout") or ""
        return await say(chat, "Надішліть картку або IBAN, на які гості переказуватимуть передоплату <b>напряму вам</b>. Гість побачить їх лише після того, як ви підтвердите заявку; платформа ці гроші не приймає.\n"
                         + (f"Поточні: ...{esc(cur[-4:])}" if cur else "Зараз не вказано."), [[("✖ Скасувати", "m:m")]])
    if x == "r":
        bs = await db.q("select b.*, l.title, u.name, u.phone from bookings b join listings l on l.id=b.listing_id join users u on u.id=b.guest where l.owner=? and b.status='pending'", (uid,))
        up = await db.q("select b.*, l.title, u.name from bookings b join listings l on l.id=b.listing_id join users u on u.id=b.guest where l.owner=? and b.status='confirmed' and b.co>=date('now') order by b.ci", (uid,))
        if not bs and not up:
            return await say(chat, "Нових заявок і майбутніх бронювань немає.")
        for b in bs:
            await say(chat, f"🏠 <b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']} · {uah(b['total'])}\n👤 {esc(b['name'])} {esc(b['phone'])}\n{await guest_line(b['guest'])}\n⏳ Відповісти до {b['due_by']}",
                      [[("✅ Підтвердити", f"bs:{b['id']}:c"), ("❌ Відхилити", f"bs:{b['id']}:x")], [("👤 Профіль гостя", f"gp:{b['guest']}")]])
        for b in up:
            await say(chat, f"✅ <b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']} · {uah(b['total'])} · {esc(b['name'])}" + (" · 💳 оплачено" if b["paid"] else ""), [[("✖ Скасувати бронювання", f"hc:{b['id']}")]])
        return
    if x == "i":
        r = await db.one("select count(*) n, coalesce(sum(views),0) v from listings where owner=?", (uid,))
        b = await db.one("select count(*) n, coalesce(sum(total),0) s from bookings b join listings l on l.id=b.listing_id where l.owner=? and b.status='confirmed'", (uid,))
        ha, hn = await rating(host=uid)
        return await say(chat, f"📊 Оголошень: {r['n']} · переглядів: {r['v']}\nПідтверджених бронювань: {b['n']} на {uah(b['s'])}\nРейтинг: {stars(ha, hn)}{' · 🏅 Супергосподар' if await is_super(uid) else ' (супергосподар: рейтинг від 4.7 і мінімум 3 відгуки)'}", [[("🏠 Меню", "m:m")]])


# ───────────────────────── фонові задачі ─────────────────────────
async def background():
    last_backup = ""
    while True:
        try:
            for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.ci=? and b.rem=0", (addd(today(), 1),)):
                await tell(b["guest"], f"⏰ Завтра заїзд: <b>{esc(b['title'])}</b>, {b['ci']}. Гарної подорожі!")
                await tell(b["owner"], f"⏰ Завтра заїзд гостя: {esc(b['title'])}, {b['ci']}.")
                await db.ex("update bookings set rem=1 where id=?", (b["id"],))
            for b in await db.q("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.co<=? and b.askrev=0", (today(),)):
                await tell(b["guest"], f"Як вам проживання: <b>{esc(b['title'])}</b>? Оцініть, це допоможе іншим гостям:", [[(f"{'⭐' * i}", f"rv:{b['id']}:{i}") for i in (1, 2, 3, 4, 5)]])
                await db.ex("update bookings set askrev=1 where id=?", (b["id"],))
            nows = now_s()
            for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status in ('pending','awaiting_payment') and b.due_by!='' and b.due_by<? and coalesce(b.claim_at,'')=''", (nows,)):
                await db.ex("update bookings set status='cancelled', due_by='' where id=?", (b["id"],))
                why = "господар не відповів вчасно" if b["status"] == "pending" else "час на оплату минув"
                await tell(b["guest"], f"⌛ Заявку на «{esc(b['title'])}» скасовано: {why}. Дати знову вільні.", [[("🔍 Шукати інше", "m:s")]])
                await tell(b["owner"], f"⌛ Заявку на «{esc(b['title'])}» ({b['ci']} → {b['co']}) скасовано: {why}.")
            for b in await db.q("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.paid=1 and b.payout_status='hold' and b.ci<=? and b.cin_asked=0", (today(),)):
                await tell(b["guest"], f"🏡 Ви заселилися в «{esc(b['title'])}»? Підтвердіть, і господар отримає виплату.", [[("✅ Так, все добре", f"ci:{b['id']}:1"), ("⚠️ Є проблема", f"ci:{b['id']}:0")]])
                await db.ex("update bookings set cin_asked=1 where id=?", (b["id"],))
            for b in await db.q("select id from bookings where paid=1 and payout_status='hold' and ci<=?", (addd(today(), -1),)):
                await release(b["id"])     # гість не заперечив протягом доби після заїзду
            for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.co<=? and b.askrevh=0", (today(),)):
                await tell(b["owner"], f"Як пройшло проживання гостя в «{esc(b['title'])}»? Оцініть гостя:", [[(f"{'⭐' * i}", f"hv:{b['id']}:{i}") for i in (1, 2, 3, 4, 5)]])
                await db.ex("update bookings set askrevh=1 where id=?", (b["id"],))
            await feat.tick()
            for b in await db.q("select b.id, b.guest, l.owner, l.title from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.co<=? and b.pts_done=0", (today(),)):
                r = await growth.on_stay(b)
                await db.ex("update bookings set pts_done=1 where id=?", (b["id"],))
                if r.get("guest"):
                    await tell(b["guest"], f"🎉 Проживання завершено: +{growth.PTS['stay']} балів у Клубі. Залиште відгук і отримайте ще +{growth.PTS['review']}!", [[("🎁 Мій Клуб", "cl:m")]])
                if r.get("host"):
                    await tell(b["owner"], f"🎉 Гість виїхав: +{growth.PTS['host_stay']} балів у Клубі!")
                if r.get("ref"):
                    await tell(r["ref"], f"🎉 Ваш друг завершив проживання: +{growth.PTS['ref_stay']} балів!")
            if last_backup != today():
                (DATA / "backups").mkdir(exist_ok=True)
                # Online backup includes committed WAL data consistently.
                async with db.aiosqlite.connect(str(DATA / "backups" / f"domivka_{today()}.db")) as backup:
                    await db.DB.backup(backup)
                for old in sorted((DATA / "backups").glob("*.db"))[:-14]:
                    old.unlink()
                last_backup = today()
        except Exception:
            logging.exception("фонова задача")
        await asyncio.sleep(1800)


async def main():
    global WEBAPP_URL, bot, SITE, APPROVAL, BOT_USERNAME, ADMINS, PAYMODE, DEPOSIT, COMMISSION, PAY_HOURS, HOST_HOURS, FREE_CANCEL_H, BOOST_PRICE, BOOST_FREE_DAYS, PAY_TOKEN, PAY_REQ
    load_env()
    deployment.check_config()
    num = lambda k, d: int(re.sub(r"\D", "", os.environ.get(k, "")) or d)
    PAYMODE = os.environ.get("PAYMENT_MODE", "direct").lower()
    if PAYMODE not in ("off", "direct", "manual", "telegram"):
        sys.exit("PAYMENT_MODE має бути: direct, off, manual або telegram")
    PAY_TOKEN, PAY_REQ = os.environ.get("PAYMENT_PROVIDER_TOKEN", ""), os.environ.get("PAYMENT_REQUISITES", "")
    DEPOSIT, COMMISSION = min(100, max(1, num("DEPOSIT_PERCENT", 30))), min(50, num("COMMISSION_PERCENT", 0))
    PAY_HOURS, HOST_HOURS, FREE_CANCEL_H = num("PAY_WINDOW_HOURS", 12), num("HOST_REPLY_HOURS", 24), num("FREE_CANCEL_HOURS", 48)
    BOOST_PRICE, BOOST_FREE_DAYS = num("BOOST_PRICE_PER_DAY", 0), num("BOOST_FREE_DAYS", 3)
    if PAYMODE == "telegram" and not PAY_TOKEN:
        sys.exit("PAYMENT_MODE=telegram, але не вказано PAYMENT_PROVIDER_TOKEN у .env")
    if PAYMODE == "manual" and not PAY_REQ:
        sys.exit("PAYMENT_MODE=manual, але не вказано PAYMENT_REQUISITES у .env (картка/IBAN для оплат)")
    ADMINS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
    SITE = os.environ.get("SITE_URL", "")
    APPROVAL = os.environ.get("REQUIRE_APPROVAL", "0") == "1"
    PRO["free"], PRO["max"] = max(1, num("FREE_LISTINGS", 3)), max(5, num("PRO_LISTINGS", 50))
    PRO["stars"] = {30: num("PRO_STARS_30", 400), 90: num("PRO_STARS_90", 1000)}
    PRO["boost_off"] = min(90, num("PRO_BOOST_DISCOUNT", 20))
    DATA.mkdir(exist_ok=True)
    await db.init(str(DATA / "domivka.db"))
    await market.setup(sys.modules[__name__])
    await cart.setup(sys.modules[__name__])
    await business.setup(sys.modules[__name__])
    await billing.setup(sys.modules[__name__])
    market_bot.setup(sys.modules[__name__])
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception:
        sys.exit("Токен не підійшов. Перевірте BOT_TOKEN у файлі .env (скопіюйте його з @BotFather ще раз).")
    BOT_USERNAME = me.username or ""
    expected = os.environ.get("EXPECTED_BOT_USERNAME", "").lstrip("@").lower()
    if expected and expected != BOT_USERNAME.lower():
        sys.exit("BOT_TOKEN належить іншому боту. Перевірте EXPECTED_BOT_USERNAME і окремий .env.")
    feat.setup(sys.modules[__name__])
    await bot.set_my_commands([
        BotCommand(command="start", description="Домівка — оберіть свій маркет"),
        BotCommand(command="account", description="Мій кабінет"),
        BotCommand(command="club", description="Мій клуб, рівень та бонуси"),
        BotCommand(command="cart", description="Кошик у продавців"),
        BotCommand(command="friends", description="Запросити друзів"),
        BotCommand(command="admin", description="Керування сервісом"),
        BotCommand(command="paysupport", description="Допомога з оплатою Stars"),
        BotCommand(command="version", description="Версія запущеного сервісу"),
        BotCommand(command="services", description="Нерухомість або маркет"),
        BotCommand(command="market", description="Знайти товари"),
        BotCommand(command="sell", description="Продати річ"),
        BotCommand(command="import", description="Імпорт свого оголошення за посиланням"),
        BotCommand(command="moderate", description="Модерація для адміністратора"),
        BotCommand(command="orders", description="Мої покупки та продажі"),
        BotCommand(command="rent", description="Оренда нерухомості"),
        BotCommand(command="settings", description="Мова та налаштування"),
        BotCommand(command="menu", description="Меню нерухомості"),
        BotCommand(command="app", description="Відкрити мініапп"),
        BotCommand(command="help", description="Допомога")])
    explicit = os.environ.get("WEBAPP_URL", "").rstrip("/")
    if os.environ.get("WEBAPP", "on").lower() != "off":
        try:
            port = await webapp.start(sys.modules[__name__], num("WEBAPP_PORT", 8080))
        except OSError:
            sys.exit("Порт для Mini App зайнятий. Найімовірніше, бот уже запущений в іншому вікні: закрийте його й запустіть знову.")
        if explicit.startswith("https://"):
            await on_tunnel_url(explicit)
        elif os.environ.get("TUNNEL", "auto").lower() != "off":
            asyncio.create_task(tunnel.supervise(port, on_tunnel_url))
            logging.info("Готую адресу для Mini App (перший раз ~1 хвилина, завантажується cloudflared)…")
    asyncio.create_task(background())
    logging.info("Бот @%s запущено. Адміністраторів: %d. Режим оплати: %s. Не закривайте це вікно.", BOT_USERNAME, len(ADMINS), PAYMODE)
    await bot.delete_webhook(drop_pending_updates=False)
    logging.info("Domivka %s — marketplace, business and cart enabled", VERSION)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

