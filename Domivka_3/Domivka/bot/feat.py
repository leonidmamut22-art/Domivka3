"""Нові можливості «Домівки»: верифікація, оплата напряму між гостем і господарем, акти заселення/виїзду,
довгострокова оренда (договір, графік платежів, лічильники, комуналка), зручності, мапа й радіус, порівняння, AI-помічник.

Принцип: платформа НЕ приймає й НЕ тримає гроші. Бот лише організовує угоду й фіксує факти.
Модуль підключається з bot.py через setup(); усе спільне (say, tell, db, стан S) береться звідти."""
import datetime as dt
import html
import io
import json
import logging
import math
import os
import re

import db

B = None  # головний модуль (bot.py)
say = tell = esc = uah = kb = today = addd = None
REQ_VER, LONG_DAYS, BOOST_STARS = True, 28, 0
PKG: dict = {}

AMEN = {
    "pwr": ("⚡", "Світло без перебоїв", r"генератор|ecoflow|екофлоу|павербанк|блекаут|без перебоїв|інвертор"),
    "shl": ("🛡", "Укриття поруч", r"укритт|сховищ|бомбосхов"),
    "net": ("📶", "Інтернет/Starlink", r"інтернет|wi-?fi|вайфай|starlink|старлінк"),
    "wat": ("💧", "Стабільна вода/бойлер", r"бойлер|стабільна вода|гаряча вода"),
    "htg": ("🔥", "Автономне опалення", r"автономн"),
    "pet": ("🐾", "З тваринами", r"тварин|собак|песик|кішк|\bкіт\b|з котом|\bpets?\b"),
    "idp": ("🧡", "Приймаю ВПО", r"\bвпо\b|переселен"),
    "prk": ("🅿️", "Паркінг", r"паркін|парко|паркув"),
    "elv": ("🛗", "Ліфт", r"ліфт"),
    "acn": ("❄️", "Кондиціонер", r"кондиціонер"),
    "wsh": ("🧺", "Пральна машина", r"пральн"),
    "bal": ("🌿", "Балкон", r"балкон"),
    "kid": ("👶", "Для сімей з дітьми", r"\bдіт|дитяч|сімей|сім'я"),
}
MK = {"el": ("⚡ Світло", "кВт·год", "t_el"), "wat": ("💧 Вода", "м³", "t_wat"), "gas": ("🔥 Газ", "м³", "t_gas")}
KL = {"rent": "🏠 Оренда", "util_el": "⚡ Світло", "util_wat": "💧 Вода", "util_gas": "🔥 Газ"}
ST = {"due": "🟡 до сплати", "review": "🔎 на перевірці господаря", "claimed": "⏳ чекає підтвердження", "paid": "✅ сплачено", "rejected": "✖ відхилено"}
DEP_MODES = {"cash": "💵 Оплата при заселенні (без передоплати)", "card": "💳 Передоплата господарю напряму на картку",
             "trust": "🤝 Без передоплати для перевірених гостей, інші платять наперед"}
CANCEL = [("✖ Скасувати", "m:m")]


def setup(mod):
    global B, say, tell, esc, uah, kb, today, addd, REQ_VER, LONG_DAYS, BOOST_STARS, PKG
    B = mod
    say, tell, esc, uah, kb, today, addd = mod.say, mod.tell, mod.esc, mod.uah, mod.kb, mod.today, mod.addd
    num = lambda k, d: int(re.sub(r"\D", "", os.environ.get(k, "")) or d)
    REQ_VER = os.environ.get("REQUIRE_VERIFIED_HOST", "1") != "0"
    LONG_DAYS = num("LONG_TERM_DAYS", 28)
    BOOST_STARS = num("BOOST_STARS_PER_DAY", 0)
    PKG = {d: num(f"BOOST_STARS_{d}", BOOST_STARS * d) for d in (3, 7, 30)}


def S():
    return B.S


def fnum(t: str):
    try:
        return float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def amen_from_text(t: str) -> list:
    t = (t or "").lower()
    return [k for k, (_, _, rx) in AMEN.items() if re.search(rx, t)]


def amen_labels(s: str) -> list:
    return [f"{AMEN[k][0]} {AMEN[k][1]}" for k in (s or "").split(",") if k in AMEN]


def km(a, b, c, d) -> float:
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def stars_on() -> bool:
    return any(PKG.values())


def boost_price(d: int) -> int:
    return PKG.get(d, BOOST_STARS * d)


async def send_photos(chat: int, ids: list, caption: str = ""):
    ids = ids[:10]
    if not ids:
        return
    if len(ids) == 1:
        return await B.bot.send_photo(chat, ids[0], caption=caption[:1000] or None)
    from aiogram.types import InputMediaPhoto
    await B.bot.send_media_group(chat, [InputMediaPhoto(media=p, caption=caption[:1000] if i == 0 else None) for i, p in enumerate(ids)])


# ═════════════════════════ 1. ВЕРИФІКАЦІЯ ═════════════════════════
async def verify_menu(chat: int, u: dict):
    v = u.get("verified") or 0
    if v == 2:
        return await say(chat, "✅ Ви вже верифіковані. Бейдж видно в профілі й оголошеннях.", [[("🏠 Меню", "m:m")]])
    if v == 1:
        return await say(chat, "⏳ Документи на перевірці. Зазвичай це до доби, я напишу, щойно буде рішення.", [[("🏠 Меню", "m:m")]])
    S()[u["id"]] = {"flow": "z_vdoc"}
    await say(chat, "🛡 <b>Верифікація</b> (один раз)\n\nВерифіковані господарі можуть приймати передоплату напряму, а верифіковані гості "
                    "бронюють без передоплати там, де господар це дозволив.\n\n<b>Крок 1 з 2.</b> Надішліть фото документа (паспорт або ID-картка). "
                    "Номер можна закрити: потрібні фото й ім'я.\n\nДокумент бачить лише адміністратор. Після рішення фото видаляються з бази бота.", [CANCEL])


async def verify_submit(chat: int, u: dict, doc: str, selfie: str):
    await db.ex("insert or replace into verif(user,doc,selfie,status) values(?,?,?,'pending')", (u["id"], doc, selfie))
    await db.ex("update users set verified=1 where id=?", (u["id"],))
    S().pop(u["id"], None)
    for ad in B.ADMINS:
        try:
            await B.bot.send_photo(ad, doc, caption=f"🛡 Верифікація: документ\n{esc(u['name'])} (id {u['id']}) · {esc(u['role'])}")
            await B.bot.send_photo(ad, selfie, caption="Селфі. Обличчя збігається з документом?",
                                   reply_markup=kb([[("✅ Схвалити", f"z:av:{u['id']}:1"), ("✖ Відхилити", f"z:av:{u['id']}:0")]]))
        except Exception:
            logging.warning("не вдалося надіслати верифікацію адміну %s", ad)
    await say(chat, "Дякую! Надіслано на перевірку ⏳ Повідомлю про рішення.", [[("🏠 Меню", "m:m")]])


async def verify_list(chat: int):
    rows = await db.q("select v.*, u.name from verif v join users u on u.id=v.user where v.status='pending' limit 10")
    if not rows:
        return await say(chat, "Заявок на верифікацію немає ✓", [[("⬅ Назад", "a:m")]])
    for r in rows:
        try:
            await B.bot.send_photo(chat, r["doc"], caption=f"🛡 {esc(r['name'])} (id {r['user']}): документ")
            await B.bot.send_photo(chat, r["selfie"], caption="Селфі", reply_markup=kb([[("✅ Схвалити", f"z:av:{r['user']}:1"), ("✖ Відхилити", f"z:av:{r['user']}:0")]]))
        except Exception:
            await say(chat, f"Не вдалося показати фото користувача {r['user']}.")


async def verify_decide(chat: int, target: int, ok: bool):
    u = await db.one("select * from users where id=?", (target,))
    if not u or u["verified"] != 1:
        return await say(chat, "Заявку вже оброблено.")
    await db.ex("update users set verified=? where id=?", (2 if ok else 3, target))
    await db.ex("update verif set doc='', selfie='', status=? where user=?", ("ok" if ok else "no", target))  # мінімізація даних
    await tell(target, "✅ Верифікацію пройдено! Тепер у вас бейдж «Верифікований»." if ok
               else "✖ Верифікацію не пройдено: фото нечіткі або не збігаються. Спробуйте ще раз: меню → 🛡 Верифікація.")
    await say(chat, "Схвалено ✓" if ok else "Відхилено ✓")


# ═════════════════════════ 2. АНТИШАХРАЙСТВО: ДУБЛІ ФОТО ═════════════════════════
def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


async def dhash(fid: str) -> str:
    try:
        from PIL import Image
        f = await B.bot.get_file(fid)
        buf = io.BytesIO()
        await B.bot.download_file(f.file_path, buf)
        return dhash_img(Image.open(buf))
    except Exception:
        logging.warning("хеш фото недоступний")
        return ""


