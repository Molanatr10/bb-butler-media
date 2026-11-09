"""Small authenticated media worker. No advertising, user accounts or media retention."""
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, urlencode
from difflib import SequenceMatcher

import httpx
from fastapi import FastAPI, Header, HTTPException
from langdetect import detect, DetectorFactory
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field
from youtube_public import omega_post, PublicYouTubeError, PublicYouTubeLimitError
from youtube_audio_public import public_audio_post, PublicAudioUnavailable
from youtube_audio_proxy import download_proxy_audio, ProxyAudioUnavailable

DetectorFactory.seed = 0
Image.MAX_IMAGE_PIXELS = 25_000_000
MAX_VIDEO = 45 * 1024 * 1024
MAX_IMAGE = 10 * 1024 * 1024
ALLOWED_CHAT = int(os.environ.get('ALLOWED_CHAT_ID', '0'))
ADDITIONAL_ALLOWED_CHATS = {-1002568879931}  # Bellona GP, enabled by the owner.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
pool = ThreadPoolExecutor(max_workers=2)
transcode_gate = threading.Semaphore(1)
inspect_pool = ThreadPoolExecutor(max_workers=2)
delivery_pool = ThreadPoolExecutor(max_workers=2)
youtube_pool = ThreadPoolExecutor(max_workers=2)
source_http = httpx.Client(timeout=15, follow_redirects=False, limits=httpx.Limits(max_connections=12,max_keepalive_connections=8,keepalive_expiry=30))
telegram_http = httpx.Client(timeout=120, limits=httpx.Limits(max_connections=4,max_keepalive_connections=4,keepalive_expiry=60))
lock = threading.Lock()
jobs = {}
post_cache = {}
cache_lock = threading.RLock()
source_locks = {}
file_cache = {}
FALLBACK_BASE = os.environ.get('YOUTUBE_FALLBACK_URL', 'https://yt.chocolatemoo53.com').rstrip('/')


class MediaError(Exception):
    pass


class TelegramDeliveryError(MediaError):
    def __init__(self,code):
        super().__init__('Telegram could not deliver this media. Try a smaller file or lower resolution.')
        self.code=code


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
    caption: str = Field(default='', max_length=16000)
    language: str | None = Field(default=None, max_length=16)
    caption_entities: list[dict] = Field(default_factory=list,max_length=10)
    mode: str = Field(default='video', pattern=r'^(video|images|audio|mp3|preview)$')


