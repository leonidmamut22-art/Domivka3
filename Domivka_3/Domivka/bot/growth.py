"""Клуб Домівки: бали, рівні, рефералка, серії візитів.
Нагороди не коштують платформі грошей: безкоштовне просування, знижки Клубу від господарів-партнерів, статус."""
import datetime as dt

import db

LEVELS = [(0, "Гість", "🌱"), (100, "Мандрівник", "🧭"), (300, "Друг Домівки", "💎"), (700, "Амбасадор", "👑")]
PTS = {"welcome": 20, "phone": 10, "ref_phone": 20, "stay": 100, "host_stay": 50, "ref_stay": 30, "review": 20, "listing": 30, "ref_listing": 50, "daily": 2}
LABEL = {"welcome": "Бонус за приєднання", "phone": "Номер підтверджено", "ref_phone": "Друг приєднався", "stay": "Завершене проживання", "host_stay": "Гість виїхав без проблем",
         "ref_stay": "Друг пожив у житлі", "review": "Відгук про проживання", "listing": "Оголошення опубліковано", "ref_listing": "Друг-господар опублікував оголошення", "daily": "Візит у додаток", "redeem": "Обмін балів"}
REWARDS = {"boost3": ("🔥 3 дні «Топ» для оголошення", 60), "boost7": ("🔥 7 днів «Топ» для оголошення", 120)}
REF_CAP = 25      # скільки друзів можуть принести бали одному запрошувачу (захист від накрутки)


def level(pts: int) -> dict:
    i = max(k for k, (t, _, _) in enumerate(LEVELS) if pts >= t)
    nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    lo = LEVELS[i][0]
    return {"i": i, "name": LEVELS[i][1], "emoji": LEVELS[i][2], "next": nxt[0] if nxt else None, "next_name": nxt[1] if nxt else None,
            "pct": 100 if not nxt else round((pts - lo) / (nxt[0] - lo) * 100)}


def is_member(pts: int) -> bool:
    return (pts or 0) >= LEVELS[1][0]


async def award(uid: int, kind: str, ref, pts=None) -> bool:
    """Нараховує бали один раз для пари (користувач, подія, ref). Повертає True, якщо нараховано."""
    pts = PTS[kind] if pts is None else pts
    try:
        await db.ex("insert into points_log(user,kind,ref,pts) values(?,?,?,?)", (uid, kind, str(ref), pts))
    except Exception:
        return False
    await db.ex("update users set points=points+? where id=?", (pts, uid))
    return True


async def attach(uid: int, ref_uid) -> bool:
    """Прив'язка до запрошувача (лише для нового користувача, не до себе)."""
    try:
        ref_uid = int(ref_uid)
    except (TypeError, ValueError):
        return False
    if ref_uid == uid:
        return False
    u, r = await db.one("select ref_by from users where id=?", (uid,)), await db.one("select id from users where id=?", (ref_uid,))
    if not u or not r or u["ref_by"]:
        return False
    await db.ex("update users set ref_by=? where id=?", (ref_uid, uid))
    await award(uid, "welcome", 0)
    return True


async def _ref_ok(ref_uid: int, kind: str) -> bool:
    return (await db.one("select count(*) n from points_log where user=? and kind=?", (ref_uid, kind)))["n"] < REF_CAP


async def on_phone(uid: int):
    """Телефон підтверджено: бали собі й запрошувачу. Повертає id запрошувача, якщо йому нараховано."""
    u = await db.one("select ref_by from users where id=?", (uid,))
    await award(uid, "phone", 0)
    if u and u["ref_by"] and await _ref_ok(u["ref_by"], "ref_phone") and await award(u["ref_by"], "ref_phone", uid):
        return u["ref_by"]
    return None


async def on_stay(b: dict) -> dict:
    """Проживання завершено (b: guest, owner, id). Повертає, кому що нараховано."""
    out = {"guest": await award(b["guest"], "stay", b["id"]), "host": await award(b["owner"], "host_stay", b["id"])}
    g = await db.one("select ref_by from users where id=?", (b["guest"],))
    if g and g["ref_by"] and await _ref_ok(g["ref_by"], "ref_stay"):
        out["ref"] = g["ref_by"] if await award(g["ref_by"], "ref_stay", b["id"]) else None
    return out


async def on_listing(owner: int, lid: int):
    await award(owner, "listing", lid)
    u = await db.one("select ref_by from users where id=?", (owner,))
    if u and u["ref_by"] and await _ref_ok(u["ref_by"], "ref_listing"):
        await award(u["ref_by"], "ref_listing", owner)      # один раз за запрошеного господаря


async def daily(uid: int) -> int:
    """Щоденний візит і серія. Повертає нараховані бали (0, якщо сьогодні вже було)."""
    u = await db.one("select streak, last_open from users where id=?", (uid,))
    t = dt.date.today().isoformat()
    if not u or u["last_open"] == t:
        return 0
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    streak = u["streak"] + 1 if u["last_open"] == y else 1
    await db.ex("update users set streak=?, last_open=? where id=?", (streak, t, uid))
    bonus = PTS["daily"] + (8 if streak % 7 == 0 else 0)
    await award(uid, "daily", t, bonus)
    return bonus


async def snapshot(uid: int, bot_username: str) -> dict:
    u = await db.one("select points, streak, name from users where id=?", (uid,))
    lv = level(u["points"])
    inv = await db.one("select count(*) n, sum(case when coalesce(phone,'')!='' then 1 else 0 end) a from users where ref_by=?", (uid,))
    earned = (await db.one("select coalesce(sum(pts),0) s from points_log where user=? and kind like 'ref_%'", (uid,)))["s"]
    hist = await db.q("select kind, pts, created from points_log where user=? order by id desc limit 8", (uid,))
    top = await db.q("select u.name, count(*) n from users r join users u on u.id=r.ref_by group by r.ref_by order by n desc limit 5")
    return {"points": u["points"], "streak": u["streak"], "level": lv, "member": is_member(u["points"]), "invited": inv["n"], "active": inv["a"] or 0, "earned": earned,
            "history": [{"t": LABEL.get(h["kind"], h["kind"]), "p": h["pts"], "d": h["created"][:10]} for h in hist],
            "top": [{"name": (t["name"] or "Друг").split()[0], "n": t["n"]} for t in top], "link": f"https://t.me/{bot_username}?start=r_{uid}",
            "rewards": [{"id": k, "title": v[0], "cost": v[1]} for k, v in REWARDS.items()], "steps": {"friend": PTS["ref_phone"], "stay": PTS["stay"], "review": PTS["review"], "listing": PTS["listing"]}}


async def redeem(uid: int, rid: str, lid: int, apply_boost):
    """Обмін балів на просування. Повертає (ok, повідомлення)."""
    if rid not in REWARDS:
        return False, "Нагороду не знайдено."
    title, cost = REWARDS[rid]
    u = await db.one("select points from users where id=?", (uid,))
    if not u or u["points"] < cost:
        return False, f"Потрібно {cost} балів, у вас {u['points'] if u else 0}."
    if not await db.one("select 1 from listings where id=? and owner=?", (lid, uid)):
        return False, "Оберіть своє оголошення."
    until = await apply_boost(lid, 3 if rid == "boost3" else 7)
    await award(uid, "redeem", f"{rid}:{lid}:{dt.datetime.now().isoformat()}", -cost)
    return True, f"🔥 Готово! Оголошення просувається до {until}."
