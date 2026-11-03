"""Public listing import. Never executes page scripts or bypasses site access controls.

Every TCP connection uses the exact public addresses checked by PublicResolver;
redirects are handled here so each destination is subject to the same policy.
"""
import asyncio
import ipaddress
import math
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.helpers import is_ip_address


SOURCES = frozenset({"dom.ria.com", "ria.com", "olx.ua", "monomarket.ua",
                     "monomarket.com.ua", "market.monobank.ua"})
MAX_PAGE = 1_500_000
REDIRECTS = frozenset({301, 302, 303, 307, 308})
WARNING = ("Це чернетка: перевірте фото, ціну та опис перед публікацією. "
           "Імпортуйте лише власні оголошення або матеріали, які маєте право використовувати. "
           "Площадки можуть обмежувати імпорт; тоді заповніть оголошення вручну.")


def public_ip(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and any(ip in network for network in (
        ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
        ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"),
    )):
        return False
    return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved
                and not ip.is_loopback and not ip.is_link_local
                and not ip.is_unspecified and not ip.is_private)


def checked_url(url, allowed_hosts=None):
    """Canonical HTTPS URL; reject credentials, unusual ports and local literals."""
    if not isinstance(url, str) or not 1 <= len(url) <= 2000:
        raise ValueError("Вставте повне HTTPS-посилання на оголошення.")
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Некоректне посилання.")
    try:
        p = urlsplit(url)
        host = (p.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if p.scheme.lower() != "https" or not host or p.username is not None or p.password is not None or p.port not in (None, 443):
            raise ValueError
        # Brackets are used only for IPv6 addresses below; zone IDs are forbidden.
        if "%" in host:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # aiohttp accepts shortened/octal-looking IPv4 literals and skips its
            # resolver for them. Reject every literal it recognizes that Python's
            # strict IP parser does not, rather than letting getaddrinfo expand it.
            if is_ip_address(host):
                raise ValueError
            if "." not in host or any(not part for part in host.split(".")):
                raise ValueError
        else:
            if not public_ip(host):
                raise ValueError
        if allowed_hosts is not None and not any(host == root or host.endswith("." + root) for root in allowed_hosts):
            raise ValueError("Імпорт підтримує посилання DOM.RIA, OLX і monoмаркет.")
        netloc = "[" + host + "]" if ":" in host else host
        return urlunsplit(("https", netloc, p.path or "/", p.query, ""))
    except (UnicodeError, ValueError) as e:
        if str(e).startswith("Імпорт"):
            raise
        raise ValueError("Небезпечне або некоректне посилання.") from None


class PublicResolver(AbstractResolver):
    """Resolve once and pin only globally routable addresses into aiohttp."""
    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        try:
            rows = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM)
        except (OSError, asyncio.TimeoutError):
            raise ValueError("Не вдалося знайти адресу сайту.") from None
        if not rows or any(not public_ip(row[4][0]) for row in rows):
            raise ValueError("Доступ до цієї адреси заборонено.")
        unique = {}
        for fam, _, proto, _, address in rows:
            unique[(fam, address[0])] = {"hostname": host, "host": address[0],
                                       "port": port, "family": fam,
                                       "proto": proto, "flags": socket.AI_NUMERICHOST}
        return list(unique.values())

    async def close(self):
        pass


async def safe_fetch(url, max_bytes, image=False, *, allowed_hosts=None):
    """Return (bytes, content_type, final_url), rejecting oversized bodies.

    The response limit applies to decompressed bytes as well as Content-Length.
    DNS, TLS and upstream failures are deliberately reported without raw details.
    """
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 15_000_000:
        raise ValueError("Некоректний ліміт файлу.")
    target = checked_url(url, allowed_hosts)
    seen = set()
    timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=5)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": "DomivkaImport/1.0", "Accept-Language": "uk,en;q=0.8"},
        ) as session:
            async with asyncio.timeout(20):
                for _ in range(5):
                    if target in seen:
                        raise ValueError("Сайт повернув циклічне перенаправлення.")
                    seen.add(target)
                    async with session.get(target, allow_redirects=False) as response:
                        if response.status in REDIRECTS:
                            location = response.headers.get("Location")
                            if not location:
                                raise ValueError("Некоректне перенаправлення сайту.")
                            target = checked_url(urljoin(target, location), allowed_hosts)
                            continue
                        if response.status != 200:
                            raise ValueError("Сайт обмежив доступ або оголошення недоступне. Заповніть його вручну.")
                        ct = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if image:
                            if ct not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
                                raise ValueError("Файл не є підтримуваним зображенням.")
                        elif ct not in {"text/html", "application/xhtml+xml"}:
                            raise ValueError("Посилання не містить HTML-сторінки оголошення.")
                        if response.content_length is not None and response.content_length > max_bytes:
                            raise ValueError("Сторінка або фото завеликі для імпорту.")
                        result = bytearray()
                        async for chunk in response.content.iter_chunked(32768):
                            result.extend(chunk)
                            if len(result) > max_bytes:
                                raise ValueError("Сторінка або фото завеликі для імпорту.")
                        return bytes(result), ct, target
    except ValueError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise ValueError("Не вдалося завантажити оголошення. Спробуйте пізніше або заповніть його вручну.") from None
    raise ValueError("Забагато перенаправлень сайту.")


async def _photo_url(url):
    try:
        url = checked_url(url)
        p = urlsplit(url)
        try:
            ipaddress.ip_address(p.hostname)
        except ValueError:
            await PublicResolver().resolve(p.hostname, 443)
        return url
    except (ValueError, OSError, TimeoutError):
        return None


async def draft(url, parser):
    """Parse public metadata into an editable draft; never publish automatically."""
    raw, _, final = await safe_fetch(url, MAX_PAGE, allowed_hosts=SOURCES)
    try:
        parsed = parser(raw.decode("utf-8", "replace"), final)
    except Exception:
        raise ValueError("Не вдалося прочитати дані оголошення. Заповніть його вручну.") from None
    def text(key, limit):
        value = parsed.get(key, "")
        return value.strip()[:limit] if isinstance(value, str) else ""
    out = {"title": text("title", 120), "descr": text("descr", 2000),
           "city": text("city", 60), "currency": text("currency", 8),
           "price": None, "photos": [], "source": urlsplit(final).hostname,
           "source_url": final, "warning": WARNING}
    try:
        value = float(parsed.get("price"))
        if math.isfinite(value) and 0 < value <= 100_000_000:
            out["price"] = value
    except (TypeError, ValueError, OverflowError):
        pass
    for key in ("rooms", "guests"):
        value = parsed.get(key)
        if isinstance(value, int) and 1 <= value <= 100:
            out[key] = value
    photos = parsed.get("photos", [])
    if isinstance(photos, list):
        try:
            async with asyncio.timeout(8):
                checked = await asyncio.gather(*[_photo_url(p) for p in photos[:8]])
            out["photos"] = list(dict.fromkeys(p for p in checked if p))
        except TimeoutError:
            # Text remains useful when an external image CDN has DNS trouble.
            pass
    if not out["title"] and not out["photos"]:
        raise ValueError("На сторінці немає доступних даних оголошення. Заповніть його вручну.")
    return out