def dhash_img(im) -> str:
    px = list(im.convert("L").resize((9, 8)).getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


async def check_photos(lid: int):
    """Хешує фото оголошення й шукає збіги з оголошеннями ІНШИХ господарів (крадені фото). Збіг → на модерацію."""
    l = await db.one("select * from listings where id=?", (lid,))
    if not l:
        return
    cur = json.loads(l["photos"])
    have = {r["fid"] for r in await db.q("select fid from phash where listing_id=?", (lid,))}
    for fid in have - set(cur):
        await db.ex("delete from phash where listing_id=? and fid=?", (lid, fid))
    for fid in cur:
        if fid not in have:
            h = await dhash(fid)
            if h:
                await db.ex("insert or ignore into phash values(?,?,?)", (lid, fid, h))
    mine = await db.q("select h from phash where listing_id=?", (lid,))
    others = await db.q("select p.h, p.listing_id, l.owner from phash p join listings l on l.id=p.listing_id where p.listing_id!=? and l.owner!=?", (lid, l["owner"]))
    hit = {o["listing_id"] for m in mine for o in others if hamming(m["h"], o["h"]) <= 4}
    if hit:
        await db.ex("update listings set blocked=2 where id=? and blocked=0", (lid,))
        for ad in B.ADMINS:
            await tell(ad, f"🕵 Схожі фото: оголошення #{lid} має збіг із #{', #'.join(map(str, sorted(hit)))} (інший господар). Відправлено на модерацію.",
                       [[("👁 Переглянути", f"lc:{lid}"), ("⛔ Заблокувати", f"ab:{lid}")], [("✅ Усе гаразд", f"ap:{lid}")]])
        await tell(l["owner"], "⏳ Оголошення тимчасово на перевірці: схожі фото вже є в іншому оголошенні. Адміністратор розбереться найближчим часом.")


# ═════════════════════════ 3. ОПЛАТА НАПРЯМУ (платформа гроші не тримає) ═════════════════════════
async def guest_trusted(gid: int) -> bool:
    g = await db.one("select verified, strikes from users where id=?", (gid,))
    a, _ = await B.rating(guest=gid)
    done = (await db.one("select count(*) n from bookings where guest=? and status='confirmed' and co<=date('now')", (gid,)))["n"]
    if not g or g["strikes"] >= 2:
        return False
    return (g["verified"] == 2 and (not a or a >= 4.3)) or (done >= 2 and bool(a) and a >= 4.5)


async def pay_needed(l: dict, gid: int) -> bool:
    mode = B.PAYMODE
    if mode in ("manual", "telegram"):
        return True
    if mode != "direct":
        return False
    dm = l.get("dep_mode") or "cash"
    return dm == "card" or (dm == "trust" and not await guest_trusted(gid))


async def host_ready(uid: int):
    h = await db.one("select * from users where id=?", (uid,))
    if REQ_VER and h["verified"] != 2:
        return False, "Щоб приймати передоплату напряму, потрібна верифікація (фото документа + селфі, один раз). Це захищає гостей від шахраїв."
    if not h["payout"]:
        return False, "Вкажіть картку або IBAN, на які гість має переказати передоплату: меню → 💳 Як мені платити."
    return True, ""


async def ask_direct(b: dict):
    l = await db.one("select * from listings where id=?", (b["listing_id"],))
    h = await db.one("select name, payout from users where id=?", (l["owner"],))
    dep = B.deposit_of(b["total"])
    rest = f"\nРешту ({uah(b['total'] - dep)}) сплатите господарю при заселенні." if dep < b["total"] else ""
    txt = (f"💳 <b>Передоплата господарю напряму</b>\n{esc(l['title'])}\n📅 {b['ci']} → {b['co']}\nДо сплати зараз: <b>{uah(dep)}</b>{rest}\n\n"
           f"<b>Реквізити господаря ({esc(h['name'])}):</b>\n{esc(h['payout'])}\nУ призначенні вкажіть код <b>#{b['id']}</b>.\n\n"
           f"Оплатіть до {b['due_by']}, потім натисніть «Я оплатив(ла)»: господар підтвердить надходження, і бронь стане остаточною.\n"
           f"Безкоштовне скасування: не пізніше ніж за {B.FREE_CANCEL_H} год до заїзду.\n\n"
           "🔒 Платформа не приймає й не тримає ваші гроші. Не переказуйте гроші на інші реквізити й не спілкуйтесь поза ботом: якщо просять, це ознака шахрайства.")
    await tell(b["guest"], txt, [[("✅ Я оплатив(ла)", f"z:gp:{b['id']}")], [("✖ Скасувати бронювання", f"bc:{b['id']}")]])


async def guest_paid(chat: int, uid: int, bid: int):
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.status='awaiting_payment'", (bid, uid))
    if not b:
        return await say(chat, "Бронювання недоступне або час на оплату минув.")
    await db.ex("update bookings set claim_at=?, claim_rem=0, method='claimed' where id=?", (B.now_s(), bid))
    S()[uid] = {"flow": "z_proof", "bid": bid}
    g = await db.one("select name from users where id=?", (uid,))
    await tell(b["owner"], f"💰 <b>{esc(g['name'])}</b> повідомляє про оплату передоплати {uah(B.deposit_of(b['total']))} за «{esc(b['title'])}» ({b['ci']} → {b['co']}), код #{bid}.\n"
                           "Перевірте надходження на картку/рахунок і підтвердіть:", [[("✅ Отримав", f"z:hp:{bid}:1"), ("✖ Не отримав", f"z:hp:{bid}:0")]])
    await say(chat, "Господарю надіслано запит на підтвердження ✓ Можете надіслати скріншот переказу (необов'язково), він піде господарю.", [[("🏠 Меню", "m:m")]])


async def host_decide(chat: int, uid: int, bid: int, ok: bool):
    b = await db.one("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and l.owner=? and b.status='awaiting_payment'", (bid, uid))
    if not b:
        return await say(chat, "Вже оброблено.")
    if ok:
        await confirm_direct(bid)
        return await say(chat, "Оплату підтверджено, бронювання остаточне ✓")
    await db.ex("update bookings set claim_at='', method='', due_by=? where id=?", (B.now_s(B.PAY_HOURS), bid))
    await tell(b["guest"], f"✖ Господар не бачить вашої оплати за бронювання #{bid}. Перевірте реквізити й суму: якщо переказ був, надішліть квитанцію в чат господарю й натисніть кнопку ще раз.",
               [[("✅ Я оплатив(ла)", f"z:gp:{bid}")], [("💬 Написати господарю", f"ask:{b['listing_id']}")]])
    await say(chat, "Гостю повідомлено. Час на оплату продовжено.")


async def confirm_direct(bid: int):
    b = await db.one("select b.*, l.title, l.owner, l.address from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    dep = B.deposit_of(b["total"])
    await db.ex("update bookings set status='confirmed', paid=1, deposit=?, commission=0, payout_status='direct', method='direct', due_by='', claim_at='' where id=?", (dep, bid))
    await db.ex("insert into payments(kind,ref,user,amount,method) values('direct_prepay',?,?,?,'direct')", (bid, b["guest"], dep))
    h = await db.one("select name, phone, username from users where id=?", (b["owner"],))
    g = await db.one("select name, phone from users where id=?", (b["guest"],))
    info = ("🔑 Адреса та інструкції:\n" + esc(b["address"])) if b["address"] else "Адресу й інструкції заселення надішле господар у чаті."
    handle = "@" + h["username"] if h["username"] else ""
    await tell(b["guest"], f"✅ Оплату підтверджено господарем, бронювання остаточне!\n<b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']}\nГосподар: {esc(h['name'])} {esc(h['phone'])} {handle}\n{info}",
               [[("💬 Написати господарю", f"ask:{b['listing_id']}")]])
    await tell(b["owner"], f"✅ Бронювання #{bid} підтверджено.\n👤 {esc(g['name'])} {esc(g['phone'])}", [[("💬 Написати гостю", f"ask:{b['listing_id']}")]])
    await on_confirmed(bid)


async def cancel_direct(b: dict, by: str) -> str:
    """Скасування оплаченого напряму бронювання: гроші між сторонами, бот лише фіксує й нагадує правила."""
    if b.get("payout_status") != "direct":
        return ""
    hrs = (dt.datetime.fromisoformat(b["ci"] + "T14:00") - dt.datetime.now()).total_seconds() / 3600
    if by == "host":
        await tell(b["owner"], "Ви скасували оплачену бронь: поверніть гостю передоплату повністю й відмітьте це в чаті. Якщо гість поскаржиться, це вплине на ваш рейтинг.",
                   [[("💬 Чат з гостем", f"ask:{b['listing_id']}")]])
        return " Господар має повернути вам передоплату напряму. Якщо не повертає, подайте претензію."
    early = hrs >= B.FREE_CANCEL_H
    await tell(b["owner"], ("Скасування вчасно: поверніть гостю передоплату." if early else f"Скасування пізніше ніж за {B.FREE_CANCEL_H} год до заїзду: за правилами сервісу передоплата залишається у вас."))
    return " Господар має повернути вам передоплату напряму (скасування вчасно)." if early else f" Скасування пізніше ніж за {B.FREE_CANCEL_H} год: передоплата залишається господарю."


# ═════════════════════════ 4. ПІСЛЯ ПІДТВЕРДЖЕННЯ: ЗАЛОГ, АКТИ, ДОВГА ОРЕНДА ═════════════════════════
async def on_confirmed(bid: int):
    b = await db.one("select b.*, l.title, l.dep_amt, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=?", (bid,))
    if not b:
        return
    n = (dt.date.fromisoformat(b["co"]) - dt.date.fromisoformat(b["ci"])).days
    lines = []
    if b["dep_amt"]:
        lines.append(f"🔐 Залог {uah(b['dep_amt'])}: передається господарю напряму при заселенні й повертається при виїзді за актом, якщо немає претензій.")
    lines.append("📋 У день заїзду бот запропонує акт заселення з фото й показниками лічильників: це захищає і гостя, і господаря.")
    for who in (b["guest"], b["owner"]):
        await tell(who, "\n".join(lines))
    if n >= LONG_DAYS:
        await create_lease(b, n)


async def deal(bid: int, uid: int):
    b = await db.one("select b.*, l.title, l.owner, l.dep_amt, l.address from bookings b join listings l on l.id=b.listing_id where b.id=? and b.status='confirmed'", (bid,))
    if not b or uid not in (b["guest"], b["owner"]):
        return None
    return b, ("host" if uid == b["owner"] else "guest")


async def act_get(bid: int, kind: str) -> dict:
    a = await db.one("select * from acts where booking_id=? and kind=?", (bid, kind))
    if not a:
        await db.ex("insert into acts(booking_id,kind) values(?,?)", (bid, kind))
        a = await db.one("select * from acts where booking_id=? and kind=?", (bid, kind))
    return a


def meters_txt(m: dict) -> str:
    return ", ".join(f"{MK[k][0]} {v:g}" for k, v in m.items() if k in MK) or "—"


async def act_view(chat: int, uid: int, bid: int, kind: str):
    d = await deal(bid, uid)
    if not d:
        return await say(chat, "Угода недоступна.")
    b, role = d
    a = await act_get(bid, kind)
    ph, mt = json.loads(a["photos"]), json.loads(a["meters"])
    ok = lambda x: "✅" if x else "⏳"
    extra = ""
    if kind == "in" and b["dep_amt"]:
        extra = f"\n🔐 Залог {uah(b['dep_amt'])}: відмітьте в нотатці, що передали/отримали."
    if kind == "out":
        ain = await act_get(bid, "in")
        mi = json.loads(ain["meters"])
        diff = [f"{MK[k][0]}: {mt[k] - mi[k]:g} {MK[k][1]}" for k in mt if k in mi and k in MK]
        if diff:
            extra = "\n📊 Спожито за проживання: " + "; ".join(diff)
    title = "заселення" if kind == "in" else "виїзду"
    text = (f"📋 <b>Акт {title}</b>\n{esc(b['title'])}\n📅 {b['ci']} → {b['co']}\n\n📷 Фото: {len(ph)}\n🔢 Лічильники: {meters_txt(mt)}\n"
            f"📝 Нотатки: {esc(a['notes']) or '—'}{extra}\n\nГосподар: {ok(a['host_ok'])}   Гість: {ok(a['guest_ok'])}")
    p = f"z:aa:{bid}:{kind}"
    rows = [[("📷 Фото", p + ":p"), ("🔢 Лічильники", p + ":m"), ("📝 Нотатка", p + ":n")], [("✅ Підтверджую акт", p + ":ok")]]
    if ph:
        rows.append([("👁 Показати фото акта", p + ":v")])
    rows.append([("⬅ Угоди", "z:my")])
    await say(chat, text, rows)


async def act_touch(bid: int, kind: str, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    await db.ex(f"update acts set {sets}, host_ok=0, guest_ok=0, done=0 where booking_id=? and kind=?", (*fields.values(), bid, kind))


async def act_ok(chat: int, uid: int, bid: int, kind: str):
    d = await deal(bid, uid)
    if not d:
        return
    b, role = d
    a = await act_get(bid, kind)
    await db.ex(f"update acts set {'host_ok' if role == 'host' else 'guest_ok'}=1 where id=?", (a["id"],))
    a = await act_get(bid, kind)
    if not (a["host_ok"] and a["guest_ok"]):
        await tell(b["guest"] if role == "host" else b["owner"], f"📋 Друга сторона підтвердила акт ({'заселення' if kind == 'in' else 'виїзду'}) за «{esc(b['title'])}». Перегляньте й підтвердьте.",
                   [[("📋 Відкрити акт", f"z:aa:{bid}:{kind}")]])
        return await say(chat, "Ваше підтвердження збережено ✓ Чекаємо другу сторону.")
    if a["done"]:
        return
    await db.ex("update acts set done=1 where id=?", (a["id"],))
    ls = await db.one("select * from leases where booking_id=?", (bid,))
    mt = json.loads(a["meters"])
    if ls and kind == "in":
        for k, v in mt.items():
            if not await db.one("select 1 from meters where lease_id=? and kind=?", (ls["id"], k)):
                await db.ex("insert into meters(lease_id,kind,value,d,by_user) values(?,?,?,?,0)", (ls["id"], k, v, today()))
    summ = f"✅ <b>Акт {'заселення' if kind == 'in' else 'виїзду'} підписано обома сторонами</b> ({today()})\n{esc(b['title'])}\n📷 {len(json.loads(a['photos']))} фото · 🔢 {meters_txt(mt)}\nАкт збережено в боті: меню → 📋 Угоди й акти."
    await tell(b["guest"], summ + ("\n\nЯкщо є питання щодо залогу, скористайтесь кнопкою нижче." if kind == "out" else ""),
               [[("⚠️ Питання щодо залогу", f"z:cl:{bid}:r")]] if kind == "out" else None)
    await tell(b["owner"], summ, [[("🔓 Залог повернуто", f"z:dr:{bid}"), ("⚠️ Претензія", f"z:cl:{bid}:d")]] if kind == "out" else None)


# ───── претензії (залог, повернення коштів) ─────
async def claim_start(chat: int, uid: int, bid: int, kind: str):
    d = await deal(bid, uid)
    if not d:
        return
    S()[uid] = {"flow": "z_claim", "bid": bid, "kind": kind}
    await say(chat, "Опишіть претензію одним повідомленням (що сталося, яка сума). До неї автоматично додадуться акти заселення й виїзду з фото та показниками. Адміністратор розгляне справу.", [CANCEL])


async def claim_submit(chat: int, u: dict, s: dict, text: str):
    d = await deal(s["bid"], u["id"])
    S().pop(u["id"], None)
    if not d:
        return
    b, role = d
    against = b["guest"] if role == "host" else b["owner"]
    cid = await db.ex("insert into claims(booking_id,by_user,against,kind,text) values(?,?,?,?,?)", (b["id"], u["id"], against, s["kind"], text[:1500]))
    await tell(against, f"⚠️ На вас подано претензію за «{esc(b['title'])}»: адміністрація розгляне її й за потреби зв'яжеться з вами. Підготуйте докази (фото, чат).")
    for ad in B.ADMINS:
        await tell(ad, f"🧾 Претензія #{cid} (бронь #{b['id']}) від {'господаря' if role == 'host' else 'гостя'}", [[("Відкрити", f"z:acl:{cid}")]])
    await say(chat, "Претензію надіслано ✓", [[("🏠 Меню", "m:m")]])


async def claims_admin(chat: int, cid: int = 0):
    rows = await db.q("select * from claims where status='open' and id=?" if cid else "select * from claims where status='open' order by id limit 8", (cid,) if cid else ())
    if not rows:
        return await say(chat, "Відкритих претензій немає ✓", [[("⬅ Назад", "a:m")]])
    for c in rows:
        b = await db.one("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.id=?", (c["booking_id"],))
        ev = []
        for k in ("in", "out"):
            a = await db.one("select * from acts where booking_id=? and kind=?", (c["booking_id"], k))
            if a:
                ev.append(f"Акт {k}: фото {len(json.loads(a['photos']))}, {meters_txt(json.loads(a['meters']))}, нотатки: {esc(a['notes'][:300]) or '—'}")
        who = await db.one("select name, strikes from users where id=?", (c["against"],))
        await say(chat, f"🧾 <b>Претензія #{c['id']}</b> · бронь #{c['booking_id']} · {esc(b['title'] if b else '')}\nВид: {'залог/шкода' if c['kind'] == 'd' else 'повернення коштів'}\n"
                        f"Проти: {esc(who['name'])} (попереджень: {who['strikes']})\n\n{esc(c['text'])}\n\n" + ("\n".join(ev) or "Актів немає."),
                  [[("✅ Обґрунтована (+попередження)", f"z:cv:{c['id']}:1"), ("✖ Відхилити", f"z:cv:{c['id']}:0")]])
        a = await db.one("select photos from acts where booking_id=? order by id desc limit 1", (c["booking_id"],))
        if a and json.loads(a["photos"]):
            await send_photos(chat, json.loads(a["photos"]), "Фото з акта")


async def claim_decide(chat: int, cid: int, ok: bool):
    c = await db.one("select * from claims where id=? and status='open'", (cid,))
    if not c:
        return await say(chat, "Вже розглянуто.")
    await db.ex("update claims set status=? where id=?", ("ok" if ok else "no", cid))
    if ok:
        await db.ex("update users set strikes=strikes+1 where id=?", (c["against"],))
        u = await db.one("select name, strikes from users where id=?", (c["against"],))
        await tell(c["against"], "⚠️ Претензію визнано обґрунтованою: вам додано попередження. Після 3 попереджень акаунт може бути заблоковано.")
        if u["strikes"] >= 3:
            await say(chat, f"❗ У {esc(u['name'])} вже {u['strikes']} попередження.", [[("🚫 Забанити", f"ub:{c['against']}:1")]])
    await tell(c["by_user"], "✅ Вашу претензію визнано обґрунтованою." if ok else "Претензію розглянуто й відхилено.")
    await say(chat, "Рішення збережено ✓")


# ───── довгострокова оренда ─────
async def create_lease(b: dict, n: int):
    if await db.one("select 1 from leases where booking_id=?", (b["id"],)):
        return
    rent = round(b["total"] * 30 / n)
    lid = await db.ex("insert into leases(booking_id,listing_id,host,guest,rent,deposit,pay_day) values(?,?,?,?,?,?,?)",
                      (b["id"], b["listing_id"], b["owner"], b["guest"], rent, b["dep_amt"] or 0, min(int(b["ci"][8:10]), 28)))
    txt = (f"📄 <b>Довгострокова оренда</b>: «{esc(b['title'])}»\nПідготовлено договір, графік щомісячних платежів, облік лічильників і комуналки.\n"
           f"Оренда {uah(rent)}/міс · платіж щомісяця {min(int(b['ci'][8:10]), 28)}-го числа.")
    for who in (b["guest"], b["owner"]):
        await tell(who, txt, [[("📄 Відкрити угоду", f"z:ls:{lid}")]])


async def lease_of(lid: int, uid: int):
    L = await db.one("select * from leases where id=?", (lid,))
    if not L or uid not in (L["host"], L["guest"]):
        return None, None
    return L, ("host" if uid == L["host"] else "guest")


async def lease_view(chat: int, uid: int, lid: int):
    L, role = await lease_of(lid, uid)
    if not L:
        return await say(chat, "Угода недоступна.")
    b = await db.one("select b.*, l.title, l.t_el, l.t_wat, l.t_gas from bookings b join listings l on l.id=b.listing_id where b.id=?", (L["booking_id"],))
    ok = lambda x: "✅ " + x[:10] if x else "⏳"
    open_n = (await db.one("select count(*) n from ledger where lease_id=? and status in ('due','review','claimed')", (lid,)))["n"]
    text = (f"📄 <b>Оренда: {esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']}\n💰 {uah(L['rent'])}/міс, платіж щомісяця {L['pay_day']}-го\n🔐 Залог: {uah(L['deposit'])}\n"
            f"⚡ {b['t_el']:g} ₴/кВт·год · 💧 {b['t_wat']:g} ₴/м³ · 🔥 {b['t_gas']:g} ₴/м³\n\n"
            f"Умови погоджено: господар {ok(L['host_ok'])} · гість {ok(L['guest_ok'])}\nВідкритих платежів: {open_n}")
    rows = [[("📄 Договір (файл)", f"z:lf:{lid}"), ("✍️ Вказати ПІБ", f"z:ln:{lid}")], [("✅ Погоджую умови", f"z:lk:{lid}")],
            [("🧾 Платежі", f"z:lg:{lid}"), ("🔢 Передати показники", f"z:mt:{lid}")],
            [("📋 Акт заселення", f"z:aa:{b['id']}:in"), ("📋 Акт виїзду", f"z:aa:{b['id']}:out")]]
    if role == "host":
        rows.append([("🧾 Тарифи комуналки", f"z:tf:{L['listing_id']}")])
    rows.append([("⬅ Угоди", "z:my")])
    await say(chat, text, rows)


def contract_html(L: dict, b: dict, l: dict, hu: dict, gu: dict) -> str:
    e = html.escape
    hn = e(L["host_name"] or hu["name"]) + ("" if L["host_name"] else " (вкажіть ПІБ)")
    gn = e(L["guest_name"] or gu["name"]) + ("" if L["guest_name"] else " (вкажіть ПІБ)")
    return f"""<!doctype html><html lang="uk"><meta charset="utf-8"><title>Договір найму житла №{L['id']}</title>
<style>body{{font:15px/1.5 Georgia,serif;max-width:760px;margin:30px auto;padding:0 16px;color:#111}}h1{{font-size:20px;text-align:center}}
.n{{background:#fff7e0;border:1px solid #e8c766;padding:10px;font-size:13px}}td{{padding:6px 14px 6px 0;vertical-align:top}}@media print{{.n{{display:none}}}}</style>
<h1>ДОГОВІР НАЙМУ (ОРЕНДИ) ЖИТЛА №{L['id']}</h1>
<p class="n"><b>Шаблон-основа</b>, сформований ботом «Домівка» з даних бронювання. Це не юридична консультація. Перед підписанням перевірте умови, а для тривалих
строків і подальшого оформлення проконсультуйтеся з юристом чи нотаріусом. Паспортні дані й ІПН впишіть вручну, бот їх не зберігає.</p>
<p>Дата складання: {today()}</p>
<p><b>Наймодавець:</b> {hn}<br>Паспорт/ІПН: ______________________________</p>
<p><b>Наймач:</b> {gn}<br>Паспорт/ІПН: ______________________________</p>
<h3>1. Предмет</h3><p>Наймодавець передає наймачу в строкове платне користування житло: «{e(l['title'])}», {e(l['city'])}, {e(l['district'])}. Адреса: {e(l['address'] or '________________')}.</p>
<h3>2. Строк</h3><p>З {b['ci']} по {b['co']}. Передача й повернення житла оформлюються актами (заселення й виїзду) з фото та показниками лічильників, підтвердженими обома сторонами.</p>
<h3>3. Плата</h3><p>Орендна плата: {L['rent']} грн на місяць, сплачується щомісяця до {L['pay_day']}-го числа переказом напряму на реквізити наймодавця. Платформа «Домівка» не є стороною розрахунків і не приймає та не зберігає кошти сторін.</p>
<h3>4. Комунальні послуги</h3><p>Спожите оплачує наймач за показниками лічильників, які він передає щомісяця. Тарифи: електроенергія {l['t_el']:g} грн/кВт·год, вода {l['t_wat']:g} грн/м³, газ {l['t_gas']:g} грн/м³. Розрахунок = (поточні − попередні показники) × тариф.</p>
<h3>5. Залог</h3><p>Залог {L['deposit']} грн передається наймодавцю при заселенні й повертається при виїзді за актом протягом 3 днів, якщо немає обґрунтованих претензій щодо шкоди чи боргів.</p>
<h3>6. Права й обов'язки</h3><p>Наймач користується житлом за призначенням, не здає його в піднайм без письмової згоди, дбайливо ставиться до майна, повідомляє про несправності. Наймодавець забезпечує придатність житла, а доступ до нього без попередження не здійснює, окрім аварійних випадків.</p>
<h3>7. Припинення</h3><p>За взаємною згодою або з письмовим попередженням іншої сторони не менше ніж за 30 днів. Спори вирішуються переговорами, а за неможливості домовитися, у встановленому законом порядку.</p>
<h3>8. Підписи</h3><table><tr><td>Наймодавець: ____________</td><td>Наймач: ____________</td></tr></table>
<p style="font-size:12px;color:#666">Погодження в боті: наймодавець {e(L['host_ok'] or 'ні')}, наймач {e(L['guest_ok'] or 'ні')}.</p></html>"""


async def lease_file(chat: int, uid: int, lid: int):
    L, role = await lease_of(lid, uid)
    if not L:
        return
    b = await db.one("select * from bookings where id=?", (L["booking_id"],))
    l = await db.one("select * from listings where id=?", (L["listing_id"],))
    hu = await db.one("select name from users where id=?", (L["host"],))
    gu = await db.one("select name from users where id=?", (L["guest"],))
    from aiogram.types import BufferedInputFile
    await B.bot.send_document(chat, BufferedInputFile(contract_html(L, b, l, hu, gu).encode("utf-8"), filename=f"dogovir_{lid}.html"),
                              caption="📄 Відкрийте файл у браузері й роздрукуйте або збережіть як PDF. Не забудьте вписати паспортні дані й підписати.")


async def lease_accept(chat: int, uid: int, lid: int):
    L, role = await lease_of(lid, uid)
    if not L:
        return
    await db.ex(f"update leases set {'host_ok' if role == 'host' else 'guest_ok'}=? where id=?", (B.now_s(), lid))
    L = await db.one("select * from leases where id=?", (lid,))
    if L["host_ok"] and L["guest_ok"]:
        for who in (L["host"], L["guest"]):
            await tell(who, f"✅ Умови оренди (угода №{lid}) погоджено обома сторонами в боті. Щомісячні платежі й нагадування активні.")
    else:
        await say(chat, "Ваше погодження збережено ✓ Чекаємо другу сторону.")


async def ledger_view(chat: int, uid: int, lid: int):
    L, role = await lease_of(lid, uid)
    if not L:
        return
    rows = await db.q("select * from ledger where lease_id=? order by id desc limit 12", (lid,))
    lines = [f"{KL.get(r['kind'], r['kind'])} {r['period']}: {uah(r['amount'])} · {ST.get(r['status'], r['status'])}" + (f"\n   {esc(r['note'])}" if r["note"] else "") for r in rows]
    btn = []
    for r in rows:
        lab = f"{KL.get(r['kind'], '')} {r['period']}"
        if role == "guest" and r["status"] == "due":
            btn.append([(f"✅ Сплатив(ла): {lab}", f"z:lp:{r['id']}")])
        if role == "host" and r["status"] == "claimed":
            btn.append([(f"✅ Отримав: {lab}", f"z:lr:{r['id']}:1"), ("✖ Ні", f"z:lr:{r['id']}:0")])
        if role == "host" and r["status"] == "review":
            btn.append([(f"✅ Розрахунок ок: {lab}", f"z:ur:{r['id']}:1"), ("✖ Заперечую", f"z:ur:{r['id']}:0")])
    await say(chat, "🧾 <b>Платежі за оренду</b>\n\n" + ("\n".join(lines) or "Платежів ще немає: перший з'явиться в день заселення.") + "\n\nГроші переказуються напряму між вами, бот лише веде облік і квитанції.",
              btn[:8] + [[("⬅ Угода", f"z:ls:{lid}")]])


async def ledger_row(rid: int, uid: int, role: str):
    r = await db.one("select * from ledger where id=?", (rid,))
    if not r:
        return None, None
    L, rl = await lease_of(r["lease_id"], uid)
    return (r, L) if L and rl == role else (None, None)


async def ledger_paid(chat: int, uid: int, rid: int):
    r, L = await ledger_row(rid, uid, "guest")
    if not r or r["status"] != "due":
        return await say(chat, "Платіж недоступний.")
    await db.ex("update ledger set status='claimed' where id=?", (rid,))
    await tell(L["host"], f"💰 Орендар позначив оплату: {KL.get(r['kind'], '')} {r['period']}, {uah(r['amount'])}. Перевірте надходження:",
               [[("✅ Отримав", f"z:lr:{rid}:1"), ("✖ Ні", f"z:lr:{rid}:0")]])
    await say(chat, "Господарю надіслано на підтвердження ✓")


async def ledger_confirm(chat: int, uid: int, rid: int, ok: bool):
    r, L = await ledger_row(rid, uid, "host")
    if not r or r["status"] != "claimed":
        return await say(chat, "Вже оброблено.")
    if ok:
        await db.ex("update ledger set status='paid', paid_at=? where id=?", (today(), rid))
        rc = f"🧾 <b>Квитанція №{rid}</b>\n{KL.get(r['kind'], '')} за {r['period']}: {uah(r['amount'])}\nСплачено {today()} · підтверджено господарем у боті."
        await tell(L["guest"], rc)
        return await say(chat, rc)
    await db.ex("update ledger set status='due' where id=?", (rid,))
    await tell(L["guest"], f"✖ Господар не бачить оплату: {KL.get(r['kind'], '')} {r['period']}. Перевірте переказ і напишіть господарю в чат.", [[("🧾 Платежі", f"z:lg:{L['id']}")]])
    await say(chat, "Гостю повідомлено.")


async def utility_confirm(chat: int, uid: int, rid: int, ok: bool):
    r, L = await ledger_row(rid, uid, "host")
    if not r or r["status"] != "review":
        return await say(chat, "Вже оброблено.")
    await db.ex("update ledger set status=? where id=?", ("due" if ok else "rejected", rid))
    await tell(L["guest"], (f"🧾 Господар підтвердив розрахунок: {KL.get(r['kind'], '')} {r['period']}, {uah(r['amount'])}. Можна сплачувати." if ok
                            else f"✖ Господар заперечує розрахунок за {KL.get(r['kind'], '')} {r['period']}. Перевірте показники й напишіть йому в чат."), [[("🧾 Платежі", f"z:lg:{L['id']}")]])
    await say(chat, "Готово ✓")


async def meter_submit(chat: int, u: dict, s: dict, val: float):
    L, role = await lease_of(s["lease"], u["id"])
    S().pop(u["id"], None)
    if not L or role != "guest":
        return
    mk, per = s["mk"], today()[:7]
    prev = await db.one("select value from meters where lease_id=? and kind=? order by id desc limit 1", (L["id"], mk))
    if await db.one("select 1 from ledger where lease_id=? and kind=? and period=?", (L["id"], "util_" + mk, per)):
        return await say(chat, "За цей місяць показники вже передано.", [[("⬅ Угода", f"z:ls:{L['id']}")]])
    if not prev:
        await db.ex("insert into meters(lease_id,kind,value,d,by_user) values(?,?,?,?,?)", (L["id"], mk, val, today(), u["id"]))
        return await say(chat, "Початкові показники записано ✓ Наступного місяця передайте нові: я розрахую за різницею.", [[("⬅ Угода", f"z:ls:{L['id']}")]])
    if val < prev["value"]:
        return await say(chat, f"Показник менший за попередній ({prev['value']:g}). Перевірте число й спробуйте ще раз.", [[("🔢 Ще раз", f"z:mt:{L['id']}:{mk}")]])
    l = await db.one("select * from listings where id=?", (L["listing_id"],))
    tar, cons = l[MK[mk][2]], val - prev["value"]
    await db.ex("insert into meters(lease_id,kind,value,d,by_user) values(?,?,?,?,?)", (L["id"], mk, val, today(), u["id"]))
    amount = round(cons * tar)
    if amount <= 0:
        return await say(chat, f"Записано: спожито {cons:g} {MK[mk][1]}. Тариф не вказано, тож без нарахування.", [[("⬅ Угода", f"z:ls:{L['id']}")]])
    note = f"{prev['value']:g} → {val:g}: {cons:g} {MK[mk][1]} × {tar:g} ₴"
    rid = await db.ex("insert into ledger(lease_id,kind,period,amount,status,note) values(?,?,?,?,'review',?)", (L["id"], "util_" + mk, per, amount, note))
    await tell(L["host"], f"🔢 Орендар передав показники ({MK[mk][0]}): {esc(note)} = <b>{uah(amount)}</b>. Підтвердіть розрахунок:",
               [[("✅ Розрахунок ок", f"z:ur:{rid}:1"), ("✖ Заперечую", f"z:ur:{rid}:0")]])
    await say(chat, f"Розрахунок: {esc(note)} = <b>{uah(amount)}</b>. Надіслано господарю на підтвердження ✓", [[("🧾 Платежі", f"z:lg:{L['id']}")]])


async def my_deals(chat: int, uid: int):
    bs = await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and (b.guest=? or l.owner=?) and b.co>=? order by b.ci",
                    (uid, uid, addd(today(), -120)))
    if not bs:
        return await say(chat, "Підтверджених угод поки немає.", [[("🏠 Меню", "m:m")]])
    for b in bs[:8]:
        ls = await db.one("select id from leases where booking_id=?", (b["id"],))
        rows = [[("📋 Акт заселення", f"z:aa:{b['id']}:in"), ("📋 Акт виїзду", f"z:aa:{b['id']}:out")]]
        if ls:
            rows.append([("📄 Оренда: договір і платежі", f"z:ls:{ls['id']}")])
        rows.append([("⚠️ Претензія / питання", f"z:cl:{b['id']}:{'d' if b['owner'] == uid else 'r'}")])
        await say(chat, f"<b>{esc(b['title'])}</b>\n📅 {b['ci']} → {b['co']} · {'ви господар' if b['owner'] == uid else 'ви гість'}", rows)


# ═════════════════════════ 5. ЗРУЧНОСТІ, МАПА, ФІЛЬТРИ, ПОРІВНЯННЯ, AI ═════════════════════════
async def filter_extra(L: list, f: dict) -> list:
    am = set(f.get("amen") or [])
    if am:
        L = [x for x in L if am <= set(filter(None, (x.get("amen") or "").split(",")))]
    if f.get("near"):
        la, lo = f["near"]
        rad = f.get("radius") or 3
        out = []
        for x in L:
            if x.get("lat") is not None and x.get("lon") is not None:
                x["_km"] = km(la, lo, x["lat"], x["lon"])
                if x["_km"] <= rad:
                    out.append(x)
        L = out
    return L


def search_rows(f: dict) -> list:
    rows = [[(f"🏷 Фільтри{' (' + str(len(f.get('amen') or [])) + ')' if f.get('amen') else ''}", "z:fl"), ("📍 Поруч зі мною", "z:nr")]]
    if f.get("near"):
        r = f.get("radius") or 3
        rows.append([((f"✓{v} км" if v == r else f"{v} км"), f"z:rad:{v}") for v in (1, 3, 5, 10)])
    return rows


async def flt_menu(chat: int, uid: int, msg=None):
    s = S().setdefault(uid, {})
    f = s.setdefault("f", {"city": "", "district": "", "month": False, "max": 0, "rooms": 0, "guests": 0})
    am = set(f.get("amen") or [])
    keys = list(AMEN)
    rows = [[((("✓ " if k in am else "") + AMEN[k][0] + " " + AMEN[k][1]), f"z:ft:{k}")] for k in keys]
    rows.append([("🔍 Показати житло", "z:fg")])
    text = "🏷 <b>Що важливо для вас?</b> Оберіть потрібне, і я покажу лише такі оголошення:"
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=kb(rows))
        except Exception:
            return
    await say(chat, text, rows)


async def card_extra(l: dict, host: dict, f: dict, diff: int):
    lines = []
    if host and host.get("verified") == 2:
        lines.append("✅ Верифікований господар")
    am = amen_labels(l.get("amen"))
    if am:
        lines.append(" · ".join(am[:6]))
    if f and f.get("near") and l.get("lat") is not None:
        lines.append(f"📍 {km(f['near'][0], f['near'][1], l['lat'], l['lon']):.1f} км від вас")
    dm = l.get("dep_mode") or "cash"
    lines.append({"cash": "💵 Оплата при заселенні", "card": "💳 Передоплата господарю напряму", "trust": "🤝 Без передоплати для перевірених гостей"}.get(dm, "") + (f" · залог {uah(l['dep_amt'])}" if l.get("dep_amt") else ""))
    if diff <= -50:
        lines.append("⚠️ Ціна вдвічі нижча за середню: уважно перевіряйте, не переказуйте гроші поза ботом.")
    rows = [[("⚖️ Порівняти", f"z:cp:a:{l['id']}"), ("🤖 AI про житло", f"z:qa:{l['id']}")]]
    if l.get("lat") is not None:
        rows[0].append(("🗺 На мапі", f"z:mp:{l['id']}"))
    return "\n".join(x for x in lines if x), rows


async def show_map(chat: int, lid: int):
    l = await db.one("select title, lat, lon from listings where id=?", (lid,))
    if not l or l["lat"] is None:
        return await say(chat, "Господар ще не вказав розташування.")
    await B.bot.send_location(chat, round(l["lat"], 2), round(l["lon"], 2))
    await say(chat, "📍 Це <b>приблизне</b> розташування (до ~1 км). Точну адресу побачите після підтвердження бронювання.")


async def cmp_show(chat: int, uid: int):
    L = await db.q("select l.* from cmp c join listings l on l.id=c.listing_id where c.user=? order by c.rowid", (uid,))
    if not L:
        return await say(chat, "Порівняння порожнє. У картці оголошення натисніть «⚖️ Порівняти» (до 3 штук).", [[("🔍 Знайти житло", "m:s")]])
    mark = "ABC"
    out, btn = [], []
    cheap = min(L, key=lambda x: x["price_night"])["id"]
    for i, l in enumerate(L):
        ra, rn = await B.rating(lid=l["id"])
        h = await db.one("select verified from users where id=?", (l["owner"],))
        out.append(f"<b>{mark[i]} · {esc(l['title'])}</b>{' 💰 найдешевше' if len(L) > 1 and l['id'] == cheap else ''}\n📍 {esc(l['city'])}, {esc(l['district'])} · 🛏 {l['rooms']} · 👥 до {l['guests']}\n"
                   f"💰 {uah(l['price_night'])}/ніч{' · ' + uah(l['price_month']) + '/міс' if l['price_month'] else ''}\n{B.stars(ra, rn)}{' · ✅ верифікований' if h and h['verified'] == 2 else ''}\n"
                   f"{' · '.join(amen_labels(l['amen'])[:5]) or 'Зручності не вказано'}\n{DEP_MODES.get(l['dep_mode'] or 'cash', '')}")
        btn.append([(f"🔎 {mark[i]}", f"lc:{l['id']}"), (f"🗑 {mark[i]}", f"z:cp:d:{l['id']}")])
    await say(chat, "⚖️ <b>Порівняння</b>\n\n" + "\n\n".join(out), btn + [[("🤖 Порада AI", "z:cp:ai"), ("🧹 Очистити", "z:cp:c")], [("🏠 Меню", "m:m")]])


async def cmp_ai(chat: int, uid: int):
    L = await db.q("select l.* from cmp c join listings l on l.id=c.listing_id where c.user=? order by c.rowid", (uid,))
    if len(L) < 2:
        return await say(chat, "Додайте щонайменше два оголошення для порівняння.")
    data = [{k: x[k] for k in ("title", "city", "district", "price_night", "price_month", "rooms", "guests", "min_nights", "dep_mode", "dep_amt")} | {"amen": amen_labels(x["amen"])} for x in L]
    t = (await B.gemini("Коротко (до 80 слів) українською порівняй ці варіанти житла й порадь, кому що підійде. Використовуй лише наведені дані, нічого не вигадуй. "
                        + json.dumps(data, ensure_ascii=False)[:1800])).strip()
    await say(chat, ("🤖 " + esc(t)) if t else "AI-помічник не підключено (необов'язково): додайте GEMINI_API_KEY у файл .env. Порівняння вище працює й без нього.")


async def ask_ai_listing(chat: int, u: dict, lid: int, q: str):
    l = await db.one("select * from listings where id=?", (lid,))
    if not l:
        return
    ra, rn = await B.rating(lid=lid)
    facts = {"назва": l["title"], "місто": l["city"], "район": l["district"], "кімнат": l["rooms"], "гостей": l["guests"], "ціна_за_ніч": l["price_night"], "ціна_за_місяць": l["price_month"],
             "мін_ночей": l["min_nights"], "опис": l["descr"], "зручності": amen_labels(l["amen"]), "оплата": DEP_MODES.get(l["dep_mode"] or "cash"), "залог": l["dep_amt"], "рейтинг": ra}
    t = (await B.gemini("Ти помічник сервісу оренди. Відповідай українською, 1-3 речення, ЛИШЕ за цими даними оголошення. Якщо відповіді в даних немає, так і скажи й порадь запитати господаря. "
                        f"Дані: {json.dumps(facts, ensure_ascii=False)[:1800]}\nПитання гостя: {q[:300]}")).strip()
    S().pop(u["id"], None)
    await say(chat, (f"🤖 {esc(t)}\n\n<i>Відповідь за даними оголошення; важливе уточніть у господаря.</i>") if t else "AI-помічник не підключено. Запитайте господаря напряму.",
              [[("💬 Запитати господаря", f"ask:{lid}"), ("⬅ До оголошення", f"lc:{lid}")]])


async def am_menu(chat: int, uid: int, lid: int, msg=None):
    l = await db.one("select amen from listings where id=? and owner=?", (lid, uid))
    if not l:
        return
    cur = set(filter(None, (l["amen"] or "").split(",")))
    rows = [[((("✓ " if k in cur else "") + AMEN[k][0] + " " + AMEN[k][1]), f"z:am:{lid}:{k}")] for k in AMEN] + [[("⬅ До оголошення", f"ml:{lid}")]]
    text = "🏷 Позначте, що є у вашому житлі. Гості шукають за цими тегами, і це підвищує вашу видимість:"
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=kb(rows))
        except Exception:
            return
    await say(chat, text, rows)


