"""Small authenticated media worker. No advertising, user accounts or media retention."""
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, urlencode
from difflib import SequenceMatcher

import httpx
from fastapi import FastAPI, Header, HTTPException
from langdetect import detect, DetectorFactory
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

DetectorFactory.seed = 0
Image.MAX_IMAGE_PIXELS = 25_000_000
MAX_VIDEO = 45 * 1024 * 1024
MAX_IMAGE = 10 * 1024 * 1024
ALLOWED_CHAT = int(os.environ.get('ALLOWED_CHAT_ID', '0'))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
pool = ThreadPoolExecutor(max_workers=1)
lock = threading.Lock()
jobs = {}
post_cache = {}
FALLBACK_BASE = os.environ.get('YOUTUBE_FALLBACK_URL', 'https://yt.chocolatemoo53.com').rstrip('/')


class MediaError(Exception):
    pass


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    kind: str = Field(pattern=r'^(inspect|download|sticker|verify)$')
    chat_id: int
    user_id: int | None = None
    message_id: int = Field(gt=0)
    topic: int = Field(default=0, ge=0)
    url: str | None = Field(default=None, max_length=2048)
    height: int | None = Field(default=None, ge=144, le=1080)
    file_id: str | None = Field(default=None, max_length=512)
    caption: str = Field(default='', max_length=10000)
    language: str | None = Field(default=None, max_length=16)
    mode: str = Field(default='video', pattern=r'^(video|images|audio|mp3|preview)$')


def validate_source(url):
    u = urlparse(url or '')
    host = (u.hostname or '').lower().removeprefix('www.')
    paths = {
        'youtube.com': r'/(watch|shorts/[\w-]+|live/[\w-]+)/?',
        'm.youtube.com': r'/(watch|shorts/[\w-]+)/?',
        'youtu.be': r'/[\w-]+/?',
        'tiktok.com': r'/@[^/]+/(video|photo)/\d+/?',
        'vm.tiktok.com': r'/[\w-]+/?', 'vt.tiktok.com': r'/[\w-]+/?',
        'instagram.com': r'/(p|reel|reels|tv)/[\w-]+/?',
        'x.com': r'/[^/]+/status/\d+/?', 'twitter.com': r'/[^/]+/status/\d+/?',
        'mobile.twitter.com': r'/[^/]+/status/\d+/?',
        'open.spotify.com':r'/(?:intl-[a-z]{2}/)?(?:track|episode|album|playlist|show)/[A-Za-z0-9]{22}/?',
        'spotify.link':r'/[A-Za-z0-9]+/?',
    }
    if u.scheme != 'https' or u.username or u.password or u.port or not re.fullmatch(paths.get(host, r'(?!)'), u.path):
        raise MediaError('Use a Spotify, YouTube, TikTok, X/Twitter or Instagram post link.')
    if host.endswith('youtube.com') and u.path == '/watch' and not re.fullmatch(r'[\w-]{6,}', parse_qs(u.query).get('v', [''])[0]):
        raise MediaError('Use a single YouTube video link.')
    return url


def public_url(url):
    u = urlparse(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port:
        raise MediaError('Unsupported media address.')
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise MediaError('Unsupported media address.')
    return url


def fetch_file(url, target, limit, headers=None):
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(5):
            public_url(url)
            with client.stream('GET', url, headers=headers or {}) as r:
                if r.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers.get('location', ''))
                    continue
                r.raise_for_status()
                size = 0
                with open(target, 'wb') as f:
                    for chunk in r.iter_bytes(65536):
                        size += len(chunk)
                        if size > limit:
                            raise MediaError('This media is too large. Choose a lower resolution or a shorter clip.')
                        f.write(chunk)
                return
    raise MediaError('The media address redirected too many times.')