def validate_source(url):
    u = urlparse(url or '')
    host = (u.hostname or '').lower().removeprefix('www.')
    paths = {
        'youtube.com': r'/(watch|shorts/[\w-]+|live/[\w-]+)/?',
        'm.youtube.com': r'/(watch|shorts/[\w-]+)/?',
        'youtu.be': r'/[\w-]+/?',
        'tiktok.com': r'/(?:@[^/]+/(?:video|photo)/\d+|t/[A-Za-z0-9]+|v/\d+\.html|embed/v2/\d+)/?',
        'm.tiktok.com': r'/(?:@[^/]+/(?:video|photo)/\d+|t/[A-Za-z0-9]+|v/\d+\.html)/?',
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
    for _ in range(5):
        public_url(url)
        with source_http.stream('GET', url, headers=headers or {},timeout=30) as r:
            if r.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, r.headers.get('location', ''))
                continue
            r.raise_for_status()
            declared=r.headers.get('content-length')
            if declared and declared.isdigit() and int(declared)>limit:raise MediaError('This media is too large. Choose a lower resolution or a shorter clip.')
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
        transcode=command[0]=='ffmpeg' and any(codec in command for codec in ('libx264','libmp3lame','aac'))
        with transcode_gate if transcode else contextlib.nullcontext():
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
    return [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--concurrent-fragments', '4', '--force-ipv4', '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1', '--js-runtimes', 'node']


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


def youtube_native(url, directory, mode=None, height=None):
    video=youtube_id(url)
    if not re.fullmatch(r'[\w-]{11}',video or ''):raise MediaError('Invalid YouTube video.')
    return run([sys.executable,str(Path(__file__).with_name('youtube_native.py')),
                'download' if mode else 'inspect',video,mode or 'video',str(height or 720)],
               directory,timeout=150 if mode else 35,json_output=True)


def fallback_post(url):
    video=youtube_id(url)
    if not FALLBACK_BASE or not re.fullmatch(r'[\w-]{11}',video or ''):raise MediaError('No free YouTube fallback is available.')
    public_url(FALLBACK_BASE)
    response=source_http.get(f'{FALLBACK_BASE}/api/v1/videos/{video}',params={'local':'true'},headers={'User-Agent':'Mozilla/5.0','Referer':FALLBACK_BASE+'/'},timeout=20)
    if response.status_code==403:
        try:
            from curl_cffi import requests as browser_http
            response=browser_http.get(f'{FALLBACK_BASE}/api/v1/videos/{video}',params={'local':'true'},impersonate='chrome',allow_redirects=False,verify=True,timeout=12)
        except Exception:raise MediaError('The free YouTube fallback blocks this cloud server.') from None
    if response.status_code!=200:raise MediaError('The free YouTube fallback is temporarily unavailable or rate-limited.')
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


def deezer_catalog_match(entity):
    title=str(entity.get('title') or '')
    artist=next(iter(entity.get('artists') or []),{}).get('name','')
    expected=float(entity.get('duration') or 0)/1000
    if not title or not artist:return None
    response=source_http.get('https://api.deezer.com/search',params={'q':artist+' '+title,'limit':10},timeout=6)
    response.raise_for_status();tracks=response.json().get('data') or []
    normalize=lambda text:' '.join(re.findall(r'\w+',str(text).casefold()))
    candidates=[]
    for track in tracks:
        if normalize(track.get('title'))!=normalize(title):continue
        if normalize((track.get('artist') or {}).get('name'))!=normalize(artist):continue
        duration=float(track.get('duration') or 0)
        if expected and abs(duration-expected)>max(5,expected*.02):continue
        if not track.get('id'):continue
        candidates.append((abs(duration-expected),track['id'],track))
    return min(candidates,key=lambda row:row[:2])[2] if candidates else None


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
    response=source_http.get(f'https://open.spotify.com/embed/{kind}/{identifier}',timeout=20,follow_redirects=True)
    response.raise_for_status()
    match=re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',response.text,re.S)
    if not match:raise MediaError('Spotify metadata is temporarily unavailable.')
    entity=json.loads(match.group(1)).get('props',{}).get('pageProps',{}).get('state',{}).get('data',{}).get('entity',{})
    if not entity.get('title'):raise MediaError('Spotify could not identify this item.')
    title=str(entity['title']);artist=', '.join(a.get('name','') for a in (entity.get('artists') or []))
    try:deezer=deezer_catalog_match(entity)
    except (httpx.HTTPError,ValueError,TypeError):deezer=None
    return {'engine':'spotify','info':{'title':f'{artist} — {title}' if artist else title,'description':f'{artist} — {title}' if artist else title},'entity':entity,'url':url,'deezer':deezer}


def post_info(url, directory):
    validate_source(url)
    if youtube_id(url):url='https://www.youtube.com/watch?v='+youtube_id(url)
    with cache_lock:
        cached=post_cache.get(url)
        if cached and cached[0]>time.time():return cached[1]
        if len(source_locks)>=32:
            for key in list(source_locks):
                if not source_locks[key].locked():source_locks.pop(key,None)
        gate=source_locks.setdefault(url,threading.Lock())
    with gate:
        with cache_lock:
            cached=post_cache.get(url)
            if cached and cached[0]>time.time():return cached[1]
        post=_extract_post_info(url,directory)
        with cache_lock:
            for key in list(post_cache):
                if post_cache[key][0]<=time.time():post_cache.pop(key,None)
            if len(post_cache)>=20:post_cache.pop(next(iter(post_cache)),None)
            ttl=3600 if youtube_id(url) else 600
            # Signed playback URLs may expire sooner than reusable metadata.
            for address,_ in post.get('files',[]):
                if youtube_id(url):
                    try:
                        expires=float(parse_qs(urlparse(address).query).get('expire',[''])[0])
                        if math.isfinite(expires):ttl=min(ttl,max(0,expires-time.time()-120))
                    except (ValueError,TypeError):pass
            post_cache[url]=(time.time()+ttl,post)
        return post


def twitter_json(identifier,timeout=10):
    from yt_dlp.jsinterp import js_number_to_string
    token=js_number_to_string((int(identifier)/1e15)*math.pi,36).translate(str.maketrans(dict.fromkeys('0.')))
    response=source_http.get('https://cdn.syndication.twimg.com/tweet-result',params={'id':identifier,'lang':'en','token':token},timeout=timeout)
    response.raise_for_status();data=response.json()
    if data.get('id_str')!=identifier:raise MediaError('Twitter did not return this public post.')
    return data


def twitter_context(data,identifier):
    node=data.get('parent') or {}
    if not isinstance(node,dict):node={}
    parent=data.get('in_reply_to_status_id_str')
    if not node and isinstance(parent,str) and re.fullmatch(r'\d{1,25}',parent) and parent!=identifier:
        try:node=twitter_json(parent,timeout=2)
        except (MediaError,httpx.HTTPError,ValueError,TypeError):pass
    if not node:node=data.get('quoted_tweet') or {}
    if not isinstance(node,dict):return None
    target=node.get('id_str')
    if not isinstance(target,str) or not re.fullmatch(r'\d{1,25}',target) or target==identifier:return None
    user=node.get('user') or {};caption=str(node.get('text') or '')[:6000]
    has_media=bool(node.get('video') or node.get('video_info') or node.get('mediaDetails') or node.get('photos'))
    if not caption.strip() and not has_media:return None
    return {'caption':caption,'language':str(node.get('lang') or 'unknown'),'has_media':has_media,'url':'https://x.com/i/status/'+target,
        'source_author':{'name':str(user.get('name') or user.get('screen_name') or 'Twitter author')[:128],'username':str(user.get('screen_name') or ''),'url':'https://x.com/i/status/'+target+'?mx=2'}}


def twitter_public_post(url):
    identifier=urlparse(url).path.rstrip('/').split('/')[-1]
    data=twitter_json(identifier)
    caption=str(data.get('text') or '');files=[];heights=set()
    for media in data.get('mediaDetails') or []:
        if media.get('type')=='photo':
            address=media.get('media_url_https') or media.get('media_url')
            if address:files.append((public_url(address),{'extension':'jpg'}))
        elif media.get('type') in ('video','animated_gif'):
            info=media.get('video_info') or {}
            if float(info.get('duration_millis') or 0)>600000:raise MediaError('This free downloader supports videos up to 10 minutes.')
            variants=[]
            for v in info.get('variants') or []:
                if v.get('content_type')!='video/mp4':continue
                address=public_url(v.get('url'));dimensions=re.search(r'/(\d+)x(\d+)/',address)
                h=min(map(int,dimensions.groups())) if dimensions else min((media.get('original_info') or {}).get('width',1080),(media.get('original_info') or {}).get('height',1080))
                variants.append({'url':address,'height':h,'bitrate':int(v.get('bitrate') or 0),'filesize':v.get('filesize')})
                if 144<=h<=1080:heights.add(h)
            if variants:
                best=max(variants,key=lambda v:(v['height'],v['bitrate']))
                files.append((best['url'],{'extension':'mp4','variants':variants,'duration':float(info.get('duration_millis') or 0)/1000}))
    if len(files)>20:raise MediaError('Use a post with 20 or fewer media items.')
    if not files and not caption:raise MediaError('This tweet has no public text or media.')
    author=data.get('user') or {};name=str(author.get('name') or author.get('screen_name') or 'Twitter author')[:128]
    sizes={}
    for height in heights:
        total=0;estimated=False
        for _,meta in files:
            if not meta.get('variants'):continue
            choices=[v for v in meta['variants'] if v['height']<=height] or meta['variants']
            selected=max(choices,key=lambda v:(v['height'],v['bitrate']))
            exact=selected.get('filesize')
            if isinstance(exact,(int,float)) and math.isfinite(exact) and exact>0:total+=exact
            elif selected['bitrate']>0 and meta['duration']>0:
                total+=selected['bitrate']*meta['duration']/8;estimated=True
            else:break
        else:
            if total>0:sizes[height]={'bytes':round(total),'estimated':estimated}
    return {'engine':'direct' if files else 'text','files':files,'heights':sorted(heights),'video_sizes':sizes,'info':{'title':caption[:180] or 'Twitter post','description':caption},'context_tweet':twitter_context(data,identifier),'source_author':{'name':name,'username':str(author.get('screen_name') or ''),'url':'https://x.com/i/status/'+identifier+'?mx=2'},'original_only':not heights}


class InstagramSimplePhoto(HTMLParser):
    """Read only the post image, never avatars, recommendations or video covers."""
    def __init__(self,shortcode):
        super().__init__(convert_charrefs=True)
        self.shortcode=shortcode;self.stack=[];self.images=[];self.caption=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);classes=set(attrs.get('class','').split())
        if tag=='img' and 'EmbeddedMediaImage' in classes:
            for parent,values in reversed(self.stack):
                if parent=='a' and 'EmbeddedMedia' in values.get('class','').split():
                    link=urlparse(values.get('href',''))
                    if link.scheme=='https' and link.hostname in ('www.instagram.com','instagram.com') and link.path.rstrip('/') in ('/p/'+self.shortcode,'/reel/'+self.shortcode):
                        self.images.append(attrs.get('src',''))
                    break
        if tag=='br' and any('Caption' in a.get('class','').split() for _,a in self.stack):self.caption.append('\n')
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.stack.append((tag,attrs))

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break

    def handle_data(self,data):
        if any('Caption' in a.get('class','').split() for _,a in self.stack) and not any('CaptionUsername' in a.get('class','').split() for _,a in self.stack):self.caption.append(data)