async def dep_menu(chat: int, uid: int, lid: int):
    l = await db.one("select * from listings where id=? and owner=?", (lid, uid))
    if not l:
        return
    rows = [[(("✓ " if (l["dep_mode"] or "cash") == m else "") + t, f"z:dm:{lid}:{m}")] for m, t in DEP_MODES.items()]
    rows.append([(f"🔐 Залог за шкоду: {uah(l['dep_amt'])}", f"z:da:{lid}")])
    rows.append([("⬅ До оголошення", f"ml:{lid}")])
    await say(chat, "💳 <b>Як гість платить вам</b>\nГроші йдуть <b>напряму вам</b>, платформа їх не тримає.\n\n• <b>При заселенні</b>: гість платить на місці.\n• <b>Передоплата на картку</b>: гість переказує частину наперед, ви підтверджуєте кнопкою. Потрібна верифікація.\n"
                    "• <b>Довіра</b>: перевірені гості без передоплати, нові гості платять наперед.\n\n🔐 Залог повертається при виїзді за актом, якщо немає претензій.", rows)


# ═════════════════════════ ДИСПЕТЧЕР КНОПОК (префікс «z:») ═════════════════════════
async def cb(c, u: dict, a: list):
    chat, uid, k = c.message.chat.id, u["id"], a[0]
    adm = uid in B.ADMINS
    if k == "vm":
        return await verify_menu(chat, u)
    if k == "avl" and adm:
        return await verify_list(chat)
    if k == "av" and adm:
        return await verify_decide(chat, int(a[1]), a[2] == "1")
    if k == "acl" and adm:
        return await claims_admin(chat, int(a[1]) if len(a) > 1 else 0)
    if k == "cv" and adm:
        return await claim_decide(chat, int(a[1]), a[2] == "1")
    if k == "fl":
        return await flt_menu(chat, uid)
    if k == "ft":
        f = S().setdefault(uid, {}).setdefault("f", {"city": "", "district": "", "month": False, "max": 0, "rooms": 0, "guests": 0})
        f["amen"] = sorted(set(f.get("amen") or []) ^ {a[1]})
        return await flt_menu(chat, uid, c.message)
    if k == "fg":
        return await B.run_search(chat, uid, S().get(uid, {}).get("f") or {"city": "", "district": "", "month": False, "max": 0, "rooms": 0, "guests": 0})
    if k == "nr":
        s = S().setdefault(uid, {})
        s.setdefault("f", {"city": "", "district": "", "month": False, "max": 0, "rooms": 0, "guests": 0})
        s["flow"] = "z_near"
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
        return await B.bot.send_message(chat, "Поділіться розташуванням, і я покажу житло поруч (радіус можна змінити).",
                                        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Надіслати розташування", request_location=True)], [KeyboardButton(text="Не зараз")]],
                                                                         resize_keyboard=True, one_time_keyboard=True))
    if k == "rad":
        f = S().get(uid, {}).get("f")
        if f:
            f["radius"] = int(a[1])
            return await B.run_search(chat, uid, f)
        return
    if k == "cp":
        if a[1] == "a":
            n = (await db.one("select count(*) n from cmp where user=?", (uid,)))["n"]
            if n >= 3 and not await db.one("select 1 from cmp where user=? and listing_id=?", (uid, int(a[2]))):
                return await say(chat, "У порівнянні вже 3 оголошення: видаліть зайве.", [[("⚖️ Порівняння", "z:cp:s")]])
            await db.ex("insert or ignore into cmp values(?,?)", (uid, int(a[2])))
            return await say(chat, "Додано до порівняння ✓", [[("⚖️ Відкрити порівняння", "z:cp:s")]])
        if a[1] == "d":
            await db.ex("delete from cmp where user=? and listing_id=?", (uid, int(a[2])))
        if a[1] == "c":
            await db.ex("delete from cmp where user=?", (uid,))
        if a[1] == "ai":
            return await cmp_ai(chat, uid)
        return await cmp_show(chat, uid)
    if k == "qa":
        S()[uid] = {"flow": "z_qa", "lid": int(a[1])}
        return await say(chat, "🤖 Що хочете дізнатися про це житло? Напишіть питання (наприклад: «чи є укриття поруч?», «чи можна з собакою?»).", [CANCEL])
    if k == "mp":
        return await show_map(chat, int(a[1]))
    if k == "ai":
        return await say(chat, "🤖 <b>AI-помічник</b>\nПросто напишіть, що шукаєте, своїми словами. Наприклад:\n• «2 кімнати на місяць у центрі до 15000, з генератором»\n• «з собакою, поруч укриття»\n• «для сім'ї з дитиною, тиха вулиця»\n"
                               "Зручності (світло, укриття, тварини, ВПО…) я розпізную автоматично. Можна також натиснути «🏷 Фільтри» чи «📍 Поруч» у результатах.", [[("🔍 Знайти житло", "m:s"), ("🏠 Меню", "m:m")]])
    if k == "am" and len(a) >= 2:
        lid = int(a[1])
        l = await db.one("select amen from listings where id=? and owner=?", (lid, uid))
        if not l:
            return
        if len(a) == 3:
            cur = set(filter(None, (l["amen"] or "").split(","))) ^ {a[2]}
            await db.ex("update listings set amen=? where id=?", (",".join(x for x in AMEN if x in cur), lid))
            return await am_menu(chat, uid, lid, c.message)
        return await am_menu(chat, uid, lid)
    if k == "gl":
        if not await db.one("select 1 from listings where id=? and owner=?", (int(a[1]), uid)):
            return
        S()[uid] = {"flow": "z_gl", "lid": int(a[1])}
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
        return await B.bot.send_message(chat, "Надішліть геолокацію житла (скріпка → Геопозиція → оберіть точку на мапі). Гості бачитимуть лише приблизне місце (до ~1 км).",
                                        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Я зараз у житлі", request_location=True)], [KeyboardButton(text="Не зараз")]], resize_keyboard=True, one_time_keyboard=True))
    if k == "dm" and len(a) >= 2:
        lid = int(a[1])
        if len(a) == 3 and a[2] in DEP_MODES and await db.one("select 1 from listings where id=? and owner=?", (lid, uid)):
            await db.ex("update listings set dep_mode=? where id=?", (a[2], lid))
            if a[2] in ("card", "trust"):
                ok, msg = await host_ready(uid)
                if not ok:
                    await say(chat, "ℹ️ " + msg, [[("🛡 Верифікація", "z:vm"), ("💳 Реквізити", "m:pr")]])
        return await dep_menu(chat, uid, lid)
    if k == "da":
        if not await db.one("select 1 from listings where id=? and owner=?", (int(a[1]), uid)):
            return
        S()[uid] = {"flow": "z_da", "lid": int(a[1])}
        return await say(chat, "Сума залогу за можливу шкоду, ₴ (0 = без залогу). Його гість передає вам напряму при заселенні:", [CANCEL])
    if k == "tf":
        if not await db.one("select 1 from listings where id=? and owner=?", (int(a[1]), uid)):
            return
        S()[uid] = {"flow": "z_tf", "lid": int(a[1])}
        return await say(chat, "Тарифи для довгої оренди. Надішліть три числа через пробіл: <b>світло ₴/кВт·год, вода ₴/м³, газ ₴/м³</b>.\nНаприклад: <code>4.32 38 8</code> (0 = не нараховувати).", [CANCEL])
    if k == "gp":
        return await guest_paid(chat, uid, int(a[1]))
    if k == "hp":
        return await host_decide(chat, uid, int(a[1]), a[2] == "1")
    if k == "my":
        return await my_deals(chat, uid)
    if k == "aa":
        bid, kind = int(a[1]), a[2]
        act = a[3] if len(a) > 3 else ""
        d = await deal(bid, uid)
        if not d:
            return await say(chat, "Угода недоступна.")
        if act == "p":
            S()[uid] = {"flow": "z_aph", "bid": bid, "kind": kind}
            return await say(chat, "Надішліть фото стану житла (до 10, можна по одному). Коли завершите, натисніть «Готово».", [[("✅ Готово", f"z:aa:{bid}:{kind}")]])
        if act == "m":
            return await say(chat, "Який лічильник?", [[(v[0], f"z:ak:{bid}:{kind}:{m}") for m, v in MK.items()], [("⬅ Назад", f"z:aa:{bid}:{kind}")]])
        if act == "n":
            S()[uid] = {"flow": "z_ant", "bid": bid, "kind": kind}
            return await say(chat, "Напишіть нотатку до акта (стан, недоліки, передача ключів і залогу):", [CANCEL])
        if act == "ok":
            return await act_ok(chat, uid, bid, kind)
        if act == "v":
            return await send_photos(chat, json.loads((await act_get(bid, kind))["photos"]), "Фото акта")
        S().pop(uid, None)
        return await act_view(chat, uid, bid, kind)
    if k == "ak":
        S()[uid] = {"flow": "z_amt", "bid": int(a[1]), "kind": a[2], "mk": a[3]}
        return await say(chat, f"Введіть показник: {MK[a[3]][0]} ({MK[a[3]][1]}), число:", [CANCEL])
    if k == "dr":
        d = await deal(int(a[1]), uid)
        if d and d[1] == "host":
            await db.ex("update bookings set dep_back=1 where id=?", (int(a[1]),))
            await tell(d[0]["guest"], f"🔓 Господар відмітив, що залог за «{esc(d[0]['title'])}» повернено. Якщо це не так, подайте претензію.", [[("⚠️ Залог не повернуто", f"z:cl:{a[1]}:r")]])
            return await say(chat, "Відмічено ✓")
        return
    if k == "cl":
        return await claim_start(chat, uid, int(a[1]), a[2])
    if k == "ls":
        return await lease_view(chat, uid, int(a[1]))
    if k == "lf":
        return await lease_file(chat, uid, int(a[1]))
    if k == "ln":
        S()[uid] = {"flow": "z_ln", "lid": int(a[1])}
        return await say(chat, "Напишіть ваше прізвище, ім'я та по батькові повністю (для договору):", [CANCEL])
    if k == "lk":
        return await lease_accept(chat, uid, int(a[1]))
    if k == "lg":
        return await ledger_view(chat, uid, int(a[1]))
    if k == "lp":
        return await ledger_paid(chat, uid, int(a[1]))
    if k == "lr":
        return await ledger_confirm(chat, uid, int(a[1]), a[2] == "1")
    if k == "ur":
        return await utility_confirm(chat, uid, int(a[1]), a[2] == "1")
    if k == "mt":
        lid = int(a[1])
        if len(a) == 3:
            S()[uid] = {"flow": "z_met", "lease": lid, "mk": a[2]}
            return await say(chat, f"Введіть поточний показник: {MK[a[2]][0]} ({MK[a[2]][1]}), число:", [CANCEL])
        return await say(chat, "Який лічильник передаєте?", [[(v[0], f"z:mt:{lid}:{m}") for m, v in MK.items()], [("⬅ Угода", f"z:ls:{lid}")]])


# ═════════════════════════ ПОВІДОМЛЕННЯ У МАЙСТРАХ (стани «z_…») ═════════════════════════
async def on_msg(m, u: dict, s: dict, text: str) -> bool:
    chat, uid, fl = m.chat.id, u["id"], s.get("flow", "")
    if m.location and fl in ("z_gl", "z_near"):
        la, lo = m.location.latitude, m.location.longitude
        from aiogram.types import ReplyKeyboardRemove
        if fl == "z_gl":
            if await db.one("select 1 from listings where id=? and owner=?", (s["lid"], uid)):
                await db.ex("update listings set lat=?, lon=? where id=?", (la, lo, s["lid"]))
            S().pop(uid, None)
            await B.bot.send_message(chat, "Геолокацію збережено ✓", reply_markup=ReplyKeyboardRemove())
            return await B.listing_menu(chat, s["lid"], uid) or True
        f = s["f"]
        f["near"], f["radius"] = (la, lo), f.get("radius") or 3
        await B.bot.send_message(chat, "Шукаю поруч…", reply_markup=ReplyKeyboardRemove())
        await B.run_search(chat, uid, f)
        return True
    if text == "Не зараз" and fl in ("z_gl", "z_near"):
        from aiogram.types import ReplyKeyboardRemove
        S().pop(uid, None)
        await B.bot.send_message(chat, "Добре.", reply_markup=ReplyKeyboardRemove())
        await B.home(chat, u)
        return True
    if not fl.startswith("z_"):
        return False
    if fl == "z_vdoc":
        if not m.photo:
            await say(chat, "Надішліть саме фото документа.")
        else:
            s["doc"], s["flow"] = m.photo[-1].file_id, "z_vsel"
            await say(chat, "<b>Крок 2 з 2.</b> Тепер селфі (бажано з документом у руці), щоб було видно обличчя.", [CANCEL])
        return True
    if fl == "z_vsel":
        if not m.photo:
            await say(chat, "Надішліть селфі як фото.")
        else:
            await verify_submit(chat, u, s["doc"], m.photo[-1].file_id)
        return True
    if fl == "z_proof":
        if m.photo or m.document:
            b = await db.one("select b.listing_id, l.owner from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=?", (s["bid"], uid))
            if b:
                cap = f"📎 Скріншот переказу від гостя, бронь #{s['bid']}"
                (await B.bot.send_photo(b["owner"], m.photo[-1].file_id, caption=cap, reply_markup=kb([[("✅ Отримав", f"z:hp:{s['bid']}:1"), ("✖ Не отримав", f"z:hp:{s['bid']}:0")]]))
                 if m.photo else await B.bot.send_document(b["owner"], m.document.file_id, caption=cap, reply_markup=kb([[("✅ Отримав", f"z:hp:{s['bid']}:1"), ("✖ Не отримав", f"z:hp:{s['bid']}:0")]])))
            S().pop(uid, None)
            await say(chat, "Скріншот надіслано господарю ✓", [[("🏠 Меню", "m:m")]])
        else:
            await say(chat, "Надішліть фото чи файл квитанції або /menu.")
        return True
    if fl == "z_claim":
        if text:
            await claim_submit(chat, u, s, text)
        return True
    if fl == "z_aph":
        if not m.photo:
            await say(chat, "Надішліть фото або натисніть «Готово».", [[("✅ Готово", f"z:aa:{s['bid']}:{s['kind']}")]])
            return True
        a = await act_get(s["bid"], s["kind"])
        ph = json.loads(a["photos"])
        if len(ph) >= 10:
            await say(chat, "Максимум 10 фото.", [[("✅ Готово", f"z:aa:{s['bid']}:{s['kind']}")]])
            return True
        ph.append(m.photo[-1].file_id)
        await act_touch(s["bid"], s["kind"], photos=json.dumps(ph))
        await say(chat, f"Фото додано ({len(ph)}/10).", [[("✅ Готово", f"z:aa:{s['bid']}:{s['kind']}")]])
        return True
    if fl == "z_ant":
        if text:
            a = await act_get(s["bid"], s["kind"])
            who = "господар" if (await deal(s["bid"], uid) or (0, ""))[1] == "host" else "гість"
            await act_touch(s["bid"], s["kind"], notes=(a["notes"] + f"\n• {who}: {text[:500]}").strip()[:1800])
            S().pop(uid, None)
            await act_view(chat, uid, s["bid"], s["kind"])
        return True
    if fl == "z_amt":
        v = fnum(text) if text else None
        if v is None or v < 0:
            await say(chat, "Введіть число, наприклад 12345.6")
            return True
        a = await act_get(s["bid"], s["kind"])
        mt = json.loads(a["meters"])
        mt[s["mk"]] = v
        await act_touch(s["bid"], s["kind"], meters=json.dumps(mt))
        S().pop(uid, None)
        await act_view(chat, uid, s["bid"], s["kind"])
        return True
    if fl == "z_met":
        v = fnum(text) if text else None
        if v is None or v < 0:
            await say(chat, "Введіть число, наприклад 12345.6")
        else:
            await meter_submit(chat, u, s, v)
        return True
    if fl == "z_ln":
        if 5 <= len(text) <= 120:
            L, role = await lease_of(s["lid"], uid)
            if L:
                await db.ex(f"update leases set {'host_name' if role == 'host' else 'guest_name'}=? where id=?", (text, L["id"]))
            S().pop(uid, None)
            await say(chat, "Збережено ✓ Тепер завантажте договір.", [[("📄 Договір (файл)", f"z:lf:{s['lid']}")]])
        else:
            await say(chat, "Напишіть ПІБ повністю (5–120 символів).")
        return True
    if fl in ("z_da", "z_tf"):
        own = await db.one("select 1 from listings where id=? and owner=?", (s["lid"], uid))
        if not own:
            S().pop(uid, None)
            return True
        if fl == "z_da":
            if not text.isdigit() or int(text) > 10 ** 7:
                await say(chat, "Введіть ціле число (0 = без залогу).")
                return True
            await db.ex("update listings set dep_amt=? where id=?", (int(text), s["lid"]))
            S().pop(uid, None)
            return await dep_menu(chat, uid, s["lid"]) or True
        parts = [fnum(x) for x in text.split()]
        if len(parts) != 3 or any(p is None or p < 0 for p in parts):
            await say(chat, "Потрібні три числа через пробіл, наприклад: <code>4.32 38 8</code>")
            return True
        await db.ex("update listings set t_el=?, t_wat=?, t_gas=? where id=?", (*parts, s["lid"]))
        S().pop(uid, None)
        await say(chat, "Тарифи збережено ✓", [[("⬅ До оголошення", f"ml:{s['lid']}")]])
        return True
    if fl == "z_qa":
        if text:
            await ask_ai_listing(chat, u, s["lid"], text)
        return True
    return False


# ═════════════════════════ ФОНОВІ ЗАДАЧІ ═════════════════════════
async def tick():
    for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='awaiting_payment' and b.claim_at!='' and b.claim_rem=0 and b.claim_at<?", (B.now_s(-5),)):
        await tell(b["owner"], f"⏰ Гість чекає на підтвердження оплати за «{esc(b['title'])}» (#{b['id']}). Перевірте надходження:", [[("✅ Отримав", f"z:hp:{b['id']}:1"), ("✖ Не отримав", f"z:hp:{b['id']}:0")]])
        await db.ex("update bookings set claim_rem=1 where id=?", (b["id"],))
    for b in await db.q("select b.*, l.title from bookings b join listings l on l.id=b.listing_id where b.status='awaiting_payment' and b.claim_at!='' and b.claim_rem=1 and b.claim_at<?", (B.now_s(-24),)):
        for ad in B.ADMINS:
            await tell(ad, f"⚠️ Гість заявив про оплату, а господар мовчить понад добу: бронь #{b['id']} «{esc(b['title'])}».")
        await db.ex("update bookings set claim_rem=2 where id=?", (b["id"],))
    for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.ci<=? and b.co>? and b.act_in=0", (today(), today())):
        for who in (b["guest"], b["owner"]):
            await tell(who, f"📋 Сьогодні заселення: «{esc(b['title'])}». Складіть акт: фото стану й показники лічильників, це захистить обох.", [[("📋 Відкрити акт заселення", f"z:aa:{b['id']}:in")]])
        await db.ex("update bookings set act_in=1 where id=?", (b["id"],))
    for b in await db.q("select b.*, l.title, l.owner from bookings b join listings l on l.id=b.listing_id where b.status='confirmed' and b.co<=? and b.co>=? and b.act_out=0", (today(), addd(today(), -5))):
        for who in (b["guest"], b["owner"]):
            await tell(who, f"📋 Виїзд із «{esc(b['title'])}»: складіть акт виїзду (фото й показники), і розрахуйтесь за залог.", [[("📋 Відкрити акт виїзду", f"z:aa:{b['id']}:out")]])
        await db.ex("update bookings set act_out=1 where id=?", (b["id"],))
    # щомісячні платежі довгої оренди
    for L in await db.q("select l.*, b.ci, b.co, t.title from leases l join bookings b on b.id=l.booking_id join listings t on t.id=l.listing_id where b.status='confirmed' and b.ci<=? and b.co>?", (today(), today())):
        per = today()[:7]
        if today() < f"{per}-{L['pay_day']:02d}" or await db.one("select 1 from ledger where lease_id=? and kind='rent' and period=?", (L["id"], per)):
            continue
        rid = await db.ex("insert into ledger(lease_id,kind,period,amount) values(?,?,?,?)", (L["id"], "rent", per, L["rent"]))
        await tell(L["guest"], f"🏠 Оренда за {per}: <b>{uah(L['rent'])}</b> («{esc(L['title'])}»). Переказ напряму господарю. Заодно передайте показники лічильників.",
                   [[("✅ Сплатив(ла)", f"z:lp:{rid}"), ("🔢 Показники", f"z:mt:{L['id']}")]])
        await tell(L["host"], f"🏠 Нарахована оренда за {per}: {uah(L['rent'])} («{esc(L['title'])}»). Після переказу гість позначить оплату, а ви підтвердите.", [[("🧾 Платежі", f"z:lg:{L['id']}")]])
    for r in await db.q("select r.*, l.guest from ledger r join leases l on l.id=r.lease_id where r.status='due' and r.remind=0 and r.created<?", (B.now_s(-72),)):
        await tell(r["guest"], f"⏰ Нагадування: {KL.get(r['kind'], '')} за {r['period']}, {uah(r['amount'])}, ще не сплачено.", [[("✅ Сплатив(ла)", f"z:lp:{r['id']}")]])
        await db.ex("update ledger set remind=1 where id=?", (r["id"],))