def run(command, directory, timeout=180, json_output=False):
    output = Path(directory) / 'command-output.json'
    errors = Path(directory) / 'command-errors.txt'
    try:
        with open(output, 'wb') as f, open(errors, 'wb') as stderr:
            result = subprocess.run(command, cwd=directory, stdout=f, stderr=stderr, timeout=timeout, check=False)
        if result.returncode:
            reason=errors.read_text(encoding='utf-8',errors='replace')[-65536:].lower()
            if 'sign in to confirm' in reason or 'confirm you’re not a bot' in reason:
                raise MediaError('YouTube is blocking requests from this cloud server and requires sign-in. This video cannot currently be downloaded here.')
            if 'http error 429' in reason or 'too many requests' in reason:
                raise MediaError('The source site is rate-limiting this cloud server. Please try again later.')
            if 'login required' in reason or 'login-required' in reason or 'cookies are needed' in reason:
                raise MediaError('This post requires source-site login and cannot be downloaded anonymously.')
            if 'certificate_verify_failed' in reason or 'certificate verify failed' in reason:
                raise MediaError('The media server could not verify the source site’s TLS certificate.')
            raise MediaError('This post could not be downloaded. It may require login, be unavailable, or be blocked by the source site.')
        if output.stat().st_size > 8 * 1024 * 1024:
            raise MediaError('This post contains too much media.')
        if json_output:
            return json.loads(output.read_text(encoding='utf-8'))
    except subprocess.TimeoutExpired:
        raise MediaError('The source site took too long. Try again later or use a shorter video.') from None


