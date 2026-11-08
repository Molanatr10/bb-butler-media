"""Bounded opportunistic lookup of already processed public YouTube audio."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import ipaddress
import json
import math
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx

API_URL = 'https://prexzyapis.com/download/aio'
AUDIO_HOSTS = frozenset({'cdn-ytb-h2.zm.io.vn', 'cdn-ytb-inter.zm.io.vn', 'cdn-ytb-mega.zm.io.vn'})
MAX_BYTES = 45 * 1024 * 1024
MAX_RESPONSE = 64 * 1024
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='public-audio')
_SLOTS = threading.BoundedSemaphore(2)


class PublicAudioUnavailable(ValueError):
    pass


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise PublicAudioUnavailable('Public audio was not ready within the fast lookup budget.')
    return left


def _video_id(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().removeprefix('www.')
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
            raise ValueError('Invalid source')
        if host == 'youtu.be':
            candidate = parsed.path.strip('/')
        elif host in ('youtube.com', 'm.youtube.com') and parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif host in ('youtube.com', 'm.youtube.com') and re.fullmatch(r'/(?:shorts|live)/[\w-]+/?', parsed.path):
            candidate = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        else:
            candidate = ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate):
            raise ValueError('Invalid video')
        return candidate
    except (TypeError, ValueError):
        raise PublicAudioUnavailable('Use a single public YouTube video link.') from None


def _public_media_url(value, video_id, path):
    try:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        if (parsed.scheme != 'https' or parsed.hostname not in AUDIO_HOSTS
                or parsed.username or parsed.password or parsed.port or parsed.fragment
                or parsed.path != path or query.get('video_id') != [video_id]
                or query.get('ext') != ['m4a'] or query.get('quality') != ['default']):
            raise ValueError('Unsupported address')
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise ValueError('Non-public address')
        return value
    except (OSError, TypeError, ValueError):
        raise PublicAudioUnavailable('The public audio service returned an unsupported address.') from None


def _duration(value, expected):
    try:
        actual = float(value)
        # Both bounds must hold. File duration is checked again by the caller.
        if not math.isfinite(actual) or not 0 < actual <= 600 or abs(actual - expected) > min(3, expected * .01):
            raise ValueError('Different recording duration')
        return actual
    except (TypeError, ValueError, OverflowError):
        raise PublicAudioUnavailable('The public audio did not match the expected recording duration.') from None


def _chunks(response, deadline):
    seen = 0
    for chunk in response.iter_bytes():
        _remaining(deadline)
        seen += len(chunk)
        if seen > MAX_RESPONSE:
            raise PublicAudioUnavailable('The public audio response was too large.')
        yield chunk
    _remaining(deadline)


def _fetch(url, client, expected, deadline):
    video_id = _video_id(url)
    with client.stream('GET', API_URL, params={'url': 'https://www.youtube.com/watch?v=' + video_id},
                       headers={'Accept': 'application/json'}, timeout=httpx.Timeout(_remaining(deadline)),
                       follow_redirects=False) as response:
        if response.status_code != 200:
            raise PublicAudioUnavailable('The free public audio service is unavailable.')
        body = json.loads(b''.join(_chunks(response, deadline)))
    data = body.get('data')
    if body.get('status') is not True or not isinstance(data, dict) or data.get('video_id') != video_id:
        raise PublicAudioUnavailable('The public audio service returned a different video.')
    media = next((m for m in data.get('medias', []) if isinstance(m, dict)
                  and m.get('extension') == 'm4a' and m.get('quality') == 'default'), None)
    if not media:
        raise PublicAudioUnavailable('No audio-only public stream was available.')
    address = _public_media_url(media.get('url'), video_id, '/sse-progress')
    buffer = b''
    event = ''
    fields = []
    with client.stream('GET', address, headers={'Accept': 'text/event-stream'},
                       timeout=httpx.Timeout(_remaining(deadline)), follow_redirects=False) as response:
        if response.status_code != 200 or 'text/event-stream' not in response.headers.get('content-type', ''):
            raise PublicAudioUnavailable('The public audio service did not return processing status.')
        for chunk in _chunks(response, deadline):
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.rstrip(b'\r').decode('utf-8')
                if line.startswith('event:'):
                    event = line.partition(':')[2].strip()
                elif line.startswith('data:'):
                    fields.append(line.partition(':')[2].lstrip())
                elif not line:
                    if fields:
                        state = json.loads('\n'.join(fields))
                        if event == 'error' or state.get('status') in ('error', 'failed'):
                            raise PublicAudioUnavailable('The public audio service could not prepare this recording.')
                        if event == 'completed':
                            info = state.get('file_info') or {}
                            if (state.get('status') != 'success' or info.get('downloaded') is not True
                                    or info.get('video_id') != video_id or info.get('ext') != 'm4a'):
                                raise PublicAudioUnavailable('The public audio was not complete or belonged to another video.')
                            duration = _duration(info.get('duration'), expected)
                            size = int(info.get('size') or 0)
                            if not 0 < size <= MAX_BYTES:
                                raise PublicAudioUnavailable('The public audio file was empty or too large.')
                            target = _public_media_url(state.get('download_url'), video_id, '/download')
                            title = str(info.get('title') or data.get('title') or 'YouTube audio').strip()[:180]
                            _remaining(deadline)
                            return {'engine': 'public-audio', 'files': [(target, {'extension': 'm4a'})],
                                    'info': {'id': video_id, 'title': title, 'description': title, 'duration': duration},
                                    'expected_duration': expected}
                    event, fields = '', []
    raise PublicAudioUnavailable('The public audio was not ready within the fast lookup budget.')


def public_audio_post(youtube_url, client, expected_duration, budget=3.0):
    """Return ready audio within three seconds, otherwise let the caller fall back.

    A bounded executor enforces the caller's absolute deadline even if a server
    slowly trickles bytes. In-flight reads also use the remaining deadline;
    timed-out workers release their slot after closing their response contexts.
    """
    try:
        expected = float(expected_duration)
        seconds = min(3.0, float(budget))
        if not math.isfinite(expected) or not 0 < expected <= 600 or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Invalid bounds')
    except (TypeError, ValueError, OverflowError):
        raise PublicAudioUnavailable('Invalid public audio lookup limits.') from None
    deadline = time.monotonic() + seconds
    _video_id(youtube_url)
    if not _SLOTS.acquire(blocking=False):
        raise PublicAudioUnavailable('The public audio lookup is busy.')
    try:
        future = _POOL.submit(_fetch, youtube_url, client, expected, deadline)
    except Exception:
        _SLOTS.release()
        raise PublicAudioUnavailable('The public audio lookup could not start.') from None
    future.add_done_callback(lambda completed: _SLOTS.release())
    try:
        result = future.result(timeout=_remaining(deadline))
        _remaining(deadline)
        return result
    except (FutureTimeout, PublicAudioUnavailable):
        future.cancel()
        raise PublicAudioUnavailable('Public audio was not ready within the fast lookup budget.') from None
    except Exception:
        raise PublicAudioUnavailable('The public audio service could not retrieve this recording.') from None
