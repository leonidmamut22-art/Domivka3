"""Additive migration and rental API property-type regression tests."""
import sys
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import db
import webapp


class PropertyTypesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = str(Path(__file__).resolve().parent / (".property-" + uuid.uuid4().hex + ".sqlite"))
        self.old_b = webapp.B
        await db.init(self.path)
        await db.ex("insert into users(id,name,role,phone) values(1,'Owner','host','380991234567')")
        self.user = await db.one("select * from users where id=1")
        self.mod = SimpleNamespace(
            VIS="active=1 and blocked=0", is_boosted=lambda _: False,
            eff=lambda row, month: row["price_night"],
            create_listing=self.create_listing,
        )
        self.client = TestClient(TestServer(webapp.make_app(self.mod)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await db.DB.close()
        webapp.B = self.old_b
        for suffix in ("", "-wal", "-shm"):
            Path(self.path + suffix).unlink(missing_ok=True)

    async def create_listing(self, user, data):
        lid = await db.ex(
            "insert into listings(owner,title,city,district,price_night) values(?,?,?,?,?)",
            (user["id"], data["title"], data["city"], data["district"], data["price_night"]),
        )
        return lid, 0

    async def test_additive_default_survives_reopening(self):
        lid, _ = await self.create_listing(self.user, {"title": "Old apartment", "city": "Lutsk", "district": "Center", "price_night": 100})
        self.assertEqual((await db.one("select property_type from listings where id=?", (lid,)))["property_type"], "apartment")
        await db.DB.close()
        await db.init(self.path)
        self.assertEqual((await db.one("select title,property_type from listings where id=?", (lid,)))["title"], "Old apartment")

    async def test_filter_returns_only_selected_type_and_rejects_unknown(self):
        for kind in ("apartment", "house", "commercial", "land"):
            lid, _ = await self.create_listing(self.user, {"title": kind, "city": "Lutsk", "district": "Center", "price_night": 100})
            await db.ex("update listings set property_type=? where id=?", (kind, lid))
        with patch.object(webapp, "auth", new=AsyncMock(return_value=(None, None))):
            response = await self.client.get("/api/listings?property_type=commercial")
            self.assertEqual(response.status, 200)
            items = await response.json()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["property_type"], "commercial")
            response = await self.client.get("/api/listings?property_type=arbitrary")
            self.assertEqual(response.status, 400)

    async def test_create_and_edit_persist_validated_type(self):
        form = {"title": "Commercial office", "city": "Lutsk", "district": "Center", "price_night": "100", "property_type": "commercial"}
        with patch.object(webapp, "need", new=AsyncMock(return_value=self.user)):
            response = await self.client.post("/api/host/new", data=form)
            self.assertEqual(response.status, 200)
            lid = (await response.json())["id"]
            self.assertEqual((await db.one("select property_type from listings where id=?", (lid,)))["property_type"], "commercial")
            response = await self.client.post(f"/api/host/listing/{lid}", json={"property_type": "garage"})
            self.assertEqual(response.status, 200)
            self.assertEqual((await db.one("select property_type from listings where id=?", (lid,)))["property_type"], "garage")
            response = await self.client.post(f"/api/host/listing/{lid}", json={"property_type": "bad"})
            self.assertEqual(response.status, 400)
            response = await self.client.post("/api/host/new", data={**form, "property_type": "bad"})
            self.assertEqual(response.status, 400)
        self.assertEqual((await db.one("select count(*) n from listings"))["n"], 1)


if __name__ == "__main__":
    unittest.main()