def ytdlp_args():
    return [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--force-ipv4', '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1', '--js-runtimes', 'node']


def youtube_id(url):
    u=urlparse(url);host=(u.hostname or '').removeprefix('www.')
    if host=='youtu.be':return u.path.strip('/')
    if host in ('youtube.com','m.youtube.com'):
        return parse_qs(u.query).get('v',[''])[0] if u.path=='/watch' else u.path.rstrip('/').rsplit('/',1)[-1]
    return None


def format_height(f):
    match=re.search(r'(\d+)p',str(f.get('qualityLabel') or f.get('resolution') or ''))
    if match:return int(match.group(1))
    match=re.search(r'\d+x(\d+)',str(f.get('size') or ''))
    return int(match.group(1)) if match else 0


def fallback_post(url):
    video=youtube_id(url)
    if not FALLBACK_BASE or not re.fullmatch(r'[\w-]{11}',video or ''):raise MediaError('No free YouTube fallback is available.')
    public_url(FALLBACK_BASE)
    response=httpx.get(f'{FALLBACK_BASE}/api/v1/videos/{video}',params={'local':'true'},headers={'User-Agent':'Mozilla/5.0','Referer':FALLBACK_BASE+'/'},timeout=20)
    if not response.is_success:raise MediaError('The free YouTube fallback is temporarily unavailable or rate-limited.')
    data=response.json()
    if data.get('videoId')!=video or data.get('error'):raise MediaError('The free YouTube fallback could not retrieve this video.')
    formats=[*data.get('adaptiveFormats',[]),*data.get('formatStreams',[])]
    if not any(f.get('url') for f in formats):raise MediaError('No downloadable formats were found for this video.')
    if int(data.get('lengthSeconds') or 0)>600:raise MediaError('This free downloader supports videos up to 10 minutes.')
    return {'engine':'invidious','video_id':video,'info':{'title':data.get('title'),'description':data.get('description'),'duration':data.get('lengthSeconds')},'formats':formats}


def fallback_stream_url(post,format):
    # Metadata can contain expired signed CDN URLs. Resolve a fresh original
    # stream through the provider at download time instead of replaying them.
    itag=str(format.get('itag') or '')
    if not itag.isdecimal():raise MediaError('The selected stream could not be identified.')
    return FALLBACK_BASE+'/latest_version?'+urlencode({'id':post['video_id'],'itag':itag,'local':'true'})


def spotify_post(url):
    if urlparse(url).hostname=='spotify.link':
        with httpx.Client(follow_redirects=False,timeout=15) as client:
            for _ in range(5):
                public_url(url);response=client.get(url)
                if not response.is_redirect:break
                from urllib.parse import urljoin
                url=urljoin(url,response.headers.get('location',''))
                if (urlparse(url).hostname or '').endswith('spotify.com'):break
    validate_source(url)
    parts=urlparse(url).path.strip('/').split('/');kind,identifier=parts[-2:]
    if kind not in ('track','episode'):raise MediaError('Please send an individual Spotify track or podcast episode rather than a playlist or album.')
    response=httpx.get(f'https://open.spotify.com/embed/{kind}/{identifier}',timeout=20,follow_redirects=True)
    response.raise_for_status()
    match=re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',response.text,re.S)
    if not match:raise MediaError('Spotify metadata is temporarily unavailable.')
    entity=json.loads(match.group(1)).get('props',{}).get('pageProps',{}).get('state',{}).get('data',{}).get('entity',{})
    if not entity.get('title'):raise MediaError('Spotify could not identify this item.')
    title=str(entity['title']);artist=', '.join(a.get('name','') for a in (entity.get('artists') or []))
    return {'engine':'spotify','info':{'title':f'{artist} — {title}' if artist else title,'description':f'{artist} — {title}' if artist else title},'entity':entity,'url':url}


def post_info(url, directory):
    validate_source(url)
    for key in list(post_cache):
        if post_cache[key][0]<=time.time():del post_cache[key]
    if len(post_cache)>=20:del post_cache[next(iter(post_cache))]
    cached=post_cache.get(url)
    if cached and cached[0]>time.time():return cached[1]
    host=urlparse(url).hostname or ''
    if 'spotify' in host:return spotify_post(url)
    if youtube_id(url) and FALLBACK_BASE:
        try:
            post=fallback_post(url);post_cache[url]=(time.time()+600,post);return post
        except (MediaError,httpx.HTTPError,ValueError):pass
    try:
        info = run(ytdlp_args() + ['--dump-single-json', '--skip-download', '--', url], directory, json_output=True)
        entries = info.get('entries') or [info]
        if len(entries) > 20:
            raise MediaError('Use a post with 20 or fewer media items.')
        if any(e.get('duration', 0) and e['duration'] > 600 for e in entries):
            raise MediaError('This free downloader supports videos up to 10 minutes.')
        if any(e.get('formats') or e.get('url') for e in entries):
            return {'engine': 'yt-dlp', 'info': info, 'entries': entries}
    except MediaError:
        # Gallery support covers photo-only and mixed-media posts.
        if 'youtu' in (urlparse(url).hostname or ''):
            raise
    raw = run([sys.executable, '-m', 'gallery_dl', '--config-ignore', '--dump-json', '--range', '1-21', '--', url], directory, json_output=True)
    files = [(r[1], r[2]) for r in raw if isinstance(r, list) and len(r) > 2 and r[0] == 3]
    if not files:
        raise MediaError('No downloadable media found. The post may require login or be blocked by the source site.')
    if len(files) > 20:
        raise MediaError('Use a post with 20 or fewer media items.')
    return {'engine': 'gallery-dl', 'files': files, 'info': files[0][1]}


def metadata(post):
    info = post['info']
    caption = str(info.get('description') or info.get('content') or info.get('caption') or info.get('title') or '')[:6000]
    title = str(info.get('title') or caption[:120] or 'Media')[:180]
    try:
        language = 'fa' if re.search('[پچژگ]', caption) else detect(caption)
    except Exception:
        language = 'unknown'
    if post['engine'] == 'yt-dlp':
        entries = post['entries']
        all_formats=[f for e in entries for f in e.get('formats', [])]
        formats = [f for f in all_formats if f.get('vcodec') not in ('none',None)]
        heights = sorted({int(f['height']) for f in formats if f.get('height') and 144 <= f['height'] <= 1080})
        has_video = bool(formats) or any(e.get('height') for e in entries)
        has_audio = any(f.get('acodec') not in ('none',None) for f in all_formats)
        if not heights and has_video:
            heights = [min(1080, int(entries[0].get('height') or 720))]
    elif post['engine']=='invidious':
        heights=sorted({format_height(f) for f in post['formats'] if str(f.get('type','')).startswith('video/') and 144<=format_height(f)<=1080})
        has_video=bool(heights);has_audio=any(str(f.get('type','')).startswith('audio/') for f in post['formats'])
    elif post['engine']=='spotify':
        return {'title':title,'caption':caption,'language':language,'media_type':'audio','has_video':False,'has_audio':True,'heights':[],
          'audio_modes':(['preview'] if (post['entity'].get('audioPreview') or {}).get('url') else [])+['audio','mp3'],'spotify_match':True}
    else:
        has_video = any(str(meta.get('extension') or '').lower() in ('mp4', 'webm', 'mov') for _, meta in post['files'])
        heights = [360, 480, 720, 1080] if has_video else []
        has_audio=has_video
    return {'caption': caption, 'title': title, 'language': language, 'has_video': has_video,'has_audio':has_audio,'media_type':'video' if has_video else 'audio' if has_audio else 'images', 'heights': heights,'audio_modes':['audio','mp3'] if has_audio else []}


def convert_sticker(source, output):
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert('RGBA')
        width, height = image.size
        ratio = 512 / max(width, height)
        image = image.resize((max(1, round(width * ratio)), max(1, round(height * ratio))), Image.Resampling.LANCZOS)
        for quality in [92, 80, 65, 45]:
            image.save(output, 'WEBP', quality=quality, method=4)
            if Path(output).stat().st_size <= 512 * 1024:
                return
    raise MediaError('This image could not fit Telegram’s sticker size limit.')


def telegram(method, body, files=None):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise MediaError('The media server is not connected to Telegram yet.')
    with httpx.Client(timeout=120) as client:
        url = f'https://api.telegram.org/bot{token}/{method}'
        response = client.post(url, data={k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in body.items()}, files=files) if files else client.post(url, json=body)
        data = response.json()
    if not data.get('ok'):
        raise MediaError('Telegram could not deliver this media. Try a smaller file or lower resolution.')
    return data['result']


def base_body(job):
    body = {'chat_id': job.chat_id, 'reply_parameters': {'message_id': job.message_id, 'allow_sending_without_reply': True}}
    if job.topic:
        body['message_thread_id'] = job.topic
    return body


def album_batches(items):
    # Avoid a lone final image for 11, 21, ... items.
    groups = []
    items = list(items)
    while items:
        n = 9 if len(items) == 11 else min(10, len(items))
        groups.append(items[:n]); items = items[n:]
    return groups


def normalize_video(source, height, directory, index):
    if source.stat().st_size > MAX_VIDEO:
        raise MediaError('This video is too large. Choose a lower resolution.')
    output = Path(directory) / f'normalized-{index}.mp4'
    probe=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,codec_type,height','-of','json',str(source)],directory,json_output=True)
    if float(probe.get('format',{}).get('duration',601))>600:
        raise MediaError('This free downloader supports videos up to 10 minutes.')
    streams=probe.get('streams',[])
    video=next((s for s in streams if s.get('codec_type')=='video'),{})
    audio=next((s for s in streams if s.get('codec_type')=='audio'),{})
    compatible=video.get('codec_name')=='h264' and video.get('height',99999)<=(height or 1080) and audio.get('codec_name','aac') in ('aac','mp3')
    codec_args=['-c','copy'] if compatible else ['-vf',f"scale=-2:'max(2,trunc(min({height or 1080},ih)/2)*2)'",'-c:v','libx264','-threads','1','-filter_threads','1','-preset','ultrafast','-crf','26','-c:a','aac']
    run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),*codec_args,'-movflags','+faststart','-fs',str(MAX_VIDEO+1),str(output)],directory,timeout=240)
    if output.stat().st_size > MAX_VIDEO:
        raise MediaError('This video is too large. Choose a lower resolution.')
    return output


