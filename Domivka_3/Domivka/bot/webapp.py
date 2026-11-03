"""Mini App «Домівки» v2: пошук, бронювання, мої поїздки, кабінет господаря.
Працює в тому ж процесі, що й бот, з тією ж базою й тією ж бізнес-логікою (bot.make_booking, decide_booking тощо)."""
import asyncio
import hashlib
import hmac
import html as htmllib
import io
import ipaddress
import json
import os
import logging
import re
import socket
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
from PIL import Image

from aiohttp import web
from aiogram.types import BufferedInputFile

import db
import feat
import growth
import market
import business
import billing
import cart
from version import VERSION
import listing_import
import deployment

B = None
RUNNERS = []
CACHE: dict = {}
HITS: dict = defaultdict(deque)
INDEX = Path(__file__).resolve().parent.parent / "webapp" / "index.html"
PROPERTY_TYPES = ("apartment", "house", "room", "commercial", "garage", "land", "other")


# ───── Імпорт оголошення за посиланням (власні оголошення господаря) ─────
IMPORTS: dict = defaultdict(deque)


async def safe_get(url: str, maxb: int, image: bool = False):
    return await listing_import.safe_fetch(url, maxb, image)



def to_jpeg(data: bytes):
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        if min(im.size) < 300:
            return None
        im.thumbnail((1600, 1600))
        o = io.BytesIO()
        im.save(o, "JPEG", quality=86)
        return o.getvalue()
    except Exception:
        return None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_listing_page(src: str, base: str) -> dict:
    """Дістає назву, опис, ціну, місто й фото з Open Graph та schema.org JSON-LD."""
    meta: dict = {}
    for m in re.finditer(r"<meta\b[^>]*>", src, re.I):
        t = m.group(0)
        k = re.search(r"(?:property|name)=[\"']([^\"']+)[\"']", t, re.I)
        v = re.search(r"content=[\"']([^\"']*)[\"']", t, re.I)
        if k and v:
            meta.setdefault(k.group(1).lower(), []).append(htmllib.unescape(v.group(1)))
    out = {"title": "", "descr": "", "price": None, "currency": "", "city": "", "rooms": None, "guests": None, "photos": []}
    imgs = list(meta.get("og:image", [])) + list(meta.get("og:image:url", []))
    ldname = ""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', src, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for o in _walk(data):
            if isinstance(o.get("name"), str) and not ldname and o.get("@type") not in ("BreadcrumbList", "ListItem", "Organization", "WebSite", "WebPage", "Person"):
                ldname = o["name"]
            if isinstance(o.get("description"), str) and len(o["description"]) > len(out["descr"]):
                out["descr"] = o["description"]
            im = o.get("image")
            for x in (im if isinstance(im, list) else [im]):
                if isinstance(x, dict):
                    x = x.get("url") or x.get("contentUrl")
                if isinstance(x, str):
                    imgs.append(x)
            of = o.get("offers") if isinstance(o.get("offers"), (dict, list)) else None
            for ofx in (of if isinstance(of, list) else [of]) if of else []:
                if isinstance(ofx, dict) and ofx.get("price") and out["price"] is None:
                    try:
                        out["price"], out["currency"] = float(str(ofx["price"]).replace(" ", "").replace(",", ".")), str(ofx.get("priceCurrency") or "")
                    except ValueError:
                        pass
            ad = o.get("address")
            if isinstance(ad, dict) and ad.get("addressLocality") and not out["city"]:
                out["city"] = str(ad["addressLocality"])[:60]
            if o.get("numberOfRooms") and out["rooms"] is None:
                try:
                    out["rooms"] = int(float(str(o["numberOfRooms"]).replace(",", ".")))
                except ValueError:
                    pass
    out["title"] = (meta.get("og:title") or [""])[0] or ldname or ((re.search(r"<title[^>]*>(.*?)</title>", src, re.I | re.S) or [None, ""])[1]).strip()
    out["title"] = htmllib.unescape(re.sub(r"\s+", " ", out["title"]))[:120]
    out["descr"] = (out["descr"] or (meta.get("og:description") or meta.get("description") or [""])[0]).strip()[:2000]
    if out["price"] is None:
        pa = (meta.get("product:price:amount") or meta.get("og:price:amount") or [""])[0]
        try:
            out["price"], out["currency"] = float(pa.replace(" ", "").replace(",", ".")), (meta.get("product:price:currency") or meta.get("og:price:currency") or [""])[0]
        except ValueError:
            m = re.search(r"(\d[\d \u00a0]{2,8})\s*(?:грн|₴|UAH)", out["title"] + " " + out["descr"])
            if m:
                out["price"], out["currency"] = float(re.sub(r"\D", "", m.group(1))), "UAH"
    if out["rooms"] is None:
        m = re.search(r"(\d)[ -]?(?:кімн|комн|к\.|room)", out["title"] + " " + out["descr"], re.I)
        out["rooms"] = int(m.group(1)) if m else None
    seen = []
    for u in imgs:
        u = urljoin(base, u.strip())
        if u.startswith("http") and u not in seen and not re.search(r"\.(svg|ico|gif)(\?|$)", u, re.I):
            seen.append(u)
    out["photos"] = seen[:8]
    return out


def tg_user(init: str, token: str):
    """Перевірка підпису Telegram (initData). Повертає словник користувача або None."""
    try:
        pairs = parse_qsl(init, keep_blank_values=True)
        d = dict(pairs)
        if len(pairs) != len(d):
            return None
        h = d.pop("hash")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        age = time.time() - int(d["auth_date"])
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h) or not -30 <= age <= 86400:
            return None
        user = json.loads(d["user"])
        return user if isinstance(user, dict) and isinstance(user.get("id"), int) and not isinstance(user["id"], bool) and user["id"] > 0 else None
    except Exception:
        return None


def valid(init: str, token: str):
    u = tg_user(init, token)
    return u["id"] if u else None


def fail(msg: str, code: str = "err", status: int = 400):
    return web.json_response({"error": msg, "code": code}, status=status)


async def body(r) -> dict:
    try:
        j = await r.json()
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


WARNED: dict = {}


