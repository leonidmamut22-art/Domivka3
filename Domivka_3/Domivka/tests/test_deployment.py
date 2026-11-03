import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import deployment

class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def test_production_needs_stable_origin_and_no_quick_tunnel(self):
        with patch.dict(os.environ,{'DEPLOYMENT_MODE':'production','WEBAPP_URL':'https://domivka.example','TUNNEL':'off','WEBAPP':'on'}):
            deployment.check_config()
            with patch.dict(os.environ,{'TUNNEL':'auto'}):
                with self.assertRaises(ValueError):deployment.check_config()
    def test_bad_origins_rejected(self):
        for url in ('http://example.com','https://user:secret@example.com','https://example.com/wrong','https://example.com/?bot=other',''):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):deployment.validate_origin(url)
    async def test_matching_identity(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'service':'domivka','bot_id':123}))) as client:
            data=await deployment.verify_public_app('https://example.com',123,client=client)
            self.assertEqual(data['bot_id'],123)
    async def test_other_bot_refused(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'service':'domivka','bot_id':999}))) as client:
            with self.assertRaises(ValueError):await deployment.verify_public_app('https://example.com',123,client=client)
    async def test_other_service_refused(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'service':'other','bot_id':123}))) as client:
            with self.assertRaises(ValueError):await deployment.verify_public_app('https://example.com',123,client=client)
