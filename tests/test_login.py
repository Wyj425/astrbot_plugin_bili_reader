"""Offline authentication regressions; no platform network requests are made."""
import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        package_name = "_login_test_" + ROOT.name
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        http = types.ModuleType("aiohttp")
        http.ClientSession = Mock(side_effect=AssertionError("Unexpected network request"))
        http.ClientTimeout = lambda **kwargs: types.SimpleNamespace(**kwargs)
        self.modules = patch.dict(sys.modules, {package_name: package, "aiohttp": http})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location(package_name + ".login", ROOT / "login.py")
        self.login = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.login
        spec.loader.exec_module(self.login)
        self.package_name = package_name
        self.service = types.SimpleNamespace(
            root=pathlib.Path(self.directory.name),
            cache=types.SimpleNamespace(values={"old": 1}, failures={"old": 1}),
            backend=types.SimpleNamespace(
                reader=types.SimpleNamespace(cache={"old": 1}, failed={"old": 1})),
            login_state={"state": "idle"}, login_task=None)

    class Response:
        def __init__(self, payload, cookies=None):
            self.payload = payload
            self.cookies = {key: types.SimpleNamespace(value=value)
                            for key, value in (cookies or {}).items()}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self):
            return self.payload

    def session(self, response):
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()
        session.get.return_value = response
        return session

    async def test_missing_credentials_is_not_logged_in(self):
        self.assertEqual(await self.login.verify(self.service), {"logged_in": False})

    async def test_qr_generation_starts_bilibili_poll(self):
        response = self.Response({"code": 0, "data": {
            "url": "https://example.com/test-login", "qrcode_key": "test-key"}})
        with patch.object(self.login.aiohttp, "ClientSession", return_value=self.session(response)), \
             patch.object(self.login, "qr_image", return_value="data:image/png;base64,AAAA"), \
             patch.object(self.login, "poll_bili", new_callable=AsyncMock) as poll:
            await self.login.begin(self.service)
            self.assertEqual(self.service.login_state["state"], "scan")
            await self.service.login_task
            poll.assert_awaited_once_with(self.service, "test-key")

    async def test_confirmed_login_persists_credentials_and_clears_cache(self):
        response = self.Response({"code": 0, "data": {"code": 0, "url": ""}},
                                 {"SESSDATA": "test-session", "bili_jct": "test-csrf",
                                  "DedeUserID": "123"})
        with patch.object(self.login.aiohttp, "ClientSession", return_value=self.session(response)), \
             patch.object(self.login.asyncio, "sleep", new_callable=AsyncMock):
            await self.login.poll_bili(self.service, "test-key")
        saved = json.loads((self.service.root / "credentials.json").read_text())
        self.assertEqual(saved["sessdata"], "test-session")
        self.assertEqual(saved["bili_jct"], "test-csrf")
        self.assertEqual(self.service.login_state["state"], "done")
        self.assertEqual(self.service.cache.values, {})
        self.assertEqual(self.service.cache.failures, {})

    async def test_expired_qr_does_not_write_credentials(self):
        response = self.Response({"code": 0, "data": {"code": 86038}})
        with patch.object(self.login.aiohttp, "ClientSession", return_value=self.session(response)), \
             patch.object(self.login.asyncio, "sleep", new_callable=AsyncMock):
            await self.login.poll_bili(self.service, "test-key")
        self.assertEqual(self.service.login_state["state"], "expired")
        self.assertFalse((self.service.root / "credentials.json").exists())
