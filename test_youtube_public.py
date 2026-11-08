import unittest
from unittest.mock import Mock, patch

from youtube_public import API_URL, MAX_BYTES, PublicYouTubeError, omega_post


class PublicYouTubeTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://youtube.com/shorts/QyS1O40wvFI?tracking=ignored'
        self.media = 'https://rr3.googlevideo.com/videoplayback?itag=18&mime=video%2Fmp4&dur=65.344&clen=4847766'
        self.data = {'success': True, 'data': {'id': 'opaque-provider-job-id',
                     'title': 'Test video', 'size': 4847765, 'type': 'mp4', 'url': self.media}}
        self.client = Mock()
        self.client.get.return_value.status_code = 200
        self.client.get.return_value.json.side_effect = lambda: self.data
        self.dns = patch('youtube_public.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def test_exact_video_is_canonicalized_and_opaque_job_id_is_not_used(self):
        post = omega_post(self.url, self.client)
        self.client.get.assert_called_once_with(API_URL,
            params={'action': 'download', 'url': 'https://www.youtube.com/watch?v=QyS1O40wvFI'},
            timeout=25, follow_redirects=False)
        self.assertEqual(post['info']['id'], 'QyS1O40wvFI')
        self.assertEqual(post['files'], [(self.media, {'extension': 'mp4'})])
        self.assertEqual(post['info']['duration'], 65.344)
        self.assertTrue(post['original_only'])
        self.assertNotIn('heights', post)

    def test_source_and_playback_addresses_are_strictly_restricted(self):
        for url in ['https://youtube.com.evil.test/watch?v=QyS1O40wvFI',
                    'http://youtu.be/QyS1O40wvFI', 'https://youtube.com:443/watch?v=QyS1O40wvFI',
                    'https://youtube.com/playlist?list=QyS1O40wvFI', 'https://youtu.be/shortid']:
            with self.subTest(url=url), self.assertRaises(PublicYouTubeError):
                omega_post(url, self.client)
        for media in ['http://rr3.googlevideo.com/videoplayback',
                      'https://rr3.googlevideo.com.evil.test/videoplayback',
                      'https://127.0.0.1/videoplayback', 'https://user:password@rr3.googlevideo.com/videoplayback',
                      'https://rr3.googlevideo.com:443/videoplayback']:
            self.data['data']['url'] = media
            with self.subTest(media=media), self.assertRaises(PublicYouTubeError):
                omega_post(self.url, self.client)
        self.data['data']['url'] = self.media
        with patch('youtube_public.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)

    def test_real_size_and_duration_bounds_cannot_be_hidden_by_reported_metadata(self):
        for replacement in ['dur=601', 'dur=nan', 'dur=-1']:
            self.data['data']['url'] = self.media.replace('dur=65.344', replacement)
            with self.subTest(replacement=replacement), self.assertRaises(PublicYouTubeError):
                omega_post(self.url, self.client)
        self.data['data']['url'] = self.media.replace('clen=4847766', f'clen={MAX_BYTES+1}')
        with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)
        self.data['data']['url'] = self.media
        self.data['data']['size'] = MAX_BYTES + 1
        with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)

    def test_failures_and_missing_bounds_never_become_downloads(self):
        self.client.get.return_value.status_code = 429
        with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)
        self.client.get.return_value.status_code = 200
        for body in [{'success': False}, {'success': True, 'data': None},
                     {'success': True, 'data': {'title': 'Bad', 'type': 'mp3'}}]:
            self.data = body
            with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)
        self.data = {'success': True, 'data': {'title': 'No bounds', 'type': 'mp4',
                    'url': 'https://rr3.googlevideo.com/videoplayback?mime=video%2Fmp4'}}
        with self.assertRaises(PublicYouTubeError):omega_post(self.url, self.client)


if __name__ == '__main__':
    unittest.main()