def instagram_embed_post(url):
    shortcode=urlparse(url).path.strip('/').split('/')[-1]
    response=source_http.get(f'https://www.instagram.com/p/{shortcode}/embed/captioned/',headers={'User-Agent':'Mozilla/5.0'},timeout=12)
    response.raise_for_status()
    if len(response.content)>4*1024*1024:raise MediaError('Instagram returned too much metadata.')
    match=re.search(r'"contextJSON"\s*:\s*("(?:\\.|[^"\\])*")',response.text)
    if not match:
        # Instagram serves some single photos as HTML with contextJSON:null.
        # Explicit simple/non-sidecar flags prevent returning a video cover or
        # silently truncating an album to its first photo.
        flags=re.search(r'"PolarisEmbedSimple","init",\[\],\[({[^{}]+})\]',response.text)
        config=json.loads(flags.group(1)) if flags else {}
        if config.get('contextJSON','missing') is None and config.get('isRichEmbed') is False and config.get('isSidecar') is False and config.get('isProfileEmbed') is False and config.get('isGuideEmbed') is False:
            photo=InstagramSimplePhoto(shortcode);photo.feed(response.text)
            if len(photo.images)==1:
                caption=''.join(photo.caption).strip()
                return {'engine':'direct','files':[(public_url(photo.images[0]),{'extension':'jpg'})],'info':{'title':caption[:180] or 'Instagram photo','description':caption},'original_only':True}
        raise MediaError('Instagram did not expose public embed media.')
    context=json.loads(json.loads(match.group(1)))
    media=context.get('gql_data',{}).get('shortcode_media',{})
    if media.get('shortcode')!=shortcode or context.get('context',{}).get('copyright_blocked'):raise MediaError('This Instagram post is not publicly downloadable.')
    caption='\n'.join(x.get('node',{}).get('text','') for x in media.get('edge_media_to_caption',{}).get('edges',[]))
    nodes=[x.get('node',{}) for x in media.get('edge_sidecar_to_children',{}).get('edges',[])] or [media]
    files=[]
    for node in nodes:
        if float(node.get('video_duration') or 0)>600:raise MediaError('This free downloader supports videos up to 10 minutes.')
        video=node.get('video_url');address=video or node.get('display_url')
        if address:files.append((public_url(address),{'extension':'mp4' if video else 'jpg'}))
    if not files or len(files)>20:raise MediaError('No supported Instagram media was found.')
    return {'engine':'direct','files':files,'info':{'title':caption[:180] or 'Instagram media','description':caption},'original_only':True}


def tiktok_public_post(url):
    response=source_http.get('https://www.tikwm.com/api/',params={'url':url},timeout=12)
    response.raise_for_status()
    data=response.json()
    if data.get('code')!=0:raise MediaError('The public TikTok downloader could not retrieve this post.')
    data=data.get('data') or {};files=[]
    if float(data.get('duration') or 0)>600:raise MediaError('This free downloader supports videos up to 10 minutes.')
    if data.get('images'):
        files=[(public_url(address),{'extension':'jpg'}) for address in data['images']]
    else:
        address=data.get('play') or data.get('hdplay')
        if address:files=[(public_url(address),{'extension':'mp4'})]
    if not files or len(files)>20:raise MediaError('No supported TikTok media was found.')
    return {'engine':'direct','files':files,'info':{'title':data.get('title') or 'TikTok media','description':data.get('title') or ''},'original_only':True}


def _extract_post_info(url,directory):
    host=urlparse(url).hostname or ''
    if 'spotify' in host:return spotify_post(url)
    if host.removeprefix('www.') in ('x.com','twitter.com','mobile.twitter.com'):
        try:return twitter_public_post(url)
        except (MediaError,httpx.HTTPError,ValueError,TypeError):pass
    if host.removeprefix('www.').endswith('tiktok.com'):
        try:return tiktok_public_post(url)
        except (MediaError,httpx.HTTPError,ValueError):pass
    if host.removeprefix('www.')=='instagram.com':
        try:return instagram_embed_post(url)
        except (MediaError,httpx.HTTPError,ValueError,KeyError):pass
    public_limit = None
    if youtube_id(url):
        try:return omega_post(url,source_http)
        except PublicYouTubeLimitError as error:public_limit = str(error)
        except (PublicYouTubeError,httpx.HTTPError,ValueError,TypeError):pass
        try:
            native=youtube_native(url,directory)
            if native.get('heights'):return {'engine':'youtube-native',**native}
        except (MediaError,ValueError):pass
    if youtube_id(url) and FALLBACK_BASE:
        try:
            return fallback_post(url)
        except (MediaError,httpx.HTTPError,ValueError):pass
    try:
        info = run(ytdlp_args() + ['--dump-single-json', '--skip-download', '--', url], directory, timeout=40, json_output=True)
        entries = info.get('entries') or [info]
        if len(entries) > 20:
            raise MediaError('Use a post with 20 or fewer media items.')
        if any(e.get('duration', 0) and e['duration'] > 600 for e in entries):
            raise MediaError('This free downloader supports videos up to 10 minutes.')
        if any(e.get('formats') or e.get('url') for e in entries):
            return {'engine': 'yt-dlp', 'info': info, 'entries': entries,
                    'portrait_resolution': host.removeprefix('www.')=='instagram.com'}
    except MediaError:
        # Gallery support covers photo-only and mixed-media posts.
        if 'youtu' in (urlparse(url).hostname or ''):
            if public_limit:
                raise MediaError(public_limit) from None
            raise
    raw = run([sys.executable, '-m', 'gallery_dl', '--config-ignore', '--dump-json', '--range', '1-21', '--', url], directory, timeout=35, json_output=True)
    files = [(r[1], r[2]) for r in raw if isinstance(r, list) and len(r) > 2 and r[0] == 3]
    if not files:
        raise MediaError('No downloadable media found. The post may require login or be blocked by the source site.')
    if len(files) > 20:
        raise MediaError('Use a post with 20 or fewer media items.')
    return {'engine': 'gallery-dl', 'files': files, 'info': files[0][1]}


