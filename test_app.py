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
os.environ['MEDIA_CALLBACK_URL']=''
import app
from fastapi.testclient import TestClient
from PIL import Image


class MediaTests(unittest.TestCase):
    def test_fast_inspection_returns_receipt_without_waiting_for_callback(self):
        import threading
        release=threading.Event();started=threading.Event();finished=threading.Event()
        def callback(job):
            started.set();release.wait(2);finished.set()
        try:
            with patch.object(app,'post_info',return_value={}),patch.object(app,'metadata',return_value={'media_type':'text','caption':'Tweet'}),patch.object(app,'notify_completion',side_effect=callback):
                response=self.client.post('/jobs',headers=self.headers,json={**self.job,'url':'https://x.com/test/status/123'})
                self.assertEqual(response.json()['status'],'done')
                self.assertTrue(started.wait(1));self.assertFalse(finished.is_set())
                release.set();self.assertTrue(finished.wait(1))
        finally:release.set()
    def test_native_youtube_metadata_and_download_use_bounded_subprocess(self):
        native={'info':{'id':'abcdefghijk','title':'Short','duration':65},'heights':[144,360,720],'has_audio':True}
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'omega_post',side_effect=app.PublicYouTubeError('unavailable')),patch.object(app,'run',return_value=native) as command:
            post=app._extract_post_info(self.job['url'],directory)
            self.assertEqual(post['engine'],'youtube-native')
            self.assertEqual(app.metadata(post)['heights'],[144,360,720])
            self.assertEqual(command.call_args.kwargs['timeout'],35)
            self.assertIn('youtube_native.py',command.call_args.args[0][1])
            app.youtube_native(self.job['url'],directory,'video',360)
            self.assertEqual(command.call_args.args[0][-3:],['abcdefghijk','video','360'])
            self.assertEqual(command.call_args.kwargs['timeout'],150)

    def test_native_youtube_stream_selection_respects_resolution_and_codec(self):
        from types import SimpleNamespace
        from youtube_native import choose_video,choose_audio
        def stream(h,codec='avc1',audio=False):
            return SimpleNamespace(resolution=f'{h}p' if h else None,includes_video_track=bool(h),includes_audio_track=audio,video_codec=codec,subtype='mp4',bitrate=128000,is_default_audio_track=audio)
        low=stream(360);expensive=stream(360,'av01');large=stream(1080);audio=stream(0,audio=True)
        self.assertIs(choose_video([large,expensive,low],360),low)
        self.assertIs(choose_audio([low,audio]),audio)
        with self.assertRaises(ValueError):choose_video([large],360)

    def test_native_youtube_merges_audio_and_normalizes_output(self):
        post={'engine':'youtube-native','info':{'duration':65}}
        job=app.Request(**{**self.job,'kind':'download','height':360})
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'youtube_native',return_value={'files':['native-0.mp4','native-1.m4a']}),patch.object(app,'run',return_value={'format':{'duration':'65.1'}}) as command,patch.object(app,'normalize_video',return_value=Path(directory)/'output.mp4'):
            self.assertEqual(app.download(post,job,directory)[0][0],'video')
            self.assertTrue(any('copy' in call.args[0] for call in command.call_args_list))

    def test_native_youtube_rejects_truncated_valid_video_and_audio(self):
        post={'engine':'youtube-native','info':{'duration':60}}
        for mode in ('video','mp3'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory,patch.object(app,'youtube_native',return_value={'files':['native-0.mp4']}),patch.object(app,'normalize_video',return_value=Path(directory)/'output.mp4'),patch.object(app,'normalize_audio',return_value=Path(directory)/'output.mp3'),patch.object(app,'run',return_value={'format':{'duration':'30'}}):
                job=app.Request(**{**self.job,'kind':'download','mode':mode})
                with self.assertRaisesRegex(app.MediaError,'incomplete stream'):app.download(post,job,directory)

    def test_native_youtube_duration_checks_individual_streams_and_tolerates_container_padding(self):
        post={'engine':'youtube-native','info':{'duration':60}}
        with patch.object(app,'run',return_value={'format':{'duration':'60.2'},'streams':[{'codec_type':'video','duration':'60.1'},{'codec_type':'audio','duration':'30'}]}):
            with self.assertRaisesRegex(app.MediaError,'incomplete stream'):app.validate_native_duration([Path('result.mp4')],post,{},'unused')
        with patch.object(app,'run',return_value={'format':{'duration':'60.2'},'streams':[{'codec_type':'audio','duration':'60.1'}]}):
            app.validate_native_duration([Path('result.mp3')],post,{},'unused')

    def test_social_delivery_does_not_wait_for_youtube_download_pool(self):
        job={**self.job,'kind':'download','url':'https://x.com/AdiiiFCB/status/2119524707880030446'}
        with patch.object(app.delivery_pool,'submit') as fast,patch.object(app.pool,'submit') as slow:
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json=job).status_code,200)
            self.assertEqual(fast.call_count,1);slow.assert_not_called()
            audio={**job,'id':'d'*32,'mode':'mp3'}
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json=audio).status_code,200)
            self.assertEqual(slow.call_count,1);self.assertEqual(fast.call_count,1)

    def test_native_spotify_fallback_uses_resolved_youtube_url_and_checks_duration(self):
        url='https://www.youtube.com/watch?v=abcdefghijk'
        post={'engine':'spotify','entity':{}}
        resolved={'engine':'youtube-native','source_url':url,'expected_duration':200}
        job=app.Request(**{**self.job,'kind':'download','mode':'mp3','url':'https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl'})
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'spotify_public_recording',side_effect=app.MediaError('missing')),patch.object(app,'spotify_recording',return_value=resolved),patch.object(app,'youtube_native',return_value={'files':['native-0.m4a']}) as native,patch.object(app,'run',return_value={'format':{'duration':'30'}}),patch.object(app,'normalize_audio') as normalize:
            with self.assertRaises(app.MediaError):app.download_audio(post,job,directory)
            self.assertEqual(native.call_args.args[0],url);normalize.assert_not_called()

    def setUp(self):
        self.client=TestClient(app.app)
        app.jobs.clear();app.post_cache.clear();app.file_cache.clear()
        self.headers={'Authorization':'Bearer '+os.environ['MEDIA_BACKEND_SECRET']}
        self.job={'id':'a'*32,'kind':'inspect','chat_id':-123,'message_id':12,'url':'https://www.youtube.com/watch?v=abcdefghijk'}

    def test_auth_and_group_isolation(self):
        self.assertEqual(self.client.post('/jobs',json=self.job).status_code,401)
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'chat_id':-999}).status_code,403)
        for url in ['http://youtube.com/watch?v=abcdef','https://localhost/','https://youtube.com.evil.test/watch?v=abcdef','https://x.com/user','https://youtube.com/playlist?list=abc','https://x.com:443/a/status/123']:
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'url':url}).status_code,422)

    def test_bellona_backend_access_is_explicit_and_still_authenticated(self):
        job={**self.job,'chat_id':-1002568879931}
        self.assertEqual(self.client.post('/jobs',json=job).status_code,401)
        with patch.object(app.youtube_pool,'submit'):
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json=job).status_code,200)

    def test_submission_is_idempotent_and_rejects_changed_payload(self):
        with patch.object(app.youtube_pool,'submit') as submit:
            for _ in range(2):
                self.assertEqual(self.client.post('/jobs',headers=self.headers,json=self.job).json(),{'status':'running'})
            self.assertEqual(submit.call_count,1)
            self.assertEqual(self.client.post('/jobs',headers=self.headers,json={**self.job,'message_id':13}).status_code,409)
        app.jobs['a'*32].update(status='done',result={'heights':[360]})
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json=self.job).json()['result'],{'heights':[360]})

    def test_private_jobs_require_matching_telegram_user(self):
        with patch.object(app.youtube_pool,'submit'):
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
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'instagram_embed_post',side_effect=app.MediaError('unavailable')),patch.object(app,'run',side_effect=[app.MediaError('not a video'),raw]) as run:
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
        with patch.object(app.source_http,'get',return_value=response):post=app.spotify_post('https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl')
        result=app.metadata(post)
        self.assertEqual(result['title'],'Artist — Track');self.assertTrue(result['spotify_match']);self.assertIn('preview',result['audio_modes'])
        job=app.Request(**{**self.job,'kind':'download','mode':'preview'})
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'fetch_file') as fetch:
            result=app.download_audio(post,job,directory)
            self.assertEqual(result[0][0],'audio');self.assertEqual(fetch.call_args.args[0],'https://p.scdn.co/preview.mp3')

    def test_fallback_resolutions_are_actual_formats_and_wrong_video_is_rejected(self):
        data={'videoId':'jNQXAC9IVRw','title':'Test','lengthSeconds':19,'adaptiveFormats':[{'type':'video/mp4','qualityLabel':'240p','url':'https://yt.chocolatemoo53.com/videoplayback'},{'type':'audio/mp4','bitrate':'128000','url':'https://yt.chocolatemoo53.com/videoplayback'}]}
        response=httpx.Response(200,json=data,request=httpx.Request('GET',app.FALLBACK_BASE))
        with patch.object(app,'public_url'),patch.object(app.source_http,'get',return_value=response):
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
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'run',return_value={'entries':entries}),patch.object(app,'post_info',return_value={'engine':'invidious','info':{'title':'Correct Artist — The Song','duration':200}}) as selected:
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



    def test_metadata_cache_single_flight_and_expiry(self):
        import concurrent.futures,time
        url='https://x.com/user/status/123'
        def extract(*args):time.sleep(.03);return {'engine':'test'}
        with patch.object(app,'_extract_post_info',side_effect=extract) as get:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
                results=list(workers.map(lambda _:app.post_info(url,'unused'),range(3)))
            self.assertEqual(get.call_count,1)
            self.assertEqual(results,[{'engine':'test'}]*3)
            app.post_cache[url]=(0,{'engine':'stale'})
            self.assertEqual(app.post_info(url,'unused'),{'engine':'test'})
            self.assertEqual(get.call_count,2)

    def test_inspection_is_not_blocked_by_download_worker(self):
        import threading
        release=threading.Event();inspected=threading.Event()
        blocked=[app.pool.submit(release.wait) for _ in range(2)]
        try:
            with patch.object(app,'execute',side_effect=lambda job:inspected.set()):
                self.assertEqual(self.client.post('/jobs',headers=self.headers,json=self.job).status_code,200)
                self.assertTrue(inspected.wait(1),'inspection waited behind download')
        finally:
            release.set()
            for task in blocked:task.result(timeout=2)

    def test_instagram_captioned_embed_and_original_video(self):
        context={'context':{'copyright_blocked':False},'gql_data':{'shortcode_media':{'shortcode':'AbCd123','video_url':'https://cdn.example.org/movie.mp4','edge_media_to_caption':{'edges':[{'node':{'text':'Original caption'}}]}}}}
        html='"contextJSON":'+json.dumps(json.dumps(context))
        response=httpx.Response(200,text=html,request=httpx.Request('GET','https://www.instagram.com/'))
        with patch.object(app.source_http,'get',return_value=response),patch.object(app,'public_url',side_effect=lambda u:u):
            post=app.instagram_embed_post('https://www.instagram.com/reel/AbCd123/')
        self.assertEqual(post['files'][0][0],'https://cdn.example.org/movie.mp4')
        self.assertEqual(app.metadata(post)['caption'],'Original caption')
        self.assertTrue(app.metadata(post)['original_only'])

    def test_instagram_simple_single_photo_avoids_slow_extractors(self):
        flags={'isRichEmbed':False,'isSidecar':False,'isGuideEmbed':False,'isProfileEmbed':False,'contextJSON':None}
        body='<img src="https://cdn.example.org/avatar.jpg"><a class="EmbeddedMedia" href="https://www.instagram.com/p/AbCd123/?ig=embed"><img class="EmbeddedMediaImage" src="https://cdn.example.org/photo.jpg?x=1&amp;y=2"></a><div class="Caption"><a class="CaptionUsername">author</a>Actual &amp; caption<br>second line</div>'
        def response(config,content=body):
            return httpx.Response(200,text='"PolarisEmbedSimple","init",[],['+json.dumps(config,separators=(',',':'))+']'+content,request=httpx.Request('GET','https://www.instagram.com/'))
        with patch.object(app.source_http,'get',return_value=response(flags)),patch.object(app,'public_url',side_effect=lambda u:u),patch.object(app,'run',side_effect=AssertionError('No subprocess should run')):
            with tempfile.TemporaryDirectory() as d:post=app._extract_post_info('https://www.instagram.com/p/AbCd123/',d)
            self.assertEqual(post['files'],[('https://cdn.example.org/photo.jpg?x=1&y=2',{'extension':'jpg'})])
            self.assertEqual(app.metadata(post)['caption'],'Actual & caption\nsecond line')
            self.assertEqual(app.metadata(post)['media_type'],'images')
        for config,content in [({**flags,'isRichEmbed':True},body),({**flags,'isSidecar':True},body),(flags,body.replace('/p/AbCd123/','/p/Other/')),(flags,body+body)]:
            with self.subTest(config=config),patch.object(app.source_http,'get',return_value=response(config,content)):
                with self.assertRaises(app.MediaError):app.instagram_embed_post('https://www.instagram.com/p/AbCd123/')

    def test_tiktok_share_forms_and_provider_photos(self):
        for url in ['https://www.tiktok.com/t/ZPLqxkx7d/','https://m.tiktok.com/v/123.html']:
            self.assertEqual(app.validate_source(url),url)
        response=httpx.Response(200,json={'code':0,'data':{'title':'Photo post','images':['https://cdn.example.org/a.jpg','https://cdn.example.org/b.jpg']}},request=httpx.Request('GET','https://www.tikwm.com/api/'))
        with patch.object(app.source_http,'get',return_value=response),patch.object(app,'public_url',side_effect=lambda u:u):
            self.assertEqual(len(app.tiktok_public_post('https://www.tiktok.com/t/ZPLqxkx7d/')['files']),2)

    def test_telegram_file_cache_resends_without_upload_and_uses_new_reply(self):
        job=app.Request(**{**self.job,'kind':'download','caption':'First'})
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'video.mp4';f.write_bytes(b'video')
            with patch.object(app,'telegram',return_value={'message_id':7,'video':{'file_id':'cached-telegram-file'}}):app.deliver([('video',f)],job)
            receipt=app.file_cache[app.media_key(job)][1]
            second=job.model_copy(update={'message_id':99,'caption':'Second'})
            with patch.object(app,'telegram',return_value={'message_id':8,'video':{'file_id':'cached-telegram-file'}}) as send:
                result=app.deliver(receipt,second,cached=True)
            self.assertTrue(result['cached']);self.assertEqual(send.call_args.args[1]['video'],'cached-telegram-file')
            self.assertEqual(send.call_args.args[1]['caption'],'Second')
            self.assertEqual(send.call_args.args[1]['reply_parameters']['message_id'],99)
            self.assertEqual(send.call_args.args[2],{})


    def test_twitter_text_only_and_photo_syndication(self):
        tweet={'id_str':'123','text':'A text-only tweet'}
        def response():return httpx.Response(200,json=tweet,request=httpx.Request('GET','https://cdn.syndication.twimg.com/'))
        with patch.object(app.source_http,'get',side_effect=lambda *a,**k:response()),patch.object(app,'public_url',side_effect=lambda u:u):
            post=app.twitter_public_post('https://x.com/user/status/123')
            self.assertEqual(app.metadata(post)['media_type'],'text')
            tweet['mediaDetails']=[{'type':'photo','media_url_https':'https://pbs.twimg.com/media/photo.jpg'}]
            post=app.twitter_public_post('https://x.com/user/status/123')
            self.assertEqual(app.metadata(post)['media_type'],'images')
            self.assertEqual(post['files'][0][0],'https://pbs.twimg.com/media/photo.jpg')

    def test_native_download_reuses_inspected_info(self):
        with tempfile.TemporaryDirectory() as d:
            args=app.native_input({'engine':'yt-dlp','info':{'title':'Inspected','formats':[]}},'https://x.com/user/status/123',d)
            self.assertEqual(args[0],'--load-info-json')
            self.assertEqual(json.loads(Path(args[1]).read_text())['title'],'Inspected')

    def test_spotify_fallback_skips_protected_recordings_and_requires_matching_duration(self):
        post={'entity':{'title':'Cheshme Man','artists':[{'name':'Dariush'}],'duration':297080}}
        entries={'entries':[{'title':'Cheshme Man','uploader':'Dariush','duration':297,'webpage_url':'https://soundcloud.com/dariush/protected'},
            {'title':'Cheshme Man','uploader':'Dariush','duration':30,'webpage_url':'https://soundcloud.com/dariush/preview'},
            {'title':'Cheshme Man Dariush','uploader':'Public','duration':297,'webpage_url':'https://soundcloud.com/public/recording'}]}
        with patch.object(app,'run',side_effect=[entries,app.MediaError('DRM protected'),{'title':'Cheshme Man Dariush','duration':297,'formats':[{}]}]) as run:
            selected=app.spotify_public_recording(post,'unused')
        self.assertEqual(selected['source_url'],'https://soundcloud.com/public/recording')
        self.assertEqual(run.call_count,3)

    def test_spotify_goriz_rejects_slowed_slug_and_wrong_duration_before_extraction(self):
        post={'entity':{'title':'Goriz','artists':[{'name':'Ebi'}],'duration':320066}}
        entries={'entries':[
            {'title':'Ebi - Goriz','uploader':'r.kh','duration':331.331,'webpage_url':'https://soundcloud.com/user-587823482/ebi-goriz-slowed-mp3'},
            {'title':'Ebi - Goriz','uploader':'Other','duration':320,'webpage_url':'https://soundcloud.com/other/ebi-goriz-slowed-mp3'},
            {'title':'Ebi - Goriz','uploader':'Ebi','duration':320.1,'webpage_url':'https://soundcloud.com/ebi/goriz'}]}
        clean={'title':'Ebi - Goriz','duration':320.1,'formats':[{}]}
        with patch.object(app,'run',side_effect=[entries,clean]) as command:
            result=app.spotify_public_recording(post,'unused')
        self.assertEqual(result['source_url'],'https://soundcloud.com/ebi/goriz')
        self.assertEqual(command.call_count,2)
        self.assertTrue(app.recording_duration_matches(320.1,320.066))
        self.assertFalse(app.recording_duration_matches(331.331,320.066))

    def test_spotify_rechecks_full_metadata_for_hidden_modified_versions(self):
        post={'entity':{'title':'Goriz','artists':[{'name':'Ebi'}],'duration':320066}}
        entries={'entries':[{'title':'Ebi - Goriz','duration':320,'webpage_url':'https://soundcloud.com/ebi/goriz'}]}
        for info in [{'title':'Ebi - Goriz (Sped Up)','duration':320,'formats':[{}]},
                     {'title':'Ebi - Goriz','track':'Goriz Remix','duration':320,'formats':[{}]},
                     {'title':'Ebi - Goriz','duration':331.331,'formats':[{}]}]:
            with self.subTest(info=info),patch.object(app,'run',side_effect=[entries,info]):
                with self.assertRaises(app.MediaError):app.spotify_public_recording(post,'unused')

    def test_spotify_prefers_verified_artist_youtube_without_invidious_wait(self):
        post={'entity':{'title':'Goriz','artists':[{'name':'Ebi'}],'duration':320066}}
        entries={'entries':[
            {'id':'slowed00001','title':'Ebi - Goriz Slowed','channel':'Ebi','channel_is_verified':True,'duration':320},
            {'id':'changed0001','title':'Ebi - Goriz','channel':'Ebi','channel_is_verified':True,'duration':331.331},
            {'id':'Gg7WaROxggI','title':'Goriz','channel':'EBI','channel_is_verified':True,'duration':321}]}
        resolved={'engine':'direct','info':{'title':'Goriz','duration':320.9}}
        with patch.object(app,'run',return_value=entries) as command,patch.object(app,'public_audio_post',side_effect=app.PublicAudioUnavailable('cold')),patch.object(app.source_http,'get') as slow,patch.object(app,'post_info',return_value=resolved) as fetch:
            result=app.spotify_recording(post,'unused',official_only=True)
        slow.assert_not_called();self.assertEqual(command.call_args.kwargs['timeout'],12)
        self.assertEqual(fetch.call_args.args[0],'https://www.youtube.com/watch?v=Gg7WaROxggI')
        self.assertEqual(result['expected_duration'],320.066)

    def test_spotify_ready_public_audio_skips_video_and_reuses_validated_source(self):
        source={'engine':'public-audio','files':[('https://cdn.example.org/audio.m4a',{'extension':'m4a'})],'info':{'duration':320},'expected_duration':320.066}
        post={'engine':'spotify','entity':{'title':'Goriz'}}
        job=app.Request(**{**self.job,'kind':'download','mode':'mp3'})
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'spotify_recording',return_value=source) as select,patch.object(app,'fetch_file') as fetch,patch.object(app,'run',return_value={'format':{'duration':'320.074'}}),patch.object(app,'normalize_audio',return_value=Path(directory)/'out.mp3'),patch.object(app,'download') as video:
            self.assertEqual(app.download_audio(post,job,directory)[0][0],'audio')
            app.download_audio(post,job,directory)
        self.assertEqual(select.call_count,1);self.assertEqual(fetch.call_count,2);video.assert_not_called()

    def test_public_audio_cloud_failure_keeps_verified_source_and_checks_actual_duration(self):
        post={'engine':'public-audio','files':[('https://cdn.example.org/audio.m4a',{})],'source_url':'https://youtube.com/watch?v=Gg7WaROxggI','expected_duration':320.066}
        job=app.Request(**{**self.job,'kind':'verify','mode':'mp3'})
        replacement={'engine':'direct','info':{'title':'Goriz','duration':320}}
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'fetch_file',side_effect=httpx.ConnectError('offline')),patch.object(app,'post_info',return_value=replacement) as resolve,patch.object(app,'download',return_value=[('video',Path('fallback.mp4'))]) as video,patch.object(app,'run',return_value={'format':{'duration':'331.285'}}),patch.object(app,'normalize_audio') as convert:
            with self.assertRaisesRegex(app.MediaError,'different version'):app.download_audio(post,job,directory)
        self.assertEqual(resolve.call_args.args[0],post['source_url']);self.assertEqual(video.call_args.args[1].url,post['source_url']);convert.assert_not_called()

    def test_spotify_validates_actual_direct_source_duration_before_audio_conversion(self):
        post={'engine':'direct','info':{'title':'Goriz'},'expected_duration':320.066}
        job=app.Request(**{**self.job,'kind':'download','mode':'mp3'})
        with patch.object(app,'download',return_value=[('video',Path('source.mp4'))]),patch.object(app,'run',return_value={'format':{'duration':'331.285'}}),patch.object(app,'normalize_audio') as convert:
            with self.assertRaisesRegex(app.MediaError,'different version'):app.download_audio(post,job,'unused')
        convert.assert_not_called()


    def test_direct_telegram_url_delivery_skips_upload_and_preserves_author_link(self):
        name='OGi';url='https://x.com/i/status/123'
        job=app.Request(**{**self.job,'kind':'download','caption':name+'\n\nTweet text','caption_entities':[{'type':'text_link','offset':0,'length':3,'url':url}]})
        with patch.object(app,'telegram',return_value={'message_id':8,'video':{'file_id':'delivered-file'}}) as api:
            result=app.deliver([('video','https://cdn.example.org/video.mp4')],job,remote=True)
        self.assertTrue(result['direct']);self.assertFalse(result['cached'])
        self.assertEqual(api.call_args.args[1]['video'],'https://cdn.example.org/video.mp4')
        self.assertEqual(api.call_args.args[2],{})
        self.assertEqual(api.call_args.args[1]['caption_entities'][0]['url'],url)


    def test_deezer_catalog_rejects_wrong_artist_and_version(self):
        entity={'title':'Cheshme Man','artists':[{'name':'Dariush'}],'duration':297080}
        tracks=[{'id':1,'title':'Cheshme Man','artist':{'name':'Someone else'},'duration':297},
          {'id':2,'title':'Cheshme Man','artist':{'name':'Dariush'},'duration':248},
          {'id':12582488,'title':'Cheshme Man','artist':{'name':'Dariush'},'duration':297,'preview':'https://cdn.example.org/preview.mp3'}]
        response=httpx.Response(200,json={'data':tracks},request=httpx.Request('GET','https://api.deezer.com/search'))
        with patch.object(app.source_http,'get',return_value=response):match=app.deezer_catalog_match(entity)
        self.assertEqual(match['id'],12582488)

if __name__=='__main__':unittest.main()
