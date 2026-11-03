"""Власна база бота (SQLite). Один файл data/domivka.db, жодних зовнішніх сервісів."""
import asyncio
import aiosqlite

SCHEMA = """
create table if not exists users(
  id integer primary key, name text, username text, phone text,
  role text not null default 'guest', banned integer not null default 0, payout text default '',
  created text default (datetime('now')));
create table if not exists listings(
  id integer primary key autoincrement, owner integer not null, title text not null,
  city text not null, district text not null, price_night integer not null, price_month integer,
  rooms integer not null default 1, guests integer not null default 2, descr text default '',
  photos text not null default '[]', discount integer not null default 0,
  active integer not null default 1, blocked integer not null default 0,
  views integer not null default 0, boost_until text default '', free_boost_at text default '', instant integer not null default 0,
  min_nights integer not null default 1, address text default '', created text default (datetime('now')));
create table if not exists bookings(
  id integer primary key autoincrement, listing_id integer not null, guest integer not null,
  ci text not null, co text not null, guests integer not null default 1, total integer not null,
  status text not null default 'pending', rem integer not null default 0, askrev integer not null default 0, askrevh integer not null default 0,
  paid integer not null default 0, deposit integer not null default 0, commission integer not null default 0, due_by text default '',
  payout_status text default '', charge_id text default '', method text default '', cin_asked integer not null default 0,
  created text default (datetime('now')));
create table if not exists blocks(listing_id integer not null, d text not null, primary key(listing_id,d));
create table if not exists reviews(
  id integer primary key autoincrement, booking_id integer not null, kind text not null default 'guest', listing_id integer not null,
  author integer not null, subject integer not null, rating integer not null, text text default '',
  created text default (datetime('now')), unique(booking_id, kind));
create table if not exists favs(user integer not null, listing_id integer not null, primary key(user,listing_id));
create table if not exists convs(id integer primary key autoincrement, listing_id integer not null, guest integer not null, unique(listing_id,guest));
create table if not exists msgs(id integer primary key autoincrement, conv_id integer not null, sender integer not null, body text not null, created text default (datetime('now')));
create table if not exists reports(id integer primary key autoincrement, listing_id integer not null, reporter integer not null, reason text, status text default 'open', created text default (datetime('now')));
create table if not exists payments(id integer primary key autoincrement, kind text not null, ref integer not null, user integer not null, amount integer not null, method text, charge_id text, created text default (datetime('now')));
create table if not exists subs(user integer primary key, plan text not null default 'pro', until text not null);
create table if not exists alerts(id integer primary key autoincrement, user integer not null, city text default '', district text default '', max integer default 0, month integer default 0);
create table if not exists verif(user integer primary key, doc text default '', selfie text default '', status text default 'pending', created text default (datetime('now')));
create table if not exists phash(listing_id integer not null, fid text not null, h text not null, primary key(listing_id, fid));
create table if not exists cmp(user integer not null, listing_id integer not null, primary key(user, listing_id));
create table if not exists acts(id integer primary key autoincrement, booking_id integer not null, kind text not null, notes text default '', photos text not null default '[]', meters text not null default '{}', host_ok integer not null default 0, guest_ok integer not null default 0, done integer not null default 0, created text default (datetime('now')), unique(booking_id, kind));
create table if not exists leases(id integer primary key autoincrement, booking_id integer not null unique, listing_id integer not null, host integer not null, guest integer not null, host_name text default '', guest_name text default '', rent integer not null, deposit integer not null default 0, pay_day integer not null default 1, host_ok text default '', guest_ok text default '', created text default (datetime('now')));
create table if not exists ledger(id integer primary key autoincrement, lease_id integer not null, kind text not null, period text not null, amount integer not null, status text not null default 'due', note text default '', remind integer not null default 0, paid_at text default '', created text default (datetime('now')), unique(lease_id, kind, period));
create table if not exists meters(id integer primary key autoincrement, lease_id integer not null, kind text not null, value real not null, d text not null, by_user integer default 0);
create table if not exists claims(id integer primary key autoincrement, booking_id integer not null, by_user integer not null, against integer not null, kind text not null, text text default '', status text default 'open', created text default (datetime('now')));
create table if not exists chat_reads(conv_id integer not null, user integer not null, last_id integer not null default 0, primary key(conv_id,user));
create table if not exists points_log(id integer primary key autoincrement, user integer not null, kind text not null, ref text not null default '', pts integer not null, created text default (datetime('now')), unique(user, kind, ref));
create index if not exists ix_b_l on bookings(listing_id, status);
create index if not exists ix_l_c on listings(city, active, blocked);
"""