def format_bytes(format,duration):
    for field,estimated in [('filesize',False),('contentLength',False),('filesize_approx',True)]:
        try:
            size=float(format.get(field) or 0)
            if math.isfinite(size) and size>0:return size,estimated
        except (TypeError,ValueError):pass
    try:
        bitrate=float(format.get('tbr') or 0)*1000 or float(format.get('bitrate') or 0)
        duration=float(duration or 0)
        if math.isfinite(bitrate) and math.isfinite(duration) and bitrate>0 and duration>0:return bitrate*duration/8,True
    except (TypeError,ValueError):pass
    return None


def video_format_sizes(post,heights):
    if post.get('video_sizes'):return post['video_sizes']
    if post.get('portrait_resolution'):return {}
    sizes={}
    if post['engine'] not in ('yt-dlp','invidious'):return sizes
    entries=post['entries'] if post['engine']=='yt-dlp' else [post]
    for height in heights:
        total=0;estimated=False
        for entry in entries:
            formats=entry.get('formats',[]);duration=entry.get('duration') or post['info'].get('duration')
            if post['engine']=='yt-dlp':
                video=[f for f in formats if f.get('vcodec') not in ('none',None) and 0<float(f.get('height') or 0)<=height]
                audio=[f for f in formats if f.get('vcodec')=='none' and f.get('acodec') not in ('none',None)]
                mp4=[f for f in video if f.get('ext')=='mp4'];video=mp4 or video
                if not video:break
                selected=max(video,key=lambda f:(float(f.get('height') or 0),float(f.get('tbr') or 0)))
                audio=audio if selected.get('acodec') in ('none',None) else []
            else:
                video=[f for f in formats if str(f.get('type','')).startswith('video/') and 0<format_height(f)<=height]
                audio=[f for f in formats if str(f.get('type','')).startswith('audio/')]
                if not video:break
                selected=max(video,key=lambda f:(format_height(f),'mp4' in f.get('type',''),int(f.get('bitrate') or 0)))
            parts=[selected]
            if audio:parts.append(max(audio,key=lambda f:(f.get('ext')=='m4a' or 'mp4' in f.get('type',''),float(f.get('abr') or f.get('bitrate') or 0))))
            values=[format_bytes(f,duration) for f in parts]
            if any(v is None for v in values):break
            total+=sum(v[0] for v in values);estimated=estimated or len(parts)>1 or any(v[1] for v in values)
        else:
            if total>0:sizes[height]={'bytes':round(total),'estimated':estimated}
    return sizes


def source_resolution(format, portrait=False):
    height = int(format.get('height') or 0)
    width = int(format.get('width') or 0)
    return min(width, height) if portrait and width and height else height


def metadata(post):
    info = post['info']
    caption = str(info.get('description') or info.get('content') or info.get('caption') or info.get('title') or '')[:6000]
    title = str(info.get('title') or caption[:120] or 'Media')[:180]
    try:
        language = 'fa' if re.search('[پچژگ]', caption) else detect(caption)
    except Exception:
        language = 'unknown'
    if post['engine']=='text':return {'title':title,'caption':caption,'language':language,'media_type':'text','source_author':post.get('source_author'),'context_tweet':post.get('context_tweet'),'has_video':False,'has_audio':False,'heights':[],'audio_modes':[]}
    if post['engine'] == 'yt-dlp':
        entries = post['entries']
        all_formats=[f for e in entries for f in e.get('formats', [])]
        formats = [f for f in all_formats if f.get('vcodec') not in ('none',None)]
        portrait=post.get('portrait_resolution',False)
        resolutions={source_resolution(f,portrait) for f in formats}
        heights=sorted(size for size in resolutions if 144 <= size <= 1080)
        has_video = bool(formats) or any(e.get('height') for e in entries)
        has_audio = any(f.get('acodec') not in ('none',None) for f in all_formats)
        if not heights and has_video:
            heights = [min(1080, source_resolution(entries[0],portrait) or 720)]
    elif post['engine']=='youtube-native':
        heights=post['heights'];has_video=bool(heights);has_audio=post['has_audio']
    elif post['engine']=='invidious':
        heights=sorted({format_height(f) for f in post['formats'] if str(f.get('type','')).startswith('video/') and 144<=format_height(f)<=1080})
        has_video=bool(heights);has_audio=any(str(f.get('type','')).startswith('audio/') for f in post['formats'])
    elif post['engine']=='spotify':
        return {'title':title,'caption':caption,'language':language,'media_type':'audio','has_video':False,'has_audio':True,'heights':[],
          'audio_modes':(['preview'] if (post.get('deezer') or {}).get('preview') or (post['entity'].get('audioPreview') or {}).get('url') else [])+['audio','mp3'],'spotify_match':True,'catalog_source':'Deezer' if post.get('deezer') else 'Spotify','preview_source':'Deezer' if (post.get('deezer') or {}).get('preview') else 'Spotify'}
    else:
        has_video = any(str(meta.get('extension') or '').lower() in ('mp4', 'webm', 'mov') for _, meta in post['files'])
        heights = post.get('heights',[] if post.get('original_only') else [360,480,720,1080] if has_video else [])
        has_audio=has_video
    return {'caption': caption, 'title': title, 'language': language, 'has_video': has_video,'has_audio':has_audio,'media_type':'video' if has_video else 'audio' if has_audio else 'images', 'heights': heights,'video_sizes':video_format_sizes(post,heights),'original_only':bool(post.get('original_only')),'source_author':post.get('source_author'),'context_tweet':post.get('context_tweet'),'audio_modes':['audio','mp3'] if has_audio else []}


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
    with contextlib.nullcontext(telegram_http) as client:
        url = f'https://api.telegram.org/bot{token}/{method}'
        response = client.post(url, data={k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in body.items()}, files=files) if files else client.post(url, json=body)
        data = response.json()
    if not data.get('ok'):
        raise TelegramDeliveryError(data.get('error_code',response.status_code))
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


