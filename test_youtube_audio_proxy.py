import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from youtube_audio_proxy import API_URL, ProxyAudioUnavailable, download_proxy_audio


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


class ProxyAudioTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://youtube.com/shorts/Gg7WaROxggI?tracking=discarded'
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / 'audio.mp3'

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_success_uses_only_canonical_public_id_and_no_credentials(self):
        def handler(request):
            self.assertEqual(str(request.url), API_URL + '?wait=true&download=true')
            self.assertEqual(request.method, 'POST')
            self.assertNotIn('authorization', request.headers)
            self.assertEqual(json.loads(request.content), {'url': 'https://www.youtube.com/watch?v=Gg7WaROxggI',
                'format': 'audio', 'quality': 'best', 'outputFormat': 'mp3'})
            return httpx.Response(200, headers={'Content-Type': 'audio/mpeg', 'Content-Length': '8'}, stream=Chunks([b'ID3-', b'data']))
        result = download_proxy_audio(self.url, self.target, self.client(handler))
        self.assertEqual(result, self.target)
        self.assertEqual(result.read_bytes(), b'ID3-data')
        self.assertFalse(self.target.with_name('audio.mp3.part').exists())

    def test_octet_stream_is_accepted_only_when_nonempty(self):
        client = self.client(lambda request: httpx.Response(200, headers={'Content-Type': 'application/octet-stream'}, stream=Chunks([b'audio'])))
        self.assertEqual(download_proxy_audio(self.url, self.target, client).read_bytes(), b'audio')
        empty = self.client(lambda request: httpx.Response(200, headers={'Content-Type': 'application/octet-stream'}, stream=Chunks([])))
        with self.assertRaises(ProxyAudioUnavailable):download_proxy_audio(self.url, self.target, empty)
        self.assertEqual(self.target.read_bytes(), b'audio')

    def test_invalid_sources_make_no_requests(self):
        for url in ['http://youtu.be/Gg7WaROxggI', 'https://youtube.com.evil.test/watch?v=Gg7WaROxggI',
                    'https://youtu.be/short', 'https://youtube.com:443/watch?v=Gg7WaROxggI']:
            with self.subTest(url=url), self.assertRaises(ProxyAudioUnavailable):
                download_proxy_audio(url, self.target, self.client(lambda request: self.fail('Request sent')))

    def test_wrong_headers_errors_and_redirects_never_write_files(self):
        for status, headers in [(200, {'Content-Type': 'text/html'}), (200, {'Content-Type': 'application/json'}),
                                (302, {'Location': 'https://evil.test/file', 'Content-Type': 'audio/mpeg'}),
                                (302, {'Location': '/file', 'Content-Type': 'audio/mpeg'}),
                                (429, {'Content-Type': 'audio/mpeg'}),
                                (200, {'Content-Type': 'audio/mpeg', 'Content-Encoding': 'gzip'})]:
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(status, headers=headers, stream=Chunks([b'file']))
            with self.subTest(status=status, headers=headers), self.assertRaises(ProxyAudioUnavailable):
                download_proxy_audio(self.url, self.target, self.client(handler))
            self.assertEqual(len(seen), 1)
            self.assertFalse(self.target.exists())

    def test_size_limits_and_partial_downloads_are_rejected(self):
        for headers, chunks in [({'Content-Length': '9'}, [b'1234']), ({'Content-Length': '0'}, []),
                                ({'Content-Length': '11'}, [b'file']), ({}, [b'123456', b'78901'])]:
            client = self.client(lambda request: httpx.Response(200, headers={'Content-Type': 'audio/mpeg', **headers}, stream=Chunks(chunks)))
            with self.subTest(headers=headers), patch('youtube_audio_proxy.MAX_BYTES', 10), self.assertRaises(ProxyAudioUnavailable):
                download_proxy_audio(self.url, self.target, client)
            self.assertFalse(self.target.exists())
            self.assertFalse(self.target.with_name('audio.mp3.part').exists())

    def test_transport_timeouts_are_sanitized(self):
        def handler(request):raise httpx.ReadTimeout('sensitive provider URL', request=request)
        with self.assertRaises(ProxyAudioUnavailable) as caught:
            download_proxy_audio(self.url, self.target, self.client(handler))
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_total_deadline_rejects_trickled_transfer(self):
        client = self.client(lambda request: httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=Chunks([b'1', b'2'])))
        with patch('youtube_audio_proxy.time.monotonic', side_effect=[0, 1, 59, 61]), self.assertRaisesRegex(ProxyAudioUnavailable, 'too long'):
            download_proxy_audio(self.url, self.target, client)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_name('audio.mp3.part').exists())

    def test_watchdog_can_interrupt_connection_before_response_headers(self):
        connection = Mock()
        network = Mock()
        network.get_extra_info.return_value = connection
        callback = []
        class Timer:
            def __init__(self, seconds, stop):callback.append(stop)
            def start(self):pass
            def cancel(self):pass
        def handler(request):
            request.extensions['trace']('connection.start_tls.complete', {'return_value': network})
            callback[0]()
            return httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=Chunks([b'audio']))
        with patch('youtube_audio_proxy.threading.Timer', Timer), self.assertRaises(ProxyAudioUnavailable):
            download_proxy_audio(self.url, self.target, self.client(handler))
        connection.shutdown.assert_called_once()
        self.assertFalse(self.target.exists())


if __name__ == '__main__':unittest.main()