def warn_once(key: str, msg: str):
    if time.time() - WARNED.get(key, 0) > 600:
        WARNED[key] = time.time()
        logging.warning(msg)


async def auth(r):
    init = r.headers.get("X-Init", "")
    tu = tg_user(init, B.bot.token)
    if not tu and r.path == "/api/me":
        warn_once("noinit" if not init else "badinit", "Mini App: " + ("запит без підпису Telegram: сторінку відкрито НЕ через Telegram (відкривайте кнопкою «Домівка» біля поля введення або /app у боті)."
                                                                       if not init else "підпис Telegram не збігається: перевірте, що Mini App відкрито саме з цього бота (той самий BOT_TOKEN)."))
    u = await db.one("select * from users where id=?", (tu["id"],)) if tu else None
    return tu, u


class Stop(Exception):
    def __init__(self, resp):
        self.resp = resp


async def need(r, host: bool = False) -> dict:
    tu, u = await auth(r)
    if not tu:
        raise Stop(fail("Відкрийте додаток у Telegram, щоб користуватися акаунтом.", "tg", 401))
    if not u:
        raise Stop(fail("Потрібна реєстрація.", "reg", 401))
    if u["banned"]:
        raise Stop(fail("Акаунт заблоковано адміністрацією.", "ban", 403))
    now = time.time()
    q = HITS[u["id"]]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 60:
        raise Stop(fail("Забагато запитів. Зачекайте хвилину.", "rate", 429))
    q.append(now)
    if host and u["role"] != "host":
        raise Stop(fail("Це доступно лише господарям.", "host", 403))
    return u


def handler(fn):
    async def w(r):
        try:
            return await fn(r)
        except Stop as e:
            return e.resp
        except web.HTTPException:
            raise
        except Exception:
            B.logging.exception("webapp: помилка в %s", fn.__name__)
            return fail("Сталася помилка. Спробуйте ще раз.", "srv", 500)
    return w


def amen_map() -> dict:
    return {k: f"{e} {t}" for k, (e, t, _) in feat.AMEN.items()}


async def item(l: dict, full: bool = False, uid: int = 0) -> dict:
    h = await db.one("select name, verified, (select 1 from subs s where s.user=users.id and s.until>=date('now')) pro from users where id=?", (l["owner"],))
    o = {"id": l["id"], "title": l["title"], "property_type": l.get("property_type", "apartment"), "city": l["city"], "district": l["district"], "pn": l["price_night"], "pm": l["price_month"], "rooms": l["rooms"],
         "guests": l["guests"], "rating": round(l["ra"], 1) if l.get("ra") else None, "rn": l.get("rn", 0), "ver": bool(h and h["verified"] == 2), "pro": bool(h and h["pro"]), "top": B.is_boosted(l),
         "np": len(json.loads(l["photos"])), "amen": [x for x in (l["amen"] or "").split(",") if x], "instant": bool(l["instant"]), "disc": l["discount"], "min": l["min_nights"], "club": l["club_pct"],
         "lat": round(l["lat"], 3) if l["lat"] is not None else None, "lon": round(l["lon"], 3) if l["lon"] is not None else None}
    if "_km" in l:
        o["km"] = round(l["_km"], 1)
    if full:
        o.update(descr=l["descr"], host=h["name"] if h else "", dep=l["dep_mode"], dep_amt=l["dep_amt"], mine=l["owner"] == uid)
    return o


def quality(l: dict, ver: bool):
    """Оцінка якості оголошення (0–100) і поради, що поліпшити."""
    sc, tips = 0, []
    n = len(json.loads(l["photos"]))
    checks = [(n >= 5, 25, f"Додайте фото: зараз {n}, потрібно мінімум 5"), (len(l["descr"] or "") >= 120, 20, "Опишіть житло детальніше (від 120 символів)"),
              (l["lat"] is not None, 10, "Позначте місце на мапі: так вас знайдуть поруч"), (len([x for x in (l["amen"] or "").split(",") if x]) >= 4, 15, "Вкажіть від 4 зручностей: за ними шукають гості"),
              (bool(l["address"]), 10, "Додайте адресу й інструкції заселення"), (bool(l["club_pct"]), 5, "Увімкніть знижку Клубу (наприклад, 5%): більше бронювань від постійних гостей"),
              (ver, 10, "Пройдіть верифікацію: з нею довіряють більше"), (bool(l["instant"]), 5, "Миттєве бронювання підвищує кількість бронювань")]
    for ok, pts, tip in checks:
        if ok:
            sc += pts
        else:
            tips.append(tip)
    return sc, tips[:4]


RATED = ("select l.*, (select avg(rating) from reviews r where r.listing_id=l.id and r.kind='guest') ra, "
         "(select count(*) from reviews r where r.listing_id=l.id and r.kind='guest') rn from listings l")