def normalize_video(source, height, directory, index, *, max_duration=600):
    if source.stat().st_size > MAX_VIDEO:
        raise MediaError('This video is too large. Choose a lower resolution.')
    output = Path(directory) / f'normalized-{index}.mp4'
    probe=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,codec_type,width,height','-of','json',str(source)],directory,json_output=True)
    duration = float(probe.get('format', {}).get('duration', 0))
    if not 0 < duration <= max_duration:
        raise MediaError(f'This download needs a valid video duration of up to {max_duration // 60} minutes.')
    streams=probe.get('streams',[])
    video=next((s for s in streams if s.get('codec_type')=='video'),{})
    audio=next((s for s in streams if s.get('codec_type')=='audio'),{})
    width=video.get('width') or 99999
    source_height=video.get('height') or 99999
    limit=height or 1080
    compatible=video.get('codec_name')=='h264' and min(width,source_height)<=limit and audio.get('codec_name','aac') in ('aac','mp3')
    if compatible:
        codec_args=['-c','copy']
    else:
        dimension='iw' if width<source_height else 'ih'
        short_edge=f"'max(2,trunc(min({limit},{dimension})/2)*2)'"
        scale=f'scale={short_edge}:-2' if dimension=='iw' else f'scale=-2:{short_edge}'
        codec_args=['-vf',scale,'-c:v','libx264','-threads','1','-filter_threads','1','-preset','ultrafast','-crf','26','-c:a','aac']
    run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),*codec_args,'-movflags','+faststart','-fs',str(MAX_VIDEO+1),str(output)],directory,timeout=240)
    if output.stat().st_size > MAX_VIDEO:
        raise MediaError('This video is too large. Choose a lower resolution.')
    result=run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(output)],directory,json_output=True)
    output_duration=float(result.get('format',{}).get('duration',0))
    if not math.isfinite(output_duration) or output_duration<=0 or output_duration+0.75<duration:
        raise MediaError('This video could not be prepared in full. Try a lower resolution or a shorter video.')
    return output


