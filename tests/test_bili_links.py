"""Short-link regressions: no network, accounts, model requests, or QQ messages."""
import importlib.util
import json
import logging
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
BV = 'BV1YtxK6BEca'
SHORT = 'https://b23.tv/example'
VIDEO = 'https://www.bilibili.com/video/' + BV


class Response:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {'Location': location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeHTTP(types.ModuleType):
    def __init__(self):
        super().__init__('aiohttp')
        self.routes = {}
        self.calls = []

    def ClientTimeout(self, **kwargs):
        return kwargs

    def ClientSession(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError('Unexpected request; video HTML must not be fetched')
        return Response(*self.routes[url])


class Links(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = FakeHTTP()
        api = types.ModuleType('astrbot.api')
        api.logger = logging.getLogger('bili-links-test')
        spec = importlib.util.spec_from_file_location('_bili_runtime_test', ROOT / 'runtime.py')
        self.runtime = importlib.util.module_from_spec(spec)
        # The package check runs without an AstrBot installation. Only the
        # imported logger and HTTP transport are replaced; resolver code is real.
        with patch.dict(sys.modules, {
            'aiohttp': self.http, 'astrbot': types.ModuleType('astrbot'),
            'astrbot.api': api,
        }):
            spec.loader.exec_module(self.runtime)

    async def test_short_link_does_not_fetch_video_html_that_returns_412(self):
        self.http.routes = {SHORT: (302, VIDEO + '?share_source=qq'), VIDEO + '?share_source=qq': (412,)}
        self.assertEqual(await self.runtime.resolve_bili(SHORT), BV)
        self.assertEqual(self.http.calls, [SHORT])

    async def test_multiple_short_redirects_and_mobile_video(self):
        middle = 'https://b23.tv/next'
        self.http.routes = {SHORT: (302, middle), middle: (307, 'https://m.bilibili.com/video/' + BV + '/?from=qq')}
        self.assertEqual(await self.runtime.resolve_bili(SHORT), BV)
        self.assertEqual(self.http.calls, [SHORT, middle])

    async def test_video_looking_external_redirect_is_rejected(self):
        self.http.routes = {SHORT: (302, 'https://bilibili.com.evil.test/video/' + BV)}
        with self.assertRaisesRegex(ValueError, 'untrusted_destination'):
            await self.runtime.resolve_bili(SHORT)
        self.assertEqual(self.http.calls, [SHORT])

    async def test_short_server_error_still_fails(self):
        self.http.routes = {SHORT: (403,)}
        with self.assertRaisesRegex(ValueError, 'link_unavailable'):
            await self.runtime.resolve_bili(SHORT)

    async def test_bvid_and_direct_video_need_no_html(self):
        self.assertEqual(await self.runtime.resolve_bili(BV), BV)
        self.assertEqual(await self.runtime.resolve_bili(VIDEO + '/?p=1'), BV)
        self.assertEqual(self.http.calls, [])

    async def test_av_short_link_still_uses_video_identifier_conversion(self):
        self.http.routes = {SHORT: (302, 'https://www.bilibili.com/video/av123456/')}
        sdk = types.ModuleType('bilibili_api')
        sdk.video = types.SimpleNamespace(Video=lambda aid: types.SimpleNamespace(get_bvid=lambda: BV))
        with patch.dict(sys.modules, {'bilibili_api': sdk}):
            self.assertEqual(await self.runtime.resolve_bili(SHORT), BV)
        self.assertEqual(self.http.calls, [SHORT])

    async def test_redirect_limit_is_still_bounded(self):
        self.http.routes = {'https://b23.tv/' + str(i): (302, '/' + str(i + 1)) for i in range(6)}
        with self.assertRaisesRegex(ValueError, 'too_many_redirects'):
            await self.runtime.resolve_bili('https://b23.tv/0')
        self.assertEqual(len(self.http.calls), 5)

    async def test_generic_expand_keeps_loading_terminal_nonvideo_pages(self):
        target = 'https://www.bilibili.com/read/cv123'
        self.http.routes = {SHORT: (302, target), target: (200,)}
        self.assertEqual(await self.runtime.expand(SHORT, ('bilibili.com', 'b23.tv')), target)
        self.assertEqual(self.http.calls, [SHORT, target])

    def test_qq_json_card_extracts_only_the_share_destination(self):
        card = {'app': 'com.tencent.structmsg', 'meta': {'news': {
            'title': '测试', 'jumpUrl': SHORT, 'preview': 'https://i0.hdslb.com/test.jpg',
        }}}
        refs = self.runtime.link_refs([{'type': 'json', 'data': {'data': json.dumps(card)}}], 'bili')
        self.assertEqual(refs, [{'id': SHORT, 'url': SHORT}])


if __name__ == '__main__':
    unittest.main()
