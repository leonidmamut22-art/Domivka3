"""Security regression tests for public listing imports. No live site calls."""
import asyncio
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import listing_import as imp


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status=200, headers=None, chunks=(b"<html>listing</html>",)):
        self.status = status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.content_length = None
        self.content = Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    responses = []
    requests = []
    config = None

    def __init__(self, **kw):
        type(self).config = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        # Actual connector is replaced with a sentinel in these network tests.
        pass

    def get(self, target, **kw):
        type(self).requests.append((target, kw))
        return type(self).responses.pop(0)


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_policy_blocks_local_credentials_ports_and_lookalikes(self):
        bad = ["http://olx.ua/a", "https://user:pass@olx.ua/a", "https://olx.ua:8443/a",
               "https://127.0.0.1/a", "https://[::1]/a", "https://169.254.169.254/",
               "https://127.1/a", "https://2130706433/a", "https://0177.0.0.1/a",
               "https://[64:ff9b::7f00:1]/a", "https://[2002:7f00:1::]/a",
               "https://olx.ua.evil.example/a", "https://evilolx.ua/a", "https://olx.ua/a\n"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                imp.checked_url(url, imp.SOURCES)
        self.assertEqual(imp.checked_url("https://www.olx.ua/item#fragment", imp.SOURCES),
                         "https://www.olx.ua/item")

    async def test_resolver_rejects_mixed_public_and_private_answers(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=answers)):
            with self.assertRaises(ValueError):
                await imp.PublicResolver().resolve("olx.ua")

    async def test_resolver_pins_checked_numeric_addresses(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        loop = asyncio.get_running_loop()
        lookup = AsyncMock(return_value=answers)
        with patch.object(loop, "getaddrinfo", new=lookup):
            result = await imp.PublicResolver().resolve("olx.ua")
        self.assertEqual(result[0]["host"], "1.1.1.1")
        self.assertEqual(result[0]["hostname"], "olx.ua")
        self.assertEqual(result[0]["flags"], socket.AI_NUMERICHOST)
        lookup.assert_awaited_once()

    async def _fetch(self, responses, limit=1000, image=False):
        Session.responses = list(responses)
        Session.requests = []
        with patch.object(imp.aiohttp, "ClientSession", Session), patch.object(imp.aiohttp, "TCPConnector", return_value=object()):
            return await imp.safe_fetch("https://olx.ua/item", limit, image, allowed_hosts=imp.SOURCES)

    async def test_redirect_is_checked_before_next_connection(self):
        with self.assertRaises(ValueError):
            await self._fetch([Response(302, {"Location": "https://127.0.0.1/secret"})])
        self.assertEqual(len(Session.requests), 1)
        with self.assertRaises(ValueError):
            await self._fetch([Response(302, {"Location": "https://elsewhere.example/item"})])
        self.assertEqual(len(Session.requests), 1)

    async def test_stream_limit_rejects_instead_of_silent_truncation(self):
        with self.assertRaises(ValueError):
            await self._fetch([Response(chunks=[b"1234", b"5678"])], limit=6)

    async def test_non_html_and_non_image_rejected(self):
        with self.assertRaises(ValueError):
            await self._fetch([Response(headers={"Content-Type": "application/json"})])
        with self.assertRaises(ValueError):
            await self._fetch([Response(headers={"Content-Type": "image/svg+xml"})], image=True)

    async def test_redirect_success_preserves_tls_and_disables_proxy_cookies(self):
        data, ct, final = await self._fetch([Response(302, {"Location": "/new"}), Response()])
        self.assertEqual(final, "https://olx.ua/new")
        self.assertEqual(ct, "text/html")
        self.assertTrue(data)
        self.assertFalse(Session.config["trust_env"])
        self.assertTrue(all(kw["allow_redirects"] is False for _, kw in Session.requests))

    async def test_draft_is_editable_and_filters_bad_photo_urls(self):
        parsed = {"title": "  My table  ", "descr": "Description", "price": float("nan"),
                  "currency": "UAH", "photos": ["https://127.0.0.1/x", "https://1.1.1.1/image.jpg"]}
        with patch.object(imp, "safe_fetch", new=AsyncMock(return_value=(b"<html/>", "text/html", "https://olx.ua/item"))):
            result = await imp.draft("https://olx.ua/item", lambda *_: parsed)
        self.assertEqual(result["title"], "My table")
        self.assertIsNone(result["price"])
        self.assertEqual(result["photos"], ["https://1.1.1.1/image.jpg"])
        self.assertTrue(result["warning"])
        self.assertEqual(result["source"], "olx.ua")


if __name__ == "__main__":
    unittest.main()