def normalize_audio(source, mode, directory, index):
    probe=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,codec_name,bit_rate','-of','json',str(source)],directory,json_output=True)
    if float(probe.get('format',{}).get('duration',601))>600:raise MediaError('This free downloader supports audio up to 10 minutes.')
    stream=next((s for s in probe.get('streams',[]) if s.get('codec_type')=='audio'),None)
    if not stream:raise MediaError('No audio stream was found in this post.')
    bitrate=min(192,max(64,int(stream.get('bit_rate') or 128000)//1000))
    output=Path(directory)/f'converted-audio-{index}.{ "mp3" if mode=="mp3" else "m4a" }'
    codec=['-c:a','copy'] if mode!='mp3' and stream.get('codec_name')=='aac' else ['-c:a','libmp3lame' if mode=='mp3' else 'aac','-b:a',f'{bitrate}k']
    run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-vn',*codec,'-threads','1','-fs',str(MAX_VIDEO+1),str(output)],directory,timeout=180)
    if output.stat().st_size>MAX_VIDEO:raise MediaError('This audio file is too large to send.')
    return output


def spotify_recording(post,directory):
    entity=post['entity'];title=str(entity.get('title') or '');artists=' '.join(a.get('name','') for a in (entity.get('artists') or []))
    query=f'{artists} {title} official audio'.strip();entries=[]
    if FALLBACK_BASE:
        try:
            response=httpx.get(FALLBACK_BASE+'/api/v1/search',params={'q':query,'type':'video'},headers={'User-Agent':'Mozilla/5.0','Referer':FALLBACK_BASE+'/'},timeout=20)
            if response.is_success:entries=response.json()[:10]
        except (httpx.HTTPError,ValueError):pass
    if not entries:
        data=run(ytdlp_args()+['--flat-playlist','--dump-single-json','--',f'ytsearch5:{query}'],directory,json_output=True)
        entries=data.get('entries',[])
    normalize=lambda text:' '.join(re.findall(r'\w+',str(text).casefold()))
    expected_duration=float(entity.get('duration') or 0)/1000
    ranked=[]
    for entry in entries:
        candidate=str(entry.get('title') or '');by=str(entry.get('author') or entry.get('channel') or '')
        duration=float(entry.get('lengthSeconds') or entry.get('duration') or 0)
        if duration>600 or expected_duration and duration and abs(duration-expected_duration)>max(15,expected_duration*.12):continue
        primary=normalize(next(iter(entity.get('artists') or []),{}).get('name',''))
        artist_match=not primary or primary in normalize(candidate+' '+by)
        if not artist_match:continue
        title_match=normalize(title) in normalize(candidate)
        score=.8 if title_match else .8*SequenceMatcher(None,normalize(title),normalize(candidate)).ratio()
        if primary:score+=.2
        if any(word in candidate.lower() and word not in title.lower() for word in ['cover','remix','live','karaoke','sped up','slowed']):score-=.4
        identifier=entry.get('videoId') or entry.get('id')
        if re.fullmatch(r'[\w-]{11}',identifier or ''):ranked.append((score,identifier))
    if not ranked or max(ranked)[0]<.65:raise MediaError('No confident public recording match was found for this Spotify track. Try its preview or send a direct video link.')
    url='https://www.youtube.com/watch?v='+max(ranked)[1]
    recording=dict(post_info(url,directory));recording['source_url']=url
    return recording


def download_audio(post,job,directory):
    if post['engine']=='spotify':
        if job.mode=='preview':
            url=(post['entity'].get('audioPreview') or {}).get('url')
            if not url:raise MediaError('Spotify does not provide a preview for this item.')
            target=Path(directory)/'spotify-preview.mp3';fetch_file(url,target,MAX_IMAGE)
            return [('audio',target)]
        post=spotify_recording(post,directory)
    paths=[]
    if post['engine']=='invidious':
        choices=[f for f in post['formats'] if str(f.get('type','')).startswith('audio/') and f.get('url')]
        if not choices:raise MediaError('No audio stream was found for this video.')
        selected=max(choices,key=lambda f:(str(f.get('type','')).startswith('audio/mp4'),int(f.get('bitrate') or 0)))
        target=Path(directory)/('source-audio.m4a' if 'mp4' in selected['type'] else 'source-audio.webm')
        fetch_file(fallback_stream_url(post,selected),target,MAX_VIDEO,{'Referer':FALLBACK_BASE+'/','User-Agent':'Mozilla/5.0'});paths=[target]
    elif post['engine']=='yt-dlp':
        run(ytdlp_args()+['--max-filesize','45M','-f','ba/b','-o',str(Path(directory)/'source-audio-%(autonumber)03d.%(ext)s'),'--',post.get('source_url') or job.url],directory,timeout=180)
        paths=sorted(p for p in Path(directory).glob('source-audio-*') if p.suffix in ('.m4a','.mp3','.webm','.mp4','.ogg','.opus'))
    else:
        return [('audio',normalize_audio(path,job.mode,directory,i)) for i,(kind,path) in enumerate(download(post,job.model_copy(update={'mode':'video'}),directory)) if kind=='video']
    if not paths:raise MediaError('No downloadable audio was found.')
    return [('audio',normalize_audio(p,job.mode,directory,i)) for i,p in enumerate(paths)]


def download(post, job, directory):
    if job.mode in ('audio','mp3','preview'):return download_audio(post,job,directory)
    paths = []
    if post['engine']=='invidious':
        choices=[f for f in post['formats'] if str(f.get('type','')).startswith('video/') and f.get('url') and 0<format_height(f)<=(job.height or 720)]
        if not choices:raise MediaError('That video resolution is not available. Send the link again to refresh the options.')
        selected=max(choices,key=lambda f:(format_height(f),'mp4' in f.get('type',''),int(f.get('bitrate') or 0)))
        video=Path(directory)/'source-video.bin';headers={'Referer':FALLBACK_BASE+'/','User-Agent':'Mozilla/5.0'}
        fetch_file(fallback_stream_url(post,selected),video,MAX_VIDEO,headers)
        audio_choices=[f for f in post['formats'] if str(f.get('type','')).startswith('audio/') and f.get('url')]
        merged=Path(directory)/'video-merged.mkv'
        if audio_choices:
            audio=max(audio_choices,key=lambda f:('mp4' in f.get('type',''),int(f.get('bitrate') or 0)))
            sound=Path(directory)/'source-audio.bin';fetch_file(fallback_stream_url(post,audio),sound,MAX_VIDEO,headers)
            run(['ffmpeg','-nostdin','-v','error','-y','-i',str(video),'-i',str(sound),'-map','0:v:0','-map','1:a:0','-c','copy',str(merged)],directory,timeout=60)
            paths=[merged]
        else:paths=[video]
    elif post['engine'] == 'yt-dlp':
        height = job.height or 720
        fmt = f'bv*[height<={height}][ext=mp4]+ba[ext=m4a]/b[height<={height}][ext=mp4]/bv*[height<={height}]+ba/b[height<={height}]'
        run(ytdlp_args() + ['--max-filesize', '45M', '-f', fmt, '--merge-output-format', 'mp4', '-o', str(Path(directory) / 'video-%(autonumber)03d.%(ext)s'), '--', job.url], directory, timeout=240)
        paths = sorted(p for p in Path(directory).glob('video-*') if p.suffix in ('.mp4', '.webm', '.mkv', '.mov'))
    else:
        for i, (url, meta) in enumerate(post['files']):
            ext = str(meta.get('extension') or 'jpg').lower()
            if ext not in ('jpg', 'jpeg', 'png', 'webp', 'mp4', 'webm', 'mov'):
                raise MediaError('This post contains an unsupported media format.')
            target = Path(directory) / f'gallery-{i}.{ext}'
            fetch_file(url, target, MAX_VIDEO if ext in ('mp4', 'webm', 'mov') else MAX_IMAGE, {'Referer': job.url, 'User-Agent': 'Mozilla/5.0'})
            paths.append(target)
    if not paths:
        raise MediaError('The requested resolution could not be downloaded. Try another resolution.')
    normalized = []
    for i, p in enumerate(paths):
        if p.suffix in ('.mp4', '.webm', '.mkv', '.mov','.bin'):
            normalized.append(('video', normalize_video(p, job.height, directory, i)))
        else:
            target = Path(directory) / f'photo-{i}.jpg'
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert('RGB'); im.thumbnail((2560, 2560)); im.save(target, 'JPEG', quality=90)
            normalized.append(('photo', target))
    return normalized


def deliver(items, job):
    ids = []
    caption = job.caption
    for index, batch in enumerate(album_batches(items)):
        with contextlib.ExitStack() as stack:
            files, media = {}, []
            for i, (kind, path) in enumerate(batch):
                key = f'file{i}'
                files[key] = (path.name, stack.enter_context(open(path, 'rb')), 'video/mp4' if kind == 'video' else ('audio/mpeg' if path.suffix=='.mp3' else 'audio/mp4') if kind=='audio' else 'image/jpeg')
                item = {'type': kind, 'media': f'attach://{key}'}
                if i == 0 and index == 0 and len(caption) <= 1024:
                    item['caption'] = caption
                if kind == 'video':
                    item['supports_streaming'] = True
                media.append(item)
            if len(batch) > 1:
                sent = telegram('sendMediaGroup', {**base_body(job), 'media': media}, files)
                ids.extend(m['message_id'] for m in sent)
            else:
                kind, _ = batch[0]
                body = {**base_body(job), kind: 'attach://file0', 'caption': caption if index == 0 and len(caption) <= 1024 else ''}
                if kind == 'video': body['supports_streaming'] = True
                sent = telegram({'video':'sendVideo','audio':'sendAudio','photo':'sendPhoto'}[kind], body, files)
                ids.append(sent['message_id'])
    if len(caption) > 1024:
        for offset in range(0, len(caption), 3900):
            sent = telegram('sendMessage', {**base_body(job), 'text': caption[offset:offset + 3900], 'link_preview_options': {'is_disabled': True}})
            ids.append(sent['message_id'])
    return {'message_ids': ids}


def execute(job):
    try:
        with tempfile.TemporaryDirectory(prefix='bb-media-') as directory:
            if job.kind == 'sticker':
                f = telegram('getFile', {'file_id': job.file_id})
                if f.get('file_size', 0) > MAX_IMAGE:
                    raise MediaError('Use an image up to 10 MB.')
                token = os.environ['TELEGRAM_BOT_TOKEN']
                source, output = Path(directory) / 'input', Path(directory) / 'sticker.webp'
                fetch_file(f'https://api.telegram.org/file/bot{token}/{f["file_path"]}', source, MAX_IMAGE)
                convert_sticker(source, output)
                with open(output, 'rb') as stream:
                    sent = telegram('sendSticker', {**base_body(job), 'sticker': 'attach://sticker'}, {'sticker': ('sticker.webp', stream, 'image/webp')})
                result = {'message_id': sent['message_id']}
            else:
                post = post_info(job.url, directory)
                if job.kind=='inspect':result=metadata(post)
                elif job.kind=='verify':
                    files=download(post,job,directory)
                    result={'verification_only':True,'metadata':metadata(post),'files':[{'kind':kind,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for kind,p in files]}
                else:result=deliver(download(post, job, directory), job)
        with lock:
            jobs[job.id].update(status='done', result=result)
    except Exception as error:
        # No URLs, bot tokens, source captions or raw library exceptions in responses/logs.
        message = str(error) if isinstance(error, MediaError) else 'Media processing failed. Try again later or choose a smaller file.'
        with lock:
            jobs[job.id].update(status='error', error=message)


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/jobs')
def submit(job: Request, authorization: str = Header(default='')):
    secret = os.environ.get('MEDIA_BACKEND_SECRET', '')
    if len(secret) < 32 or not hmac.compare_digest(authorization, f'Bearer {secret}'):
        raise HTTPException(401, 'Unauthorized')
    if job.chat_id != ALLOWED_CHAT and not (job.chat_id>0 and job.user_id==job.chat_id):
        raise HTTPException(403, 'Group not allowed')
    if job.kind != 'sticker':
        try: validate_source(job.url)
        except MediaError: raise HTTPException(422,'Unsupported post link') from None
    elif not job.file_id: raise HTTPException(422, 'Image required')
    fingerprint = hashlib.sha256(job.model_dump_json().encode()).hexdigest()
    with lock:
        for key in list(jobs):
            if jobs[key]['created'] < time.time() - 3600 and jobs[key]['status'] in ('done', 'error'):
                del jobs[key]
        existing = jobs.get(job.id)
        if existing:
            if existing['fingerprint'] != fingerprint: raise HTTPException(409, 'Job ID already used')
            return {k: existing[k] for k in ('status', 'result', 'error') if k in existing}
        if sum(j['status'] == 'running' for j in jobs.values()) >= 5:
            raise HTTPException(429, 'Queue full')
        jobs[job.id] = {'status': 'running', 'created': time.time(), 'fingerprint': fingerprint}
        pool.submit(execute, job)
    return {'status': 'running'}
