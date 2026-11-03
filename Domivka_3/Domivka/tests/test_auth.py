import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import webapp


class TelegramAuthTests(unittest.TestCase):
    token = '123456:test-token-for-unit-tests'

    def signed(self, *, age=0, user=None):
        data = {'auth_date': str(int(time.time()) - age),
                'user': json.dumps(user if user is not None else {'id': 6000000001, 'first_name': 'Test'})}
        secret = hmac.new(b'WebAppData', self.token.encode(), hashlib.sha256).digest()
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(data)

    def test_valid_large_telegram_id(self):
        self.assertEqual(webapp.tg_user(self.signed(), self.token)['id'], 6000000001)

    def test_tampered_or_wrong_token(self):
        self.assertIsNone(webapp.tg_user(self.signed(), 'wrong-token'))
        self.assertIsNone(webapp.tg_user(self.signed().replace('auth_date=', 'auth_date=1'), self.token))

    def test_expired_and_future(self):
        for age in (86402, -300):
            with self.subTest(age=age):
                self.assertIsNone(webapp.tg_user(self.signed(age=age), self.token))

    def test_duplicate_fields_and_bad_user(self):
        self.assertIsNone(webapp.tg_user(self.signed() + '&user=%7B%7D', self.token))
        for user in ([], {'id': '123'}, {'id': True}, {'id': -1}):
            with self.subTest(user=user):
                self.assertIsNone(webapp.tg_user(self.signed(user=user), self.token))


if __name__ == '__main__':
    unittest.main()