DB: aiosqlite.Connection
LOCK = asyncio.Lock()


ADD_COLS = {
    "users": [("payout", "text default ''"), ("verified", "integer not null default 0"), ("legal_name", "text default ''"), ("strikes", "integer not null default 0"),
              ("ref_by", "integer"), ("points", "integer not null default 0"), ("streak", "integer not null default 0"), ("last_open", "text default ''")],
    "listings": [("boost_until", "text default ''"), ("free_boost_at", "text default ''"), ("instant", "integer not null default 0"),
                 ("min_nights", "integer not null default 1"), ("address", "text default ''"),
                 ("dep_mode", "text default 'cash'"), ("dep_amt", "integer not null default 0"), ("amen", "text default ''"), ("lat", "real"), ("lon", "real"),
                 ("club_pct", "integer not null default 0"), ("t_el", "real not null default 0"), ("t_wat", "real not null default 0"), ("t_gas", "real not null default 0")],
    "bookings": [("askrevh", "integer not null default 0"), ("paid", "integer not null default 0"), ("deposit", "integer not null default 0"),
                 ("commission", "integer not null default 0"), ("due_by", "text default ''"), ("payout_status", "text default ''"),
                 ("charge_id", "text default ''"), ("method", "text default ''"), ("cin_asked", "integer not null default 0"),
                 ("claim_at", "text default ''"), ("claim_rem", "integer not null default 0"), ("act_in", "integer not null default 0"),
                 ("act_out", "integer not null default 0"), ("dep_back", "integer not null default 0"), ("pts_done", "integer not null default 0")],
}


async def init(path: str):
    global DB
    DB = await aiosqlite.connect(path)
    DB.row_factory = aiosqlite.Row
    await DB.execute("pragma journal_mode=wal")
    old_reviews = False
    cols = [r["name"] for r in await q("pragma table_info(reviews)")]
    if cols and "kind" not in cols:                       # стара структура відгуків → переносимо
        await DB.execute("alter table reviews rename to reviews_old")
        old_reviews = True
    await DB.executescript(SCHEMA)
    if old_reviews:
        await DB.execute("insert into reviews(id,booking_id,kind,listing_id,author,subject,rating,text,created) "
                         "select id,booking_id,'guest',listing_id,author,host,rating,text,created from reviews_old")
        await DB.execute("drop table reviews_old")
    for t, lst in ADD_COLS.items():                       # додаємо нові колонки до старої бази
        have = {r["name"] for r in await q(f"pragma table_info({t})")}
        for c, ddl in lst:
            if c not in have:
                await DB.execute(f"alter table {t} add column {c} {ddl}")
    await DB.commit()


async def q(sql: str, a: tuple = ()):
    cur = await DB.execute(sql, a)
    return [dict(r) for r in await cur.fetchall()]


async def one(sql: str, a: tuple = ()):
    r = await q(sql, a)
    return r[0] if r else None


async def ex(sql: str, a: tuple = ()):
    cur = await DB.execute(sql, a)
    await DB.commit()
    return cur.lastrowid


async def occupied(lid: int) -> set[str]:
    """Усі зайняті ночі: підтверджені/очікуючі бронювання + дати, закриті господарем."""
    import datetime as dt
    out = {r["d"] for r in await q("select d from blocks where listing_id=?", (lid,))}
    for b in await q("select ci,co from bookings where listing_id=? and status!='cancelled' and co>=date('now')", (lid,)):
        d = dt.date.fromisoformat(b["ci"])
        end = dt.date.fromisoformat(b["co"])
        while d < end:
            out.add(d.isoformat())
            d += dt.timedelta(days=1)
    return out


async def book(lid: int, guest: int, ci: str, co: str, guests: int, total: int, status: str = 'pending', due_by: str = ''):
    """Створює бронювання, лише якщо дати вільні (захист від подвійного бронювання)."""
    async with LOCK:
        if await one("select 1 from bookings where listing_id=? and status!='cancelled' and ci<? and co>?", (lid, co, ci)):
            return None
        if await one("select 1 from blocks where listing_id=? and d>=? and d<?", (lid, ci, co)):
            return None
        return await ex("insert into bookings(listing_id,guest,ci,co,guests,total,status,due_by) values(?,?,?,?,?,?,?,?)", (lid, guest, ci, co, guests, total, status, due_by))
