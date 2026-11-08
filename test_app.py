import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['MEDIA_BACKEND_SECRET']='test-secret-that-is-longer-than-32-characters'
os.environ['ALLOWED_CHAT_ID']='-123'
import app
from fastapi.testclient import TestClient
from PIL import Image


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(app.app)
        app.jobs.clear()
        self.headers={'Authorization':'Bearer '+os.environ['MEDIA_BACKEND_SECRET']}
        self.job={'id':'a'*32,'kind':'inspect','chat_id':-123,'message_id':12,'url':'https://www.youtube.com/watch?v=abcdefghijk'}

    def test_auth_and_group_isolation(self):
        self.assertEqual(self.client.post('/jobs',json=self.job).status_code,401)
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'chat_id':-999}).status_code,403)
        for url in ['http://youtube.com/watch?v=abcdef','https://localhost/','https://youtube.com.evil.test/watch?v=abcdef','https://x.com/user','https://youtube.com/playlist?list=abc','https://x.com:443/a/status/123']:
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'url':url}).status_code,422)

    def test_submission_is_idempotent_and_rejects_changed_payload(self):
        with patch.object(app.pool,'submit') as submit:
            for _ in range(2):
                self.assertEqual(self.client.post('/jobs',headers=self.headers,json=self.job).json(),{'status':'running'})
            self.assertEqual(submit.call_count,1)
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'message_id':13}).status_code,409)
        app.jobs['a'*32].update(status='done',result={'heights':[360]})
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json=self.job).json()['result'],{'heights':[360]})

    def test_private_jobs_require_matching_telegram_user(self):
        with patch.object(app.pool,'submit'):
            private={**self.job,'chat_id':123,'user_id':123}
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json=private).status_code,200)
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**private,'id':'c'*32,'user_id':999}).status_code,403)

    def test_sticker_dimensions_alpha_and_size(self):
        for size in [(1800,1000),(100,300),(250,250)]:
            with tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'source.png';out=Path(directory)/'out.webp'
                Image.new('RGBA',size,(255,0,0,100)).save(source)
                app.convert_sticker(source,out)
                with Image.open(out) as result:
                    self.assertEqual(max(result.size),512)
                    self.assertEqual(result.mode,'RGBA')
                self.assertLessEqual(out.stat().st_size,512*1024)

    def test_albums_never_leave_a_single_final_image(self):
        for count in [2,10,11,12,20,21]:
            groups=app.album_batches(range(count))
            self.assertEqual(sum(groups,[]),list(range(count)))
            self.assertTrue(all(2<=len(g)<=10 for g in groups))

    def test_album_delivery_has_caption_only_once_and_no_advertising(self):
        with tempfile.TemporaryDirectory() as directory:
            images=[]
            for n in range(11):
                p=Path(directory)/f'{n}.jpg';p.write_bytes(b'test');images.append(('photo',p))
            job=app.Request(**{**self.job,'kind':'download','caption':'سلام خوبی؟'})
            def fake(method,body,files=None):
                self.assertEqual(method,'sendMediaGroup')
                return [{'message_id':i} for i in range(len(body['media']))]
            with patch.object(app,'telegram',side_effect=fake) as telegram:
                app.deliver(images,job)
            self.assertEqual([len(c.args[1]['media']) for c in telegram.call_args_list],[9,2])
            self.assertEqual(telegram.call_args_list[0].args[1]['media'][0]['caption'],'سلام خوبی؟')
            self.assertNotIn('caption',telegram.call_args_list[1].args[1]['media'][0])

    def test_gallery_fallback_uses_correct_cli_and_detects_photos(self):
        raw=[[2,{}],[3,'https://cdn.example.org/a.jpg',{'extension':'jpg','description':'این یک پیام فارسی است'}]]
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'run',side_effect=[app.MediaError('not a video'),raw]) as run:
            post=app.post_info('https://www.instagram.com/p/AbCd123/',directory)
            self.assertIn('--config-ignore',run.call_args.args[0])
            result=app.metadata(post)
            self.assertFalse(result['has_video'])
            self.assertEqual(result['caption'],'این یک پیام فارسی است')
            self.assertEqual(result['language'],'fa')

    def test_video_resolutions_are_bounded_to_actual_formats(self):
        post={'engine':'yt-dlp','info':{'title':'An English example'},'entries':[{'formats':[{'height':360,'vcodec':'avc1'},{'height':720,'vcodec':'avc1'},{'height':2160,'vcodec':'avc1'},{'height':720,'vcodec':'none'}]}]}
        self.assertEqual(app.metadata(post)['heights'],[360,720])

    def test_private_cdn_addresses_are_blocked(self):
        with patch.object(app.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(app.MediaError):app.public_url('https://cdn.example.org/video.mp4')

    def test_source_login_failure_reports_cause_without_exposing_raw_stderr(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            command=[sys.executable,'-c',"import sys;sys.stderr.write('Sign in to confirm you are not a bot; private source detail');sys.exit(1)"]
            with self.assertRaisesRegex(app.MediaError,'YouTube is blocking') as error:app.run(command,directory)
            self.assertNotIn('private source detail',str(error.exception))

    def test_video_duration_rejected_without_silent_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'a.mp4';source.write_bytes(b'test')
            with patch.object(app,'run',return_value={'format':{'duration':'601'}}) as run:
                with self.assertRaisesRegex(app.MediaError,'10 minutes'):app.normalize_video(source,720,directory,0)
                self.assertEqual(run.call_count,1)


if __name__=='__main__':unittest.main()
