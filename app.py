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
from urllib.parse import urlparse, parse_qs

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


class MediaError(Exception):
    pass


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    kind: str = Field(pattern=r'^(inspect|download|sticker)$')
    chat_id: int
    user_id: int | None = None
    message_id: int = Field(gt=0)
    topic: int = Field(default=0, ge=0)
    url: str | None = Field(default=None, max_length=2048)
    height: int | None = Field(default=None, ge=144, le=1080)
    file_id: str | None = Field(default=None, max_length=512)
    caption: str = Field(default='', max_length=10000)
    language: str | None = Field(default=None, max_length=16)


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
    }
    if u.scheme != 'https' or u.username or u.password or u.port or not re.fullmatch(paths.get(host, r'(?!)'), u.path):
        raise MediaError('Use a public TikTok, YouTube, X/Twitter or Instagram post link.')
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


def post_info(url, directory):
    validate_source(url)
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
        formats = [f for e in entries for f in e.get('formats', []) if f.get('vcodec') != 'none']
        heights = sorted({int(f['height']) for f in formats if f.get('height') and 144 <= f['height'] <= 1080})
        if not heights:
            heights = [min(1080, int(entries[0].get('height') or 720))]
        has_video = True
    else:
        has_video = any(str(meta.get('extension') or '').lower() in ('mp4', 'webm', 'mov') for _, meta in post['files'])
        heights = [360, 480, 720, 1080] if has_video else []
    return {'caption': caption, 'title': title, 'language': language, 'has_video': has_video, 'heights': heights}


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


def download(post, job, directory):
    paths = []
    if post['engine'] == 'yt-dlp':
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
        if p.suffix in ('.mp4', '.webm', '.mkv', '.mov'):
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
                files[key] = (path.name, stack.enter_context(open(path, 'rb')), 'video/mp4' if kind == 'video' else 'image/jpeg')
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
                sent = telegram('sendVideo' if kind == 'video' else 'sendPhoto', body, files)
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
                result = metadata(post) if job.kind == 'inspect' else deliver(download(post, job, directory), job)
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