async def pay_block(b: dict, l: dict) -> dict:
    """Що має зробити гість для оплати (для бронювань у статусі «очікує оплати»)."""
    dep = B.deposit_of(b["total"])
    base = {"amount": dep, "rest": b["total"] - dep, "code": b["id"], "due": b["due_by"], "claimed": b.get("method") == "claimed"}
    if B.PAYMODE == "direct":
        h = await db.one("select name, payout from users where id=?", (l["owner"],))
        return {**base, "kind": "card", "to": h["payout"], "host": h["name"]}
    return {**base, "kind": "chat", "to": B.PAY_REQ if B.PAYMODE == "manual" else ""}


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def make_app(mod) -> web.Application:
    global B
    B = mod

    @handler
    async def market_api(r):
        if r.method == "GET" and r.path.startswith("/api/market/photo/"):
            _, viewer = await auth(r)
            if viewer and viewer["banned"]:
                return fail("Доступ обмежено.", "ban", 403)
            return await market.handle(r, viewer["id"] if viewer else 0)
        u = await need(r)
        return await market.handle(r, u["id"])

    @handler
    async def business_api(r):
        u = await need(r)
        try:
            data = dict(r.query) if r.method == "GET" else await r.json()
            path = r.path.removeprefix("/api/business") or "/"
            if path == "/billing/invoice" and r.method == "POST":
                return web.json_response(await billing.invoice(u["id"], data.get("offer_id")))
            if path == "/admin/refund-stars" and r.method == "POST":
                return web.json_response(await billing.refund(u["id"], data.get("invoice_id")))
            return web.json_response(await business.dispatch(u["id"], path, data, r.method))
        except (business.BusinessError, billing.BillingError) as e:
            return fail(e.message, "business", e.status)
        except (ValueError, TypeError):
            return fail("Некоректний запит.", "business")

    @handler
    async def cart_api(r):
        u = await need(r)
        try:
            data = dict(r.query) if r.method == "GET" else await r.json()
            return web.json_response(await cart.dispatch(u["id"], r.path.removeprefix("/api/cart") or "/", data, r.method))
        except market.MarketError as e:
            return fail(e.message, "cart", e.status)
        except (ValueError, TypeError):
            return fail("Некоректний запит.", "cart")

    @handler
    async def market_import(r):
        u = await need(r)
        q = IMPORTS[u["id"]]
        now = time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= 15:
            return fail("Забагато імпортів. Спробуйте пізніше.", "rate", 429)
        q.append(now)
        try:
            return web.json_response(await listing_import.draft((await body(r)).get("url", ""), parse_listing_page))
        except ValueError as e:
            return fail(str(e), "import")

    async def asset(r):
        # Explicit filenames: no arbitrary file access or directory listing.
        name = r.match_info["name"]
        if name not in {"market.js", "design.css", "settings.js"}:
            raise web.HTTPNotFound()
        path = INDEX.parent / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def identity(r):
        return web.json_response({"service": "domivka", "version": VERSION, "bot_id": B.bot.id, "bot_username": B.BOT_USERNAME}, headers={"Cache-Control": "no-store"})

    async def ping(r):
        return web.json_response({"ok": True, "listings": (await db.one(f"select count(*) n from listings where {B.VIS}"))["n"]})

    async def index(r):
        return web.Response(text=INDEX.read_text(encoding="utf-8").replace("__BOT__", B.BOT_USERNAME), content_type="text/html", headers={"Cache-Control": "no-cache"})

    @handler
    async def meta(r):
        rows = await db.q(f"select city, district from listings where {B.VIS}")
        ds: dict = {}
        for x in rows:
            ds.setdefault(x["city"], set()).add(x["district"])
        return web.json_response({"cities": sorted(ds), "districts": {c: sorted(v) for c, v in ds.items()}, "amen": amen_map(), "paymode": B.PAYMODE, "dep_pct": B.DEPOSIT,
                                 "free_cancel": B.FREE_CANCEL_H, "bot": B.BOT_USERNAME, "property_types": PROPERTY_TYPES})

    @handler
    async def me(r):
        tu, u = await auth(r)
        if not tu:
            return web.json_response({"tg": False})
        o = {"tg": True, "name": tu.get("first_name", ""), "reg": bool(u)}
        if u:
            bonus = await growth.daily(u["id"])
            u = await db.one("select * from users where id=?", (u["id"],))
            lv = growth.level(u["points"])
            o.update(id=u["id"], role=u["role"], phone=bool(u["phone"]), ver=u["verified"] == 2, admin=u["id"] in B.ADMINS, banned=bool(u["banned"]), points=u["points"],
                     level=lv["name"], lvl=lv["emoji"], club=growth.is_member(u["points"]), streak=u["streak"], bonus=bonus, pro=await B.is_pro(u["id"]), pro_until=await B.pro_until(u["id"]))
            if u["role"] == "host":
                o["req"] = (await db.one("select count(*) n from bookings b join listings l on l.id=b.listing_id where l.owner=? and (b.status='pending' or (b.status='awaiting_payment' and b.method='claimed'))", (u["id"],)))["n"]
            o["pay"] = (await db.one("select count(*) n from bookings where guest=? and status='awaiting_payment' and method!='claimed'", (u["id"],)))["n"]
            o["chat"] = (await db.one("select count(*) n from msgs m join convs c on c.id=m.conv_id join listings l on l.id=c.listing_id left join chat_reads r on r.conv_id=c.id and r.user=? "
                                      "where (c.guest=? or l.owner=?) and m.sender!=? and m.id>coalesce(r.last_id,0)", (u["id"], u["id"], u["id"], u["id"])))["n"]
        return web.json_response(o)

    @handler
    async def register(r):
        tu, u = await auth(r)
        if not tu:
            return fail("Відкрийте додаток у Telegram.", "tg", 401)
        j = await body(r)
        role = "host" if j.get("role") == "host" else "guest"
        if not u:
            name = " ".join(x for x in (tu.get("first_name"), tu.get("last_name")) if x) or "Гість"
            await db.ex("insert or ignore into users(id,name,username,role) values(?,?,?,?)", (tu["id"], name, tu.get("username"), role))
            if j.get("ref") and await growth.attach(tu["id"], j["ref"]):
                await B.tell(tu["id"], f"🎁 Вас запросив друг: +{growth.PTS['welcome']} балів у Клубі вже нараховано!")
        elif role == "host" and u["role"] != "host":
            await db.ex("update users set role='host' where id=?", (u["id"],))
        return await me(r)

    @handler
    async def listings(r):
        q = r.query
        tu, u = await auth(r)
        uid = u["id"] if u else 0
        sql, a = f"{RATED} where {B.VIS.replace('active', 'l.active').replace('blocked', 'l.blocked')}", []
        if q.get("property_type"):
            if q["property_type"] not in PROPERTY_TYPES:
                return fail("Невідомий тип нерухомості.", "property_type")
            sql += " and l.property_type=?"
            a.append(q["property_type"])
        if q.get("fav"):
            if not uid:
                return web.json_response([])
            sql += " and l.id in (select listing_id from favs where user=?)"
            a.append(uid)
        if q.get("city"):
            sql += " and l.city=?"
            a.append(q["city"])
        if q.get("district"):
            sql += " and l.district=?"
            a.append(q["district"])
        if q.get("rooms", "").isdigit() and int(q["rooms"]):
            sql += " and l.rooms>=?"
            a.append(int(q["rooms"]))
        if q.get("guests", "").isdigit() and int(q["guests"]):
            sql += " and l.guests>=?"
            a.append(int(q["guests"]))
        if q.get("top"):
            sql += " and l.boost_until>=date('now')"
        if q.get("instant"):
            sql += " and l.instant=1"
        rows = await db.q(sql + " order by l.id desc limit 400", tuple(a))
        month = q.get("month") == "1"
        if q.get("max", "").isdigit() and int(q["max"]):
            rows = [x for x in rows if B.eff(x, month) <= int(q["max"])]
        t = q.get("q", "").strip().lower()[:40]
        if t:  # SQLite не ігнорує регістр кирилиці, тому шукаємо в Python
            rows = [x for x in rows if t in f"{x['title']} {x['city']} {x['district']} {x['descr']}".lower()]
        f = {"amen": [x for x in q.get("amen", "").split(",") if x]}
        try:
            if q.get("lat"):
                f["near"] = (float(q["lat"]), float(q["lon"]))
                f["radius"] = min(50, max(1, float(q.get("radius", 5))))
        except ValueError:
            pass
        rows = await feat.filter_extra(rows, f)
        sort = q.get("sort", "price")
        key = (lambda x: x.get("_km", 1e9)) if f.get("near") and sort == "dist" else (lambda x: -(x["ra"] or 0)) if sort == "rating" else (lambda x: -x["id"]) if sort == "new" else (lambda x: B.eff(x, month))
        rows.sort(key=key)
        rows.sort(key=lambda x: not B.is_boosted(x))
        favs = {x["listing_id"] for x in await db.q("select listing_id from favs where user=?", (uid,))} if uid else set()
        out = []
        for x in rows[:80]:
            o = await item(x)
            o["fav"] = x["id"] in favs
            out.append(o)
        return web.json_response(out)

    @handler
    async def detail(r):
        tu, u = await auth(r)
        uid = u["id"] if u else 0
        lid = int(r.match_info["id"])
        can_review = bool(u and not u.get("banned") and (uid in B.ADMINS or business.role(uid) in ("owner","admin","moderator")))
        l = await db.one(f"{RATED} where l.id=? and ((l.active=1 and l.blocked=0) or l.owner=? or ?=1)", (lid, uid, int(can_review)))
        if not l:
            return web.json_response({}, status=404)
        if l["owner"] != uid:
            await db.ex("update listings set views=views+1 where id=?", (lid,))
        o = await item(l, True, uid)
        o["busy"] = sorted(d for d in await db.occupied(lid) if d >= B.today())
        rv = await db.q("select r.rating, r.text, r.created, u.name from reviews r join users u on u.id=r.author where r.listing_id=? and r.kind='guest' order by r.id desc limit 5", (lid,))
        o["reviews"] = [{"r": x["rating"], "t": x["text"], "d": x["created"][:10], "n": x["name"]} for x in rv]
        ha, hn = await B.rating(host=l["owner"])
        o["hrate"], o["hn"], o["sup"] = ha, hn, await B.is_super(l["owner"])
        o["pay_need"] = await feat.pay_needed(l, uid) if uid else (l["dep_mode"] or "cash") == "card"
        o["fav"] = bool(uid and await db.one("select 1 from favs where user=? and listing_id=?", (uid, lid)))
        return web.json_response(o)

    async def photo(r):
        lid, i = int(r.match_info["id"]), int(r.match_info["i"])
        _, viewer = await auth(r)
        uid = viewer["id"] if viewer and not viewer.get("banned") else 0
        can_review = bool(uid and (uid in B.ADMINS or business.role(uid) in ("owner","admin","moderator")))
        l = await db.one("select photos,active,blocked,owner from listings where id=? and ((active=1 and blocked=0) or owner=? or ?=1)", (lid, uid, int(can_review)))
        ph = json.loads(l["photos"]) if l else []
        if i < 0 or i >= len(ph):
            return web.Response(status=404)
        if ph[i] not in CACHE:
            buf = io.BytesIO()
            await B.bot.download_file((await B.bot.get_file(ph[i])).file_path, buf)
            if len(CACHE) > 150:
                CACHE.pop(next(iter(CACHE)))
            CACHE[ph[i]] = buf.getvalue()
        return web.Response(body=CACHE[ph[i]], content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400" if l["active"] and not l["blocked"] else "private, no-store"})

    @handler
    async def fav(r):
        u = await need(r)
        lid = int((await body(r)).get("id", 0))
        if await db.one("select 1 from favs where user=? and listing_id=?", (u["id"], lid)):
            await db.ex("delete from favs where user=? and listing_id=?", (u["id"], lid))
            return web.json_response({"fav": False})
        if not await db.one("select 1 from listings where id=?", (lid,)):
            return fail("Оголошення не знайдено.")
        await db.ex("insert into favs values(?,?)", (u["id"], lid))
        return web.json_response({"fav": True})

    @handler
    async def ai(r):
        text = str((await body(r)).get("text", ""))[:300].strip()
        if not text:
            return fail("Напишіть, що шукаєте.")
        f = await B.ai_filters(text)
        return web.json_response({k: f.get(k) for k in ("city", "district", "max", "rooms", "guests", "month", "amen")})

    @handler
    async def book(r):
        u = await need(r)
        j = await body(r)
        try:
            res = await B.make_booking(u, int(j["id"]), str(j["ci"]), str(j["co"]), int(j.get("guests", 1)))
        except (KeyError, ValueError, TypeError):
            return fail("Невірні дані бронювання.")
        if res.get("err") == "phone":
            return fail("Щоб бронювати, потрібен підтверджений номер телефону.", "phone", 409)
        if "err" in res:
            return fail(res["err"])
        out = {k: res[k] for k in ("bid", "status", "due", "total", "title")}
        if res["status"] == "awaiting_payment":
            b = await db.one("select * from bookings where id=?", (res["bid"],))
            out["pay"] = await pay_block(b, await db.one("select * from listings where id=?", (res["lid"],)))
        return web.json_response(out)

    @handler
    async def trips(r):
        u = await need(r)
        rows = await db.q("select b.*, l.title, l.owner, l.photos from bookings b join listings l on l.id=b.listing_id where b.guest=? order by b.id desc limit 40", (u["id"],))
        out = []
        for b in rows:
            h = await db.one("select name from users where id=?", (b["owner"],))
            reviewed = bool(await db.one("select 1 from reviews where booking_id=? and kind='guest'", (b["id"],)))
            o = {"id": b["id"], "lid": b["listing_id"], "title": b["title"], "np": len(json.loads(b["photos"])), "ci": b["ci"], "co": b["co"], "total": b["total"], "guests": b["guests"],
                 "status": b["status"], "host": h["name"] if h else "", "due": b["due_by"], "claimed": b["method"] == "claimed",
                 "can_cancel": b["status"] in ("pending", "awaiting_payment", "confirmed") and b["co"] > B.today(),
                 "can_review": b["status"] == "confirmed" and b["co"] <= B.today() and not reviewed, "reviewed": reviewed}
            if b["status"] == "awaiting_payment":
                o["pay"] = await pay_block(b, {"owner": b["owner"]})
            out.append(o)
        return web.json_response(out)

    @handler
    async def trip_cancel(r):
        u = await need(r)
        note = await B.cancel_booking(u["id"], int(r.match_info["id"]))
        if note is None:
            return fail("Бронювання не знайдено.", status=404)
        return web.json_response({"ok": True, "note": note.strip()})

    @handler
    async def trip_paid(r):
        u = await need(r)
        bid = int(r.match_info["id"])
        if B.PAYMODE != "direct":
            return fail("Підтвердити оплату можна в чаті бота.", "chat")
        await feat.guest_paid(u["id"], u["id"], bid)
        b = await db.one("select method from bookings where id=? and guest=?", (bid, u["id"]))
        return web.json_response({"ok": bool(b and b["method"] == "claimed")})

    @handler
    async def review(r):
        u = await need(r)
        j = await body(r)
        try:
            rid = await B.leave_review(u["id"], int(j.get("bid", 0)), int(j.get("rating", 0)), str(j.get("text", "")))
        except (ValueError, TypeError):
            rid = None
        return web.json_response({"ok": True}) if rid else fail("Відгук залишити неможливо (або вже залишено).")

    @handler
    async def host(r):
        u = await need(r)
        if u["role"] != "host":
            return web.json_response({"host": False})
        uid = u["id"]
        mine = []
        for l in await db.q(f"{RATED} where l.owner=? order by l.id desc", (uid,)):
            nb = (await db.one("select count(*) n from bookings where listing_id=? and status!='cancelled'", (l["id"],)))["n"]
            o = await item(l)
            sc, tips = quality(l, u["verified"] == 2)
            peers = sorted(x["price_night"] for x in await db.q(f"select price_night from listings where city=? and id!=? and rooms between ? and ? and {B.VIS}", (l["city"], l["id"], l["rooms"] - 1, l["rooms"] + 1)))
            mk = None
            if len(peers) >= 3:
                med = peers[len(peers) // 2]
                mk = {"med": med, "diff": round((l["price_night"] - med) / med * 100), "n": len(peers)}
            o.update(active=bool(l["active"]), blocked=l["blocked"], views=l["views"], nb=nb, boost=l["boost_until"] if B.is_boosted(l) else "",
                     favs=(await db.one("select count(*) n from favs where listing_id=?", (l["id"],)))["n"], chats=(await db.one("select count(*) n from convs where listing_id=?", (l["id"],)))["n"],
                     q=sc, tips=tips, market=mk)
            mine.append(o)
        reqs = []
        for b in await db.q("select b.*, l.title, u.name, u.phone from bookings b join listings l on l.id=b.listing_id join users u on u.id=b.guest "
                            "where l.owner=? and (b.status='pending' or (b.status='awaiting_payment' and b.method='claimed')) order by b.id desc", (uid,)):
            await db.ex("insert or ignore into convs(listing_id,guest) values(?,?)", (b["listing_id"], b["guest"]))
            cv = await db.one("select id from convs where listing_id=? and guest=?", (b["listing_id"], b["guest"]))
            reqs.append({"id": b["id"], "kind": "pay" if b["status"] == "awaiting_payment" else "req", "title": b["title"], "ci": b["ci"], "co": b["co"], "total": b["total"], "guests": b["guests"],
                         "guest": b["name"], "phone": b["phone"], "gl": await B.guest_line(b["guest"]), "amount": B.deposit_of(b["total"]), "due": b["due_by"], "cid": cv["id"]})
        up = await db.q("select b.*, l.title, u.name from bookings b join listings l on l.id=b.listing_id join users u on u.id=b.guest where l.owner=? and b.status='confirmed' and b.co>=date('now') order by b.ci", (uid,))
        ha, hn = await B.rating(host=uid)
        return web.json_response({"host": True, "listings": mine, "requests": reqs, "ver": u["verified"] == 2, "payout": bool(u["payout"]), "rating": ha, "rn": hn, "phone": bool(u["phone"]),
                                  "upcoming": [{"id": b["id"], "title": b["title"], "ci": b["ci"], "co": b["co"], "total": b["total"], "guest": b["name"], "paid": bool(b["paid"])} for b in up]})

    @handler
    async def host_decide(r):
        u = await need(r, True)
        j = await body(r)
        try:
            bid, ok = int(j.get("bid", 0)), bool(j.get("ok"))
        except (ValueError, TypeError):
            return fail("Невірні дані.")
        if j.get("kind") == "pay":
            if not await db.one("select 1 from bookings b join listings l on l.id=b.listing_id where b.id=? and l.owner=? and b.status='awaiting_payment'", (bid, u["id"])):
                return fail("Вже оброблено.")
            await feat.host_decide(u["id"], u["id"], bid, ok)
            return web.json_response({"ok": True, "msg": "Оплату підтверджено ✓" if ok else "Гостю повідомлено"})
        good, msg, _ = await B.decide_booking(u["id"], bid, ok)
        return web.json_response({"ok": good, "msg": msg}) if good else fail(msg)

    async def conv_of(cid: int, uid: int):
        c = await db.one("select c.*, l.title, l.owner, l.photos from convs c join listings l on l.id=c.listing_id where c.id=?", (cid,))
        return c if c and uid in (c["guest"], c["owner"]) else None

    @handler
    async def chats(r):
        u = await need(r)
        uid = u["id"]
        out = []
        for c in await db.q("select c.id, c.listing_id, c.guest, l.title, l.owner, l.photos from convs c join listings l on l.id=c.listing_id where c.guest=? or l.owner=?", (uid, uid)):
            last = await db.one("select id, sender, body, created from msgs where conv_id=? order by id desc limit 1", (c["id"],))
            if not last:
                continue
            other = await db.one("select name from users where id=?", (c["owner"] if c["guest"] == uid else c["guest"],))
            rd = await db.one("select last_id from chat_reads where conv_id=? and user=?", (c["id"], uid))
            unread = (await db.one("select count(*) n from msgs where conv_id=? and sender!=? and id>?", (c["id"], uid, rd["last_id"] if rd else 0)))["n"]
            out.append({"id": c["id"], "lid": c["listing_id"], "title": c["title"], "np": len(json.loads(c["photos"])), "other": other["name"] if other else "", "last": last["body"][:80],
                        "mine": last["sender"] == uid, "t": last["created"], "unread": unread, "key": last["id"]})
        out.sort(key=lambda x: -x["key"])
        return web.json_response(out)

    @handler
    async def chat_get(r):
        u = await need(r)
        c = await conv_of(int(r.match_info["id"]), u["id"])
        if not c:
            return fail("Чат не знайдено.", status=404)
        after = int(r.query.get("after", 0) or 0)
        ms = await db.q("select id, sender, body, created from msgs where conv_id=? and id>? order by id limit 200", (c["id"], after))
        if ms:
            await db.ex("insert into chat_reads(conv_id,user,last_id) values(?,?,?) on conflict(conv_id,user) do update set last_id=excluded.last_id", (c["id"], u["id"], ms[-1]["id"]))
        other = await db.one("select name, verified from users where id=?", (c["owner"] if c["guest"] == u["id"] else c["guest"],))
        return web.json_response({"title": c["title"], "lid": c["listing_id"], "np": len(json.loads(c["photos"])), "other": other["name"] if other else "", "ver": bool(other and other["verified"] == 2),
                                  "msgs": [{"id": m["id"], "me": m["sender"] == u["id"], "t": m["body"], "d": m["created"][11:16]} for m in ms]})

    @handler
    async def chat_send(r):
        u = await need(r)
        cid = int(r.match_info["id"])
        if not await conv_of(cid, u["id"]):
            return fail("Чат не знайдено.", status=404)
        text = str((await body(r)).get("text", "")).strip()
        if not text:
            return fail("Напишіть повідомлення.")
        res = await B.send_chat(u["id"], cid, text)
        if not res:
            return fail("Не вдалося надіслати.")
        await db.ex("insert into chat_reads(conv_id,user,last_id) values(?,?,?) on conflict(conv_id,user) do update set last_id=excluded.last_id", (cid, u["id"], res[0]))
        return web.json_response({"ok": True, "hidden": res[1]})

    @handler
    async def chat_start(r):
        u = await need(r)
        lid = int((await body(r)).get("lid", 0))
        l = await db.one("select id, owner from listings where id=?", (lid,))
        if not l:
            return fail("Оголошення не знайдено.", status=404)
        if l["owner"] == u["id"]:
            return fail("Це ваше оголошення.")
        await db.ex("insert or ignore into convs(listing_id,guest) values(?,?)", (lid, u["id"]))
        cv = await db.one("select id from convs where listing_id=? and guest=?", (lid, u["id"]))
        return web.json_response({"cid": cv["id"]})

    @handler
    async def alert(r):
        u = await need(r)
        j = await body(r)
        if (await db.one("select count(*) n from alerts where user=?", (u["id"],)))["n"] >= 5:
            return fail("Максимум 5 підписок. Видаліть зайву в боті: ⋯ Ще → Підписки.")
        try:
            mx = int(j.get("max") or 0)
        except (ValueError, TypeError):
            mx = 0
        await db.ex("insert into alerts(user,city,district,max,month) values(?,?,?,?,?)", (u["id"], str(j.get("city") or "")[:60], str(j.get("district") or "")[:60], mx, int(bool(j.get("month")))))
        return web.json_response({"ok": True})

    @handler
    async def club(r):
        u = await need(r)
        return web.json_response(await growth.snapshot(u["id"], B.BOT_USERNAME))

    @handler
    async def club_redeem(r):
        u = await need(r)
        j = await body(r)
        try:
            ok, msg = await growth.redeem(u["id"], str(j.get("id")), int(j.get("lid", 0)), B.apply_boost)
        except (ValueError, TypeError):
            return fail("Невірні дані.")
        return web.json_response({"ok": True, "msg": msg}) if ok else fail(msg)

    @handler
    async def trip_stay(r):
        u = await need(r)
        b = await db.one("select b.*, l.title, l.owner, l.address, l.lat, l.lon, l.city, l.district from bookings b join listings l on l.id=b.listing_id where b.id=? and b.guest=? and b.status='confirmed'",
                         (int(r.match_info["id"]), u["id"]))
        if not b:
            return fail("Дані для заселення доступні після підтвердження бронювання.", status=404)
        h = await db.one("select name, phone, username from users where id=?", (b["owner"],))
        cal = "https://calendar.google.com/calendar/render?" + urlencode({"action": "TEMPLATE", "text": f"Домівка: {b['title']}", "dates": f"{b['ci'].replace('-', '')}/{b['co'].replace('-', '')}",
                                                                          "details": f"Бронювання №{b['id']}. Господар: {h['name']} {h['phone'] or ''}", "location": b["address"] or f"{b['city']}, {b['district']}"})
        return web.json_response({"id": b["id"], "title": b["title"], "ci": b["ci"], "co": b["co"], "address": b["address"], "lat": b["lat"], "lon": b["lon"], "city": b["city"], "district": b["district"],
                                  "host": {"name": h["name"], "phone": h["phone"], "username": h["username"]}, "cal": cal, "lid": b["listing_id"]})

    @handler
    async def wishlist(r):
        uid = int(r.match_info["id"])
        o = await db.one("select name from users where id=?", (uid,))
        if not o:
            return web.json_response({}, status=404)
        rows = await db.q(f"{RATED} where l.id in (select listing_id from favs where user=?) and l.active=1 and l.blocked=0 limit 24", (uid,))
        return web.json_response({"name": (o["name"] or "Друг").split()[0], "items": [await item(x) for x in rows]})

    @handler
    async def host_describe(r):
        await need(r, True)
        j = await body(r)
        d = {k: str(j.get(k, ""))[:80] for k in ("title", "city", "district", "rooms", "guests")}
        d["amenities"] = str(j.get("amen", ""))[:200]
        return web.json_response({"text": await B.make_descr(d)})

    @handler
    async def host_cancel(r):
        u = await need(r, True)
        try:
            note = await B.host_cancel(u["id"], int((await body(r)).get("bid", 0)))
        except (ValueError, TypeError):
            note = None
        return web.json_response({"ok": True}) if note is not None else fail("Бронювання не знайдено.")

    @handler
    async def host_edit(r):
        u = await need(r, True)
        lid = int(r.match_info["id"])
        cur = await db.one("select * from listings where id=? and owner=?", (lid, u["id"]))
        if not cur:
            return fail("Оголошення не знайдено.", status=404)
        j = await body(r)
        upd = {}
        if "property_type" in j:
            if j["property_type"] not in PROPERTY_TYPES:
                return fail("Невідомий тип нерухомості.", "property_type")
            upd["property_type"] = j["property_type"]
        try:
            if "active" in j:
                upd["active"] = int(bool(j["active"]))
            if "instant" in j:
                upd["instant"] = int(bool(j["instant"]))
            if "price_night" in j:
                v = int(j["price_night"])
                if not 1 <= v <= 10 ** 7:
                    return fail("Невірна ціна за ніч.")
                upd["price_night"] = v
            if "price_month" in j:
                v = int(j["price_month"] or 0)
                upd["price_month"] = v if v > 0 else None
            if "discount" in j:
                v = int(j["discount"])
                if not 0 <= v <= 70:
                    return fail("Знижка від 0 до 70%.")
                upd["discount"] = v
            if "min_nights" in j:
                upd["min_nights"] = max(1, min(30, int(j["min_nights"])))
            if "club_pct" in j:
                upd["club_pct"] = max(0, min(15, int(j["club_pct"] or 0)))
            if j.get("lat") is not None and j.get("lon") is not None:
                la, lo = float(j["lat"]), float(j["lon"])
                if -90 <= la <= 90 and -180 <= lo <= 180:
                    upd["lat"], upd["lon"] = la, lo
        except (ValueError, TypeError):
            return fail("Невірні дані.")
        for k in ("title", "descr", "address"):
            if k in j:
                v = str(j[k]).strip()
                if k == "title" and not 5 <= len(v) <= 120:
                    return fail("Назва: від 5 до 120 символів.")
                upd[k] = v[:2000]
        for k, v in upd.items():
            await db.ex(f"update listings set {k}=? where id=?", (v, lid))
        if "price_night" in upd or "discount" in upd:
            await B.notify_price_drop(lid, B.eff(cur, False), B.eff(await db.one("select * from listings where id=?", (lid,)), False))
        return web.json_response({"ok": True})

    async def upload_photo(uid: int, data: bytes):
        try:
            m = await B.bot.send_photo(uid, BufferedInputFile(data, filename="p.jpg"), disable_notification=True)
            fid = m.photo[-1].file_id
            try:
                await B.bot.delete_message(uid, m.message_id)
            except Exception:
                pass
            return fid
        except Exception:
            return None

    @handler
    async def host_new(r):
        u = await need(r, True)
        if not u["phone"]:
            return fail("Спершу підтвердьте номер телефону.", "phone", 409)
        f = await r.post()
        g = lambda k, d="": str(f.get(k, d) or d).strip()
        property_type = g("property_type", "apartment")
        if property_type not in PROPERTY_TYPES:
            return fail("Невідомий тип нерухомості.", "property_type")
        try:
            d = {"title": g("title"), "city": g("city"), "district": g("district"), "price_night": int(g("price_night")), "price_month": int(g("price_month") or 0) or None,
                 "rooms": int(g("rooms", "1")), "guests": int(g("guests", "2")), "descr": g("descr")[:2000], "amen": [x for x in g("amen").split(",") if x],
                 "min_nights": int(g("min_nights", "1")), "instant": g("instant") == "1", "dep_mode": g("dep_mode", "cash"), "address": g("address")}
        except ValueError:
            return fail("Перевірте числові поля.")
        if not 5 <= len(d["title"]) <= 120 or not d["city"] or not d["district"] or len(d["city"]) > 60 or len(d["district"]) > 60:
            return fail("Заповніть назву (5–120 символів), місто й район.")
        if not 1 <= d["price_night"] <= 10 ** 7 or not 1 <= d["rooms"] <= 20 or not 1 <= d["guests"] <= 30:
            return fail("Перевірте ціну, кількість кімнат і гостей.")
        photos = []
        for fl in f.getall("photos", [])[:6]:
            data = fl.file.read(8 * 1024 * 1024)
            if data:
                fid = await upload_photo(u["id"], data)
                if not fid:
                    return fail("Не вдалося завантажити фото. Натисніть Start у боті й спробуйте ще раз.")
                photos.append(fid)
        try:
            urls = json.loads(g("photo_urls", "[]"))
        except ValueError:
            urls = []
        for pu in [x for x in urls if isinstance(x, str)][:6]:
            if len(photos) >= 6:
                break
            try:
                raw, _ct, _ = await safe_get(pu, 8 * 1024 * 1024, image=True)
                jpg = await asyncio.get_running_loop().run_in_executor(None, to_jpeg, raw)
                fid = await upload_photo(u["id"], jpg) if jpg else None
                if fid:
                    photos.append(fid)
            except Exception:
                continue
        d["photos"] = photos
        lid, blocked = await B.create_listing(u, d)
        if lid is None:
            return fail(blocked)
        await db.ex("update listings set property_type=? where id=?", (property_type, lid))
        if g("club_pct").isdigit():
            await db.ex("update listings set club_pct=? where id=?", (max(0, min(15, int(g("club_pct")))), lid))
        try:
            la, lo = float(g("lat")), float(g("lon"))
            if -90 <= la <= 90 and -180 <= lo <= 180:
                await db.ex("update listings set lat=?, lon=? where id=?", (la, lo, lid))
        except ValueError:
            pass
        return web.json_response({"ok": True, "id": lid, "blocked": blocked})

    @handler
    async def host_import(r):
        u = await need(r, True)
        q = IMPORTS[u["id"]]
        now = time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= 15:
            return fail("Забагато імпортів за годину. Спробуйте пізніше.", "rate", 429)
        q.append(now)
        url = str((await body(r)).get("url", "")).strip()[:600]
        if not re.match(r"^https?://", url):
            return fail("Вставте повне посилання, що починається з http:// або https://")
        try:
            raw, _ct, final = await safe_get(url, 1_500_000)
            res = parse_listing_page(raw.decode("utf-8", "ignore"), final)
        except Exception:
            return fail("Не вдалося відкрити сторінку. Перевірте посилання або заповніть оголошення вручну.")
        if not res["title"] and not res["photos"]:
            return fail("На сторінці не знайшлося даних оголошення. Заповніть вручну.")
        res["source"] = urlparse(final).hostname or ""
        logging.info("import listing uid=%s src=%s", u["id"], final[:200])
        return web.json_response(res)

    @handler
    async def pro_info(r):
        u = await need(r, True)
        n = (await db.one("select count(*) n from listings where owner=? and blocked!=1", (u["id"],)))["n"]
        pro = await B.is_pro(u["id"])
        return web.json_response({"pro": pro, "until": await B.pro_until(u["id"]), "listings": n, "free": B.PRO["free"], "max": B.PRO["max"], "boost_off": B.PRO["boost_off"],
                                  "plans": [{"days": d, "stars": s} for d, s in sorted(B.PRO["stars"].items()) if s > 0], "boost": {str(d): B.feat.boost_price(d) for d in (3, 7, 30)} if B.feat.stars_on() else {}})

    @handler
    async def pro_buy(r):
        u = await need(r, True)
        days = int((await body(r)).get("days", 0) or 0)
        amount = B.PRO["stars"].get(days, 0)
        if not amount:
            return fail("Такого тарифу немає.")
        link = await B.bot.create_invoice_link(title="Домівка Pro", description=f"Тариф для рієлторів і власників кількох квартир, {days} дн.", payload=f"pro:{days}", provider_token="", currency="XTR",
                                               prices=[B.LabeledPrice(label=f"Pro {days} дн.", amount=amount)])
        return web.json_response({"link": link})

    @handler
    async def boost_buy(r):
        u = await need(r, True)
        j = await body(r)
        lid, days = int(j.get("lid", 0) or 0), int(j.get("days", 0) or 0)
        l = await db.one("select * from listings where id=? and owner=?", (lid, u["id"]))
        if not l:
            return fail("Оголошення не знайдено.", "nf", 404)
        if days not in (3, 7, 30) or not B.feat.stars_on():
            return fail("Платне просування зараз вимкнене.")
        amount = B.feat.boost_price(days)
        if await B.is_pro(u["id"]):
            amount = max(1, amount * (100 - B.PRO["boost_off"]) // 100)
        link = await B.bot.create_invoice_link(title="Просування «Топ»", description=f"{l['title'][:60]}: {days} дн.", payload=f"bo:{lid}:{days}", provider_token="", currency="XTR",
                                               prices=[B.LabeledPrice(label=f"{days} дн.", amount=amount)])
        return web.json_response({"link": link, "stars": amount})

    app = web.Application(client_max_size=60 * 1024 * 1024, middlewares=[no_cache])
    app.add_routes([web.get("/api/version", lambda r: web.json_response({"version": VERSION})),
                    web.route("*", "/api/business/{tail:.*}", business_api),
                    web.route("*", "/api/cart", cart_api), web.route("*", "/api/cart/{tail:.*}", cart_api),
                    web.get("/assets/{name}", asset), web.post("/api/market/import", market_import),
                    web.route("*", "/api/market/{tail:.*}", market_api)])
    app.add_routes([web.get("/", index), web.get("/api/ping", ping), web.get("/api/identity", identity), web.get("/api/club", club), web.post("/api/club/redeem", club_redeem), web.get("/api/trip/{id}/stay", trip_stay), web.get("/api/wishlist/{id}", wishlist), web.get("/api/meta", meta), web.get("/api/chats", chats), web.post("/api/chat/start", chat_start), web.get("/api/chat/{id}", chat_get), web.post("/api/chat/{id}", chat_send), web.post("/api/alert", alert), web.get("/api/me", me), web.post("/api/register", register), web.get("/api/listings", listings),
                    web.get("/api/listing/{id}", detail), web.get("/p/{id}/{i}", photo), web.post("/api/fav", fav), web.post("/api/ai", ai), web.post("/api/book", book),
                    web.get("/api/trips", trips), web.post("/api/trip/{id}/cancel", trip_cancel), web.post("/api/trip/{id}/paid", trip_paid), web.post("/api/review", review),
                    web.get("/api/host", host), web.post("/api/host/decide", host_decide), web.post("/api/host/cancel", host_cancel), web.post("/api/host/listing/{id}", host_edit),
                    web.post("/api/host/new", host_new), web.post("/api/host/describe", host_describe),
                    web.post("/api/host/import", host_import), web.get("/api/pro", pro_info), web.post("/api/pro/buy", pro_buy), web.post("/api/boost/buy", boost_buy)])
    deployment.add_webhook(app, mod)
    return app


async def start(mod, port: int) -> int:
    """Bind exactly the configured socket; Render supplies PORT and public host."""
    host, port = deployment.server_binding(port)
    runner = web.AppRunner(make_app(mod), access_log=None, shutdown_timeout=60)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    RUNNERS.append(runner)
    return port


async def stop():
    """Drain HTTP handlers before closing the bot session on webhook shutdown."""
    while RUNNERS:
        await RUNNERS.pop().cleanup()