def normalize_audio(source, mode, directory, index):
    probe=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,codec_name,bit_rate','-of','json',str(source)],directory,json_output=True)
    if float(probe.get('format',{}).get('duration',601))>600:raise MediaError('This free downloader supports audio up to 10 minutes.')
    stream=next((s for s in probe.get('streams',[]) if s.get('codec_type')=='audio'),None)
    if not stream:raise MediaError('No audio stream was found in this post.')
    bitrate=min(192,max(64,int(stream.get('bit_rate') or 128000)//1000))
    output=Path(directory)/f'converted-audio-{index}.{ "mp3" if mode=="mp3" else "m4a" }'
    compatible=stream.get('codec_name')==('mp3' if mode=='mp3' else 'aac')
    codec=['-c:a','copy'] if compatible else ['-c:a','libmp3lame' if mode=='mp3' else 'aac','-b:a',f'{bitrate}k']
    run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-vn',*codec,'-threads','1','-fs',str(MAX_VIDEO+1),str(output)],directory,timeout=180)
    if output.stat().st_size>MAX_VIDEO:raise MediaError('This audio file is too large to send.')
    return output


def recording_duration_matches(actual, expected):
    try:actual,expected=float(actual or 0),float(expected or 0)
    except (TypeError,ValueError):return False
    return all(math.isfinite(v) and 0<v<=600 for v in (actual,expected)) and abs(actual-expected)<=max(3,expected*.01)


def recording_variant_mismatch(recording, expected_title):
    from urllib.parse import unquote
    normalize=lambda text:' '.join(re.findall(r'\w+',unquote(str(text)).casefold()))
    expected=normalize(expected_title)
    actual=' '.join(normalize(recording.get(key) or '') for key in ('title','track','webpage_url','url'))
    # Titles and URL slugs are strong version signals. Descriptions can mention
    # unrelated recommended versions, so do not reject based on their prose.
    markers=[r'\bslowed(?:down)?\b',r'\b(?:sped(?:up)?|speedup)\b|\bspeed up\b',
             r'\b(?:remix|slowremix)\b',r'\blive\b',r'\bcover\b',r'\bkaraoke\b']
    return any(re.search(marker,actual) and not re.search(marker,expected) for marker in markers)


def spotify_recording(post,directory,official_only=False):
    entity=post['entity'];title=str(entity.get('title') or '');artists=' '.join(a.get('name','') for a in (entity.get('artists') or []))
    query=f'{artists} {title} official audio'.strip();entries=[]
    try:
        data=run(ytdlp_args()+['--flat-playlist','--dump-single-json','--',f'ytsearch5:{query}'],directory,timeout=12,json_output=True)
        entries=data.get('entries',[])
    except (MediaError,ValueError):pass
    if not entries and FALLBACK_BASE:
        try:
            response=source_http.get(FALLBACK_BASE+'/api/v1/search',params={'q':query,'type':'video'},headers={'User-Agent':'Mozilla/5.0','Referer':FALLBACK_BASE+'/'},timeout=5)
            if response.is_success:entries=response.json()[:10]
        except (httpx.HTTPError,ValueError):pass
    normalize=lambda text:' '.join(re.findall(r'\w+',str(text).casefold()))
    expected_duration=float(entity.get('duration') or 0)/1000
    ranked=[]
    for entry in entries:
        candidate=str(entry.get('title') or '');by=str(entry.get('author') or entry.get('channel') or '')
        duration=float(entry.get('lengthSeconds') or entry.get('duration') or 0)
        if not recording_duration_matches(duration,expected_duration) or recording_variant_mismatch(entry,title):continue
        primary=normalize(next(iter(entity.get('artists') or []),{}).get('name',''))
        official_artist=bool(entry.get('channel_is_verified')) and normalize(by)==primary
        if official_only and not official_artist:continue
        artist_match=not primary or primary in normalize(candidate+' '+by)
        if not artist_match:continue
        title_match=(' '+normalize(title)+' ') in (' '+normalize(candidate)+' ')
        if official_only and not title_match:continue
        score=.8 if title_match else .8*SequenceMatcher(None,normalize(title),normalize(candidate)).ratio()
        if primary:score+=.2
        if official_artist:score+=.1
        identifier=entry.get('videoId') or entry.get('id')
        if re.fullmatch(r'[\w-]{11}',identifier or ''):ranked.append((score,identifier))
    if not ranked or max(ranked)[0]<.65:raise MediaError('No confident public recording match was found for this Spotify track. Try its preview or send a direct video link.')
    url='https://www.youtube.com/watch?v='+max(ranked)[1]
    recording=None
    if official_only:
        try:recording=public_audio_post(url,source_http,expected_duration,budget=3)
        except (PublicAudioUnavailable,httpx.HTTPError,ValueError):pass
    if recording is None:recording=dict(post_info(url,directory))
    recording['source_url']=url;recording['expected_duration']=expected_duration
    resolved={**recording.get('info',{}),'webpage_url':url}
    if recording_variant_mismatch(resolved,title) or not recording_duration_matches(resolved.get('duration'),expected_duration):
        raise MediaError('The public source did not match this Spotify recording closely enough. Full audio was not sent.')
    return recording


def native_input(post, url, directory):
    info=post.get('info')
    if info and post.get('engine')=='yt-dlp':
        location=Path(directory)/'inspected-info.json'
        location.write_text(json.dumps(info),encoding='utf8')
        return ['--load-info-json',str(location)]
    return ['--',url]


def spotify_public_recording(post,directory):
    entity=post['entity'];title=str(entity.get('title') or '')
    artists=' '.join(x.get('name','') for x in entity.get('artists') or [])
    expected=float(entity.get('duration') or 0)/1000
    normalize=lambda x:' '.join(re.findall(r'\w+',str(x).casefold()))
    search=run(ytdlp_args()+['--flat-playlist','--dump-single-json','--',f'scsearch5:{artists} {title}'],directory,timeout=20,json_output=True)
    for entry in search.get('entries',[]):
        candidate=normalize(entry.get('title',''));by=normalize(entry.get('uploader',''))
        duration=float(entry.get('duration') or 0)
        if not recording_duration_matches(duration,expected) or recording_variant_mismatch(entry,title):continue
        if (' '+normalize(title)+' ') not in (' '+candidate+' ') or normalize(artists) not in candidate+' '+by:continue
        address=entry.get('webpage_url') or ''
        if urlparse(address).hostname not in ('soundcloud.com','www.soundcloud.com'):continue
        try:
            info=run(ytdlp_args()+['--dump-single-json','--skip-download','--',address],directory,timeout=20,json_output=True)
        except MediaError:continue  # Login-only/DRM recordings are never unlocked.
        if not info.get('formats') or not recording_duration_matches(info.get('duration'),expected) or recording_variant_mismatch({**info,'webpage_url':address},title):continue
        return {'engine':'yt-dlp','info':info,'entries':[info],'source_url':address,'expected_duration':expected}
    raise MediaError('No matching openly playable full recording was found. The Spotify preview remains a separate short-clip option.')


def validate_native_duration(paths, post, native, directory):
    expected=float((post.get('info') or {}).get('duration') or (native.get('info') or {}).get('duration') or post.get('expected_duration') or 0)
    if not math.isfinite(expected) or expected<=0:
        raise MediaError('YouTube did not supply a valid duration. The download was not sent.')
    tolerance=max(3,expected*.03)
    for path in paths:
        probe=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,duration','-of','json',str(path)],directory,json_output=True)
        durations=[probe.get('format',{}).get('duration')]
        durations.extend(s.get('duration') for s in probe.get('streams',[]) if s.get('codec_type') in ('video','audio') and s.get('duration') not in (None,'N/A'))
        for duration in durations:
            try:actual=float(duration)
            except (TypeError,ValueError):actual=0
            if not math.isfinite(actual) or actual<=0 or abs(actual-expected)>tolerance:
                raise MediaError('YouTube supplied an incomplete stream. The download was not sent.')


def proxy_audio_error(error):
    failure=MediaError('The verified recording is temporarily unavailable from the free audio sources.')
    failure.diagnostic={'source':'audio_proxy','reason':error.reason,'http_status':error.status}
    return failure


def acquire_audio(post,job,directory):
    data=None;paths=[]
    if post['engine']=='proxy-audio':
        try:paths=[download_proxy_audio(post['source_url'],Path(directory)/'verified-source.mp3',source_http,job.mode)]
        except ProxyAudioUnavailable as error:raise proxy_audio_error(error) from None
    elif post['engine']=='public-audio':
        target=Path(directory)/'source-audio.m4a'
        try:fetch_file(post['files'][0][0],target,MAX_VIDEO);paths=[target]
        except (MediaError,httpx.HTTPError):
            # A ready audio URL can still be unavailable from this cloud IP.
            # Keep the verified video identity and duration; do not rematch.
            replacement=dict(post_info(post['source_url'],directory))
            replacement.update(source_url=post['source_url'],expected_duration=post['expected_duration'])
            post=replacement
            paths=[p for kind,p in download(post,job.model_copy(update={'mode':'video','url':post['source_url']}),directory) if kind=='video']
    elif post['engine']=='youtube-native':
        data=youtube_native(post.get('source_url') or job.url,directory,job.mode)
        paths=[Path(directory)/data['files'][0]]
    elif post['engine']=='invidious':
        choices=[f for f in post['formats'] if str(f.get('type','')).startswith('audio/') and f.get('url')]
        if not choices:raise MediaError('No audio stream was found for this video.')
        selected=max(choices,key=lambda f:(str(f.get('type','')).startswith('audio/mp4'),int(f.get('bitrate') or 0)))
        target=Path(directory)/('source-audio.m4a' if 'mp4' in selected['type'] else 'source-audio.webm')
        fetch_file(fallback_stream_url(post,selected),target,MAX_VIDEO,{'Referer':FALLBACK_BASE+'/','User-Agent':'Mozilla/5.0'});paths=[target]
    elif post['engine']=='yt-dlp':
        run(ytdlp_args()+['--max-filesize','45M','-f','ba/b','-o',str(Path(directory)/'source-audio-%(autonumber)03d.%(ext)s'),*native_input(post,post.get('source_url') or job.url,directory)],directory,timeout=180)
        paths=sorted(p for p in Path(directory).glob('source-audio-*') if p.suffix in ('.m4a','.mp3','.webm','.mp4','.ogg','.opus'))
    else:
        paths=[path for kind,path in download(post,job.model_copy(update={'mode':'video','url':post.get('source_url') or job.url}),directory) if kind=='video']
    if not paths:raise MediaError('No downloadable audio was found.')
    return paths,post,data


def download_audio(post,job,directory):
    spotify_input=None
    if post['engine']=='spotify':
        if job.mode=='preview':
            url=(post.get('deezer') or {}).get('preview') or (post['entity'].get('audioPreview') or {}).get('url')
            if not url:raise MediaError('Spotify does not provide a preview for this item.')
            target=Path(directory)/'spotify-preview.mp3';fetch_file(url,target,MAX_IMAGE)
            return [('audio',target)]
        spotify_input=post;original=post
        if post.get('deezer'):
            original={**post,'entity':{**post['entity'],'title':post['deezer']['title'],'artists':[{'name':post['deezer']['artist']['name']}],'duration':post['deezer']['duration']*1000}}
        with cache_lock:recording=spotify_input.get('recording')
        if recording and recording[0]>time.time():post=dict(recording[1])
        else:
            try:post=spotify_recording(original,directory,official_only=True)
            except (MediaError,httpx.HTTPError,ValueError):post=spotify_public_recording(original,directory)
    try:paths,post,data=acquire_audio(post,job,directory)
    except (MediaError,httpx.HTTPError):
        source=post.get('source_url')
        if post['engine']=='proxy-audio' or spotify_input is None or not source or not youtube_id(source) or not post.get('expected_duration'):raise
        try:target=download_proxy_audio(source,Path(directory)/'verified-source.mp3',source_http,job.mode)
        except ProxyAudioUnavailable as error:raise proxy_audio_error(error) from None
        paths=[target];post={**post,'engine':'proxy-audio'};data=None
    if post.get('expected_duration'):
        for p in paths:
            probe=run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(p)],directory,json_output=True)
            if not recording_duration_matches(probe.get('format',{}).get('duration'),post['expected_duration']):raise MediaError('The public source only supplied a preview or a different version. Full audio was not sent.')
    normalized=[('audio',normalize_audio(p,job.mode,directory,i)) for i,p in enumerate(paths)]
    if post['engine']=='youtube-native':validate_native_duration([p for _,p in normalized],post,data,directory)
    if spotify_input is not None:
        with cache_lock:spotify_input['recording']=(time.time()+180,dict(post))
    return normalized


