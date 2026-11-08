"""Bounded anonymous audio conversion for an already verified YouTube source."""
from pathlib import Path
import socket
import threading
import time

import httpx

from youtube_public import _video_id, PublicYouTubeError

API_URL = 'https://save.brianrianrehan.com/api/v1/download'
MAX_BYTES = 45 * 1024 * 1024
TOTAL_SECONDS = 60


class ProxyAudioUnavailable(ValueError):
    def __init__(self,message,reason='unavailable',status=None):
        super().__init__(message);self.reason=reason;self.status=status


def download_proxy_audio(source_url, target_path, client, mode='mp3'):
    """Return audio bytes only; caller validates actual recording duration."""
    try:
        video_id = _video_id(source_url)
        if mode not in ('mp3', 'audio'):
            raise ValueError('Unsupported audio mode')
    except (PublicYouTubeError, TypeError, ValueError):
        raise ProxyAudioUnavailable('Use one verified public YouTube audio source.') from None
    target = Path(target_path)
    partial = target.with_name(target.name + '.part')
    started = time.monotonic()
    expired = threading.Event()
    active = []
    connections = []

    def trace(event, details):
        if event in ('connection.connect_tcp.complete', 'connection.start_tls.complete'):
            network = details.get('return_value')
            try:
                connection = network.get_extra_info('socket') if network else None
                if connection:
                    connections.append(connection)
            except (AttributeError, OSError, RuntimeError):
                pass

    def stop():
        expired.set()
        # Capture new connections before headers exist, so a provider trickling
        # response headers cannot keep the download worker alive indefinitely.
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError, RuntimeError):
                pass
        if active:
            response = active[0]
            # HTTP/1.1 uses one request per socket. Shutdown interrupts a stalled
            # body read instead of allowing trickled bytes to extend the job.
            try:
                network = response.extensions.get('network_stream')
                connection = network.get_extra_info('socket') if network else None
                if connection:
                    connection.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError, RuntimeError):
                pass
            try:
                response.close()
            except Exception:
                pass

    timer = threading.Timer(TOTAL_SECONDS, stop)
    timer.daemon = True
    timer.start()

    def check_deadline():
        if expired.is_set() or time.monotonic() - started >= TOTAL_SECONDS:
            raise ProxyAudioUnavailable('The free audio fallback took too long.')

    try:
        # Phase bounds total at most 60 seconds before response headers; the
        # watchdog also bounds the entire streamed transfer, including trickles.
        timeout = httpx.Timeout(45, connect=8, write=4, pool=3)
        with client.stream('POST', API_URL,
                           params={'wait': 'true', 'download': 'true'},
                           json={'url': 'https://www.youtube.com/watch?v=' + video_id,
                                 'format': 'audio', 'quality': 'best', 'outputFormat': 'mp3'},
                           headers={'Accept': 'audio/*, application/octet-stream', 'Accept-Encoding': 'identity', 'Connection': 'close',
                                    'User-Agent':'Mozilla/5.0','Referer':'https://save.brianrianrehan.com/','Origin':'https://save.brianrianrehan.com'},
                           timeout=timeout, follow_redirects=False, extensions={'trace': trace}, auth=None) as response:
            active.append(response)
            check_deadline()
            mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if response.status_code != 200 or not (mime.startswith('audio/') or mime == 'application/octet-stream'):
                raise ProxyAudioUnavailable('The free audio fallback did not return an audio file.','response',response.status_code)
            encoding = response.headers.get('content-encoding', 'identity').lower()
            if encoding != 'identity':
                raise ProxyAudioUnavailable('The free audio fallback returned an unsupported encoding.')
            declared = response.headers.get('content-length')
            expected = int(declared) if declared is not None else None
            if expected is not None and not 0 < expected <= MAX_BYTES:
                raise ProxyAudioUnavailable('The free audio fallback returned an empty or oversized file.')
            size = 0
            with partial.open('wb') as output:
                # No fixed chunk size: inspect every network chunk immediately.
                for chunk in response.iter_raw():
                    check_deadline()
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ProxyAudioUnavailable('The free audio fallback file is too large.')
                    output.write(chunk)
            check_deadline()
            if not size or expected is not None and size != expected:
                raise ProxyAudioUnavailable('The free audio fallback returned an incomplete file.')
        check_deadline()
        partial.replace(target)
        return target
    except ProxyAudioUnavailable:
        raise
    except Exception as error:
        raise ProxyAudioUnavailable('The free audio fallback is temporarily unavailable.',type(error).__name__) from None
    finally:
        timer.cancel()
        active.clear()
        connections.clear()
        partial.unlink(missing_ok=True)
