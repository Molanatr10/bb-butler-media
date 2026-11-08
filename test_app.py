import io
import os
from pathlib import Path
import tempfile
import unittest
import httpx
import json
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

    def test_audio_only_sources_offer_audio_not_fake_video_resolutions(self):
        post={'engine':'yt-dlp','info':{'title':'Recording'},'entries':[{'formats':[{'acodec':'aac','vcodec':'none','abr':128}]}]}
        result=app.metadata(post)
        self.assertFalse(result['has_video']);self.assertTrue(result['has_audio']);self.assertEqual(result['heights'],[])
        self.assertEqual(result['audio_modes'],['audio','mp3'])

    def test_spotify_preview_is_labelled_and_full_audio_is_a_public_recording_match(self):
        state={'props':{'pageProps':{'state':{'data':{'entity':{'title':'Track','artists':[{'name':'Artist'}],'duration':200000,'audioPreview':{'url':'https://p.scdn.co/preview.mp3'}}}}}}}
        response=httpx.Response(200,text='<script id="__NEXT_DATA__" type="application/json">'+json.dumps(state)+'</script>',request=httpx.Request('GET','https://open.spotify.com'))
        with patch.object(app.httpx,'get',return_value=response):post=app.spotify_post('https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl')
        result=app.metadata(post)
        self.assertEqual(result['title'],'Artist — Track');self.assertTrue(result['spotify_match']);self.assertIn('preview',result['audio_modes'])
        job=app.Request(**{**self.job,'kind':'download','mode':'preview'})
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'fetch_file') as fetch:
            result=app.download_audio(post,job,directory)
            self.assertEqual(result[0][0],'audio');self.assertEqual(fetch.call_args.args[0],'https://p.scdn.co/preview.mp3')

    def test_fallback_resolutions_are_actual_formats_and_wrong_video_is_rejected(self):
        data={'videoId':'jNQXAC9IVRw','title':'Test','lengthSeconds':19,'adaptiveFormats':[{'type':'video/mp4','qualityLabel':'240p','url':'https://yt.chocolatemoo53.com/videoplayback'},{'type':'audio/mp4','bitrate':'128000','url':'https://yt.chocolatemoo53.com/videoplayback'}]}
        response=httpx.Response(200,json=data,request=httpx.Request('GET',app.FALLBACK_BASE))
        with patch.object(app,'public_url'),patch.object(app.httpx,'get',return_value=response):
            post=app.fallback_post('https://www.youtube.com/watch?v=jNQXAC9IVRw');result=app.metadata(post)
            self.assertEqual(result['heights'],[240]);self.assertTrue(result['has_audio'])
            with self.assertRaises(app.MediaError):app.fallback_post('https://www.youtube.com/watch?v=abcdefghijk')

    def test_fallback_refreshes_expired_metadata_stream_urls(self):
        post={'video_id':'jNQXAC9IVRw'}
        url=app.fallback_stream_url(post,{'itag':'140','url':'https://old.example.org/expired-stream'})
        self.assertIn('/latest_version?',url);self.assertIn('itag=140',url);self.assertNotIn('old.example.org',url)

    def test_single_audio_uses_telegram_audio_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'audio.mp3';target.write_bytes(b'test')
            with patch.object(app,'telegram',return_value={'message_id':23}) as send:
                app.deliver([('audio',target)],app.Request(**{**self.job,'kind':'download','mode':'mp3'}))
            self.assertEqual(send.call_args.args[0],'sendAudio');self.assertIn('audio',send.call_args.args[1])

    def test_spotify_recording_match_rejects_wrong_artists_and_covers(self):
        post={'engine':'spotify','entity':{'title':'The Song','artists':[{'name':'Correct Artist'}],'duration':200000}}
        entries=[{'videoId':'badbadbad01','title':'The Song','author':'Other Artist','lengthSeconds':200},
          {'videoId':'covercover1','title':'Correct Artist — The Song cover','author':'Correct Artist','lengthSeconds':200},
          {'videoId':'goodgood001','title':'Correct Artist — The Song (Official Audio)','author':'Correct Artist','lengthSeconds':200}]
        response=httpx.Response(200,json=entries,request=httpx.Request('GET',app.FALLBACK_BASE))
        with tempfile.TemporaryDirectory() as directory,patch.object(app.httpx,'get',return_value=response),patch.object(app,'post_info',return_value={'engine':'invidious'}) as selected:
            result=app.spotify_recording(post,directory)
            self.assertEqual(selected.call_args.args[0],'https://www.youtube.com/watch?v=goodgood001')
            self.assertEqual(result['source_url'],'https://www.youtube.com/watch?v=goodgood001')

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

    def test_cloud_verification_never_sends_a_telegram_message(self):
        job=app.Request(**{**self.job,'kind':'verify'})
        app.jobs[job.id]={'status':'running'}
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'clip.mp4';file.write_bytes(b'valid test media')
            with patch.object(app,'post_info',return_value={}),patch.object(app,'metadata',return_value={'title':'Test'}),patch.object(app,'download',return_value=[('video',file)]),patch.object(app,'telegram',side_effect=AssertionError('must not send')):
                app.execute(job)
        self.assertEqual(app.jobs[job.id]['status'],'done');self.assertTrue(app.jobs[job.id]['result']['verification_only'])


if __name__=='__main__':unittest.main()