def download(post, job, directory):
    if job.mode in ('audio','mp3','preview'):return download_audio(post,job,directory)
    paths = []
    if post['engine']=='youtube-native':
        data=youtube_native(post.get('source_url') or job.url,directory,'video',job.height)
        paths=[Path(directory)/name for name in data['files']]
        if len(paths)==2:
            merged=Path(directory)/'video-merged.mkv'
            run(['ffmpeg','-nostdin','-v','error','-y','-i',str(paths[0]),'-i',str(paths[1]),'-map','0:v:0','-map','1:a:0','-c','copy',str(merged)],directory,timeout=60)
            paths=[merged]
    elif post['engine']=='invidious':
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
        if (urlparse(job.url or '').hostname or '').removeprefix('www.')=='instagram.com':
            # Instagram reports portrait DASH dimensions by their long edge and
            # may omit dimensions entirely on its ready-to-send MP4 variants.
            # Normalize the downloaded file to the requested short edge below.
            portrait = f'bv*[width<={height}][ext=mp4]+ba[ext=m4a]'
            progressive = f'b[ext=mp4][width<=?{height}][height<=?{max(1920,height)}]'
            fmt = f'{progressive}/{fmt}/{portrait}'
        run(ytdlp_args() + ['--max-filesize', '45M', '-f', fmt, '--merge-output-format', 'mp4', '-o', str(Path(directory) / 'video-%(autonumber)03d.%(ext)s'), *native_input(post,job.url,directory)], directory, timeout=240)
        paths = sorted(p for p in Path(directory).glob('video-*') if p.suffix in ('.mp4', '.webm', '.mkv', '.mov'))
    else:
        for i, (url, meta) in enumerate(post['files']):
            if meta.get('variants'):
                choices=[v for v in meta['variants'] if v['height']<=(job.height or 1080)] or meta['variants']
                url=max(choices,key=lambda v:(v['height'],v['bitrate']))['url']
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
            normalized.append(('video', normalize_video(p, job.height, directory, i, max_duration=post.get('max_video_duration', 600))))
        else:
            target = Path(directory) / f'photo-{i}.jpg'
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert('RGB'); im.thumbnail((2560, 2560)); im.save(target, 'JPEG', quality=90)
            normalized.append(('photo', target))
    if post['engine']=='youtube-native':validate_native_duration([p for _,p in normalized],post,data,directory)
    return normalized


def caption_chunks(text):
    parts=[];units=0;start=0
    for char in text:
        width=2 if ord(char)>0xffff else 1
        if units+width>3900:
            yield ''.join(parts),start
            start+=units;parts=[];units=0
        parts.append(char);units+=width
    if parts:yield ''.join(parts),start


def deliver(items, job, cached=False, remote=False):
    ids = [];receipts=[]
    caption = job.caption
    def send(method,payload,files=None):
        try:return telegram(method,payload,files)
        except TelegramDeliveryError as error:
            error.partial=bool(ids);raise
    for index, batch in enumerate(album_batches(items)):
        with contextlib.ExitStack() as stack:
            files, media = {}, []
            for i, (kind, path) in enumerate(batch):
                key = f'file{i}'
                if not (cached or remote):files[key] = (path.name, stack.enter_context(open(path, 'rb')), 'video/mp4' if kind == 'video' else ('audio/mpeg' if path.suffix=='.mp3' else 'audio/mp4') if kind=='audio' else 'image/jpeg')
                item = {'type': kind, 'media': path if cached or remote else f'attach://{key}'}
                if i == 0 and index == 0 and len(caption) <= 1024:
                    item['caption'] = caption
                    if job.caption_entities:item['caption_entities']=job.caption_entities
                if kind == 'video':
                    item['supports_streaming'] = True
                media.append(item)
            if len(batch) > 1:
                sent = send('sendMediaGroup', {**base_body(job), 'media': media}, files)
                ids.extend(m['message_id'] for m in sent)
                for (kind,_),message in zip(batch,sent):
                    value=message.get(kind);value=value[-1] if isinstance(value,list) and value else value
                    if isinstance(value,dict) and value.get('file_id'):receipts.append((kind,value['file_id']))
            else:
                kind, _ = batch[0]
                body = {**base_body(job), kind: media[0]['media'], 'caption': caption if index == 0 and len(caption) <= 1024 else ''}
                if index==0 and len(caption)<=1024 and job.caption_entities:body['caption_entities']=job.caption_entities
                if kind == 'video': body['supports_streaming'] = True
                sent = send({'video':'sendVideo','audio':'sendAudio','photo':'sendPhoto'}[kind], body, files)
                ids.append(sent['message_id'])
                value=sent.get(kind);value=value[-1] if isinstance(value,list) and value else value
                if isinstance(value,dict) and value.get('file_id'):receipts.append((kind,value['file_id']))
    if len(caption) > 1024:
        for chunk,start in caption_chunks(caption):
            # Telegram entity offsets use UTF-16, including supplementary names.
            end=start+len(chunk.encode('utf-16-le'))//2
            entities=[]
            for entity in job.caption_entities:
                lo=max(start,entity['offset']);hi=min(end,entity['offset']+entity['length'])
                if hi>lo:entities.append({**entity,'offset':lo-start,'length':hi-lo})
            sent = send('sendMessage', {**base_body(job), 'text': chunk,**({'entities':entities} if entities else {}), 'link_preview_options': {'is_disabled': True}})
            ids.append(sent['message_id'])
    if job.kind=='download' and len(receipts)==len(items):
        with cache_lock:
            if len(file_cache)>=50:file_cache.pop(next(iter(file_cache)),None)
            file_cache[media_key(job)]=(time.time()+3600,receipts)
    return {'message_ids': ids,'cached':cached,'direct':remote}


