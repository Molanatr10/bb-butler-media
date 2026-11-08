import contextlib
import json
import time
import unittest
from unittest.mock import patch

from youtube_audio_public import API_URL, MAX_BYTES, PublicAudioUnavailable, public_audio_post


VIDEO_ID = 'Gg7WaROxggI'
BASE = 'https://cdn-ytb-mega.zm.io.vn'
QUERY = '?video_id=' + VIDEO_ID + '&quality=default&ext=m4a&token=example'


class Response:
    status_code = 200
    headers = {'content-type': 'text/event-stream'}
    def __init__(self, chunks, delay=0):self.chunks, self.delay = chunks, delay
    def iter_bytes(self):
        if self.delay:time.sleep(self.delay)
        yield from self.chunks


class Client:
    def __init__(self, metadata, state, event='completed', delay=0):
        self.metadata, self.state, self.event, self.delay = metadata, state, event, delay
        self.calls = []
    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == API_URL:
            yield Response([json.dumps(self.metadata).encode()])
        else:
            raw = ('event:' + self.event + '\ndata:' + json.dumps(self.state) + '\n\n').encode()
            yield Response([raw[:17], raw[17:]], self.delay)


class PublicAudioTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {'status': True, 'data': {'video_id': VIDEO_ID, 'title': 'Goriz',
                         'medias': [{'extension': 'm4a', 'quality': 'default', 'url': BASE + '/sse-progress' + QUERY}]}}
        self.state = {'status': 'success', 'download_url': BASE + '/download' + QUERY,
                      'file_info': {'video_id': VIDEO_ID, 'downloaded': True, 'ext': 'm4a',
                                    'duration': 320, 'size': 5177804, 'title': 'Goriz'}}
        self.dns = patch('youtube_audio_public.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))])
        self.dns.start();self.addCleanup(self.dns.stop)
    def invoke(self, client=None, budget=3):
        return public_audio_post('https://youtube.com/watch?v=' + VIDEO_ID,
                                 client or Client(self.metadata, self.state), 320.066, budget)

    def test_completed_audio_is_exact_id_bounded_and_not_video(self):
        client = Client(self.metadata, self.state)
        post = self.invoke(client)
        self.assertEqual(post['engine'], 'public-audio')
        self.assertEqual(post['files'], [(BASE + '/download' + QUERY, {'extension': 'm4a'})])
        self.assertEqual(post['expected_duration'], 320.066)
        self.assertEqual(post['info']['id'], VIDEO_ID)
        self.assertEqual(client.calls[0][1]['params']['url'], 'https://www.youtube.com/watch?v=' + VIDEO_ID)
        self.assertTrue(all(call[1]['follow_redirects'] is False for call in client.calls))

    def test_cold_or_stalled_audio_returns_within_hard_budget(self):
        client = Client(self.metadata, self.state, delay=.2)
        start = time.monotonic()
        with self.assertRaises(PublicAudioUnavailable):self.invoke(client, budget=.02)
        self.assertLess(time.monotonic() - start, .15)

    def test_progress_only_or_incomplete_success_never_returns_url(self):
        for event in ['progress', 'completed']:
            self.state['file_info']['downloaded'] = False
            with self.subTest(event=event), self.assertRaises(PublicAudioUnavailable):
                self.invoke(Client(self.metadata, self.state, event=event))

    def test_wrong_video_duration_size_or_format_is_rejected(self):
        for field, value in [('video_id', 'wrongwrong1'), ('duration', 331.285), ('duration', float('nan')),
                             ('size', MAX_BYTES + 1), ('size', 0), ('ext', 'mp4')]:
            original = self.state['file_info'][field]
            self.state['file_info'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PublicAudioUnavailable):self.invoke()
            self.state['file_info'][field] = original

    def test_addresses_and_response_size_are_restricted(self):
        for address in ['http://cdn-ytb-mega.zm.io.vn/download' + QUERY,
                        'https://cdn-ytb-mega.zm.io.vn.evil.test/download' + QUERY,
                        BASE + '/other-path' + QUERY, BASE + '/download' + QUERY.replace(VIDEO_ID, 'wrongwrong1')]:
            self.state['download_url'] = address
            with self.subTest(address=address), self.assertRaises(PublicAudioUnavailable):self.invoke()
        self.state['download_url'] = BASE + '/download' + QUERY
        with patch('youtube_audio_public.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(PublicAudioUnavailable):self.invoke()
        self.state['padding'] = 'x' * 65536
        with self.assertRaises(PublicAudioUnavailable):self.invoke()


if __name__ == '__main__':unittest.main()
