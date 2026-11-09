"""Free, anonymous public YouTube fallback. No account or bot credentials."""
import ipaddress
import math
import re
import socket
from urllib.parse import parse_qs, urlparse

API_URL = 'https://api.omegatech.app/api/download/YouTube'
MAX_BYTES = 45 * 1024 * 1024
MAX_DURATION = 1800


class PublicYouTubeError(ValueError):
    pass


class PublicYouTubeLimitError(PublicYouTubeError):
    pass


def _video_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower().removeprefix('www.')
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
        raise PublicYouTubeError('Use a public YouTube video link.')
    if host == 'youtu.be':
        candidate = parsed.path.strip('/')
    elif host in ('youtube.com', 'm.youtube.com'):
        if parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif re.fullmatch(r'/(?:shorts|live)/[\w-]+/?', parsed.path):
            candidate = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        else:
            candidate = ''
    else:
        candidate = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate):
        raise PublicYouTubeError('Use a single public YouTube video link.')
    return candidate


def _playback_url(value):
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or not host.endswith('.googlevideo.com') or parsed.path != '/videoplayback'
            or parsed.fragment):
        raise PublicYouTubeError('The public YouTube service returned an unsupported media address.')
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise ValueError('Non-public address')
    except (OSError, ValueError):
        raise PublicYouTubeError('The public YouTube media address is unavailable.') from None
    return value, parse_qs(parsed.query)


def _bounded_number(values, maximum, label):
    try:
        numbers = [float(value) for value in values if value is not None]
        if not numbers or any(not math.isfinite(value) or value <= 0 for value in numbers):
            raise ValueError('Invalid bounds')
    except (TypeError, ValueError, OverflowError):
        raise PublicYouTubeError(f'The public YouTube service returned an invalid {label}.') from None
    largest = max(numbers)
    if largest > maximum:
        limit = f'{maximum // 60} minutes' if label == 'duration' else f'{maximum // (1024 * 1024)} MiB'
        raise PublicYouTubeLimitError(f'The available YouTube download exceeds the {limit} limit.')
    return largest


def omega_post(url, client):
    video_id = _video_id(url)
    canonical = 'https://www.youtube.com/watch?v=' + video_id
    response = client.get(API_URL, params={'action': 'download', 'url': canonical},
                          timeout=25, follow_redirects=False)
    if response.status_code != 200:
        raise PublicYouTubeError('The free public YouTube service is temporarily unavailable.')
    try:
        body = response.json()
        data = body.get('data')
        if body.get('success') is not True or not isinstance(data, dict):
            raise ValueError('No media')
        title = data.get('title')
        if not isinstance(title, str) or not title.strip() or data.get('type') != 'mp4':
            raise ValueError('Invalid media')
        address, query = _playback_url(data.get('url') or '')
        if query.get('mime') != ['video/mp4']:
            raise ValueError('Not an MP4 video')
        duration = _bounded_number([data.get('duration'), *query.get('dur', [])], MAX_DURATION, 'duration')
        _bounded_number([data.get('size'), *query.get('clen', [])], MAX_BYTES, 'file')
    except PublicYouTubeError:
        raise
    except (TypeError, ValueError, AttributeError):
        raise PublicYouTubeError('The free public YouTube service could not retrieve this video.') from None
    # Its data.id is a conversion job identifier, not the requested YouTube ID.
    # This endpoint returns one original stream; do not invent a resolution.
    title = title.strip()[:180]
    return {'engine': 'direct', 'files': [(address, {'extension': 'mp4'})],
            'original_only': True, 'max_video_duration': MAX_DURATION,
            'info': {'id': video_id, 'title': title, 'description': title, 'duration': duration}}