def direct_items(post,job):
    if post.get('engine')!='direct' or job.mode not in ('video','images'):return None
    items=[]
    for address,meta in post.get('files') or []:
        if meta.get('variants'):
            choices=[v for v in meta['variants'] if v['height']<=(job.height or 1080)] or meta['variants']
            address=max(choices,key=lambda v:(v['height'],v['bitrate']))['url']
        items.append(('video' if meta.get('extension') in ('mp4','mov','webm') else 'photo',public_url(address)))
    return items or None


def media_key(job):
    url='https://www.youtube.com/watch?v='+youtube_id(job.url) if youtube_id(job.url or '') else job.url
    return (url,job.mode,job.height)


def notify_completion(job):
    # Only our authenticated Worker receives the opaque job ID, never source URLs.
    if job.kind=='verify':return
    target=os.environ.get('MEDIA_CALLBACK_URL','https://group-pulse.group-pulse.workers.dev/media-ready')
    if target!='https://group-pulse.group-pulse.workers.dev/media-ready':return
    secret=os.environ.get('MEDIA_BACKEND_SECRET','')
    try:httpx.post(target,json={'id':job.id},headers={'X-Media-Secret':secret},timeout=3)
    except httpx.HTTPError:pass


def execute(job):
    began=time.monotonic();timings={'queue_ms':round((time.time()-jobs.get(job.id,{}).get('created',time.time()))*1000)}
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
                with cache_lock:receipt=file_cache.get(media_key(job)) if job.kind=='download' else None
                if receipt and receipt[0]>time.time():
                    try:result=deliver(receipt[1],job,cached=True)
                    except MediaError:
                        with cache_lock:file_cache.pop(media_key(job),None)
                        raise
                    with lock:jobs[job.id].update(status='done',result=result)
                    result['timings_ms']={**timings,'total_ms':round((time.monotonic()-began)*1000)}
                    return
                post = post_info(job.url, directory)
                timings['lookup_ms']=round((time.monotonic()-began)*1000)
                if job.kind=='inspect':result=metadata(post)
                elif job.kind=='verify':
                    files=download(post,job,directory)
                    result={'verification_only':True,'metadata':metadata(post),'files':[{'kind':kind,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for kind,p in files]}
                else:
                    result=None;remote=direct_items(post,job)
                    if remote:
                        try:result=deliver(remote,job,remote=True)
                        except TelegramDeliveryError as error:
                            if error.code!=400 or getattr(error,'partial',False):raise
                    if result is None:
                        files=download(post,job,directory);timings['download_ms']=round((time.monotonic()-began)*1000)-timings['lookup_ms']
                        sent_at=time.monotonic();result=deliver(files,job);timings['send_ms']=round((time.monotonic()-sent_at)*1000)
        result['timings_ms']={**timings,'total_ms':round((time.monotonic()-began)*1000)}
        with lock:
            jobs[job.id].update(status='done', result=result)
    except Exception as error:
        # No URLs, bot tokens, source captions or raw library exceptions in responses/logs.
        message = str(error) if isinstance(error, MediaError) else 'Media processing failed. Try again later or choose a smaller file.'
        with lock:
            jobs[job.id].update(status='error', error=message)
            if job.kind=='verify':jobs[job.id]['result']={'verification_only':True,'error_type':type(error).__name__,'diagnostic':getattr(error,'diagnostic',None)}
    finally:
        ready=jobs.get(job.id,{}).get('ready')
        if ready:ready.set()
        notify_completion(job)


@app.get('/health')
def health():
    return {'ok': True, 'release': '2026-11-09-video-integrity'}


@app.post('/jobs')
def submit(job: Request, authorization: str = Header(default='')):
    secret = os.environ.get('MEDIA_BACKEND_SECRET', '')
    if len(secret) < 32 or not hmac.compare_digest(authorization, f'Bearer {secret}'):
        raise HTTPException(401, 'Unauthorized')
    if job.chat_id not in {ALLOWED_CHAT,*ADDITIONAL_ALLOWED_CHATS} and not (job.chat_id>0 and job.user_id==job.chat_id):
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
        ready=threading.Event()
        jobs[job.id] = {'status': 'running', 'created': time.time(), 'fingerprint': fingerprint, 'ready':ready}
        with cache_lock:
            cached=file_cache.get(media_key(job)) if job.kind=='download' else None
            post_key='https://www.youtube.com/watch?v='+youtube_id(job.url) if youtube_id(job.url or '') else job.url
            inspected=post_cache.get(post_key) if job.kind=='inspect' else None
            inspected=bool(inspected and inspected[0]>time.time())
        host=(urlparse(job.url or '').hostname or '').removeprefix('www.')
        direct_source=host in ('x.com','twitter.com','mobile.twitter.com','instagram.com','tiktok.com','m.tiktok.com','vm.tiktok.com','vt.tiktok.com')
        executor=(youtube_pool if youtube_id(job.url or '') else inspect_pool) if job.kind=='inspect' else delivery_pool if cached and cached[0]>time.time() or job.kind=='download' and direct_source and job.mode in ('video','images') else pool
        executor.submit(execute, job)
    # Fast inspections and Telegram sends can finish during the request. Return
    # their receipt immediately instead of forcing a callback/poll round trip.
    # Never hold the jobs lock while waiting for the executor to finish.
    if (job.kind=='inspect' and (inspected or not youtube_id(job.url or ''))) or job.kind=='download' and (cached and cached[0]>time.time() or direct_source and job.mode in ('video','images')):
        if ready.wait(.8):
            with lock:
                current=jobs.get(job.id,{})
                return {k:current[k] for k in ('status','result','error') if k in current}
    return {'status': 'running'}
