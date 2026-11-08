"""Anonymous WEB/SABR extraction in a bounded subprocess; no account cookies."""
import json
from pathlib import Path
import re
import socket
import sys

MAX_BYTES = 45 * 1024 * 1024


def height(stream):
    return int((stream.resolution or '0p').removesuffix('p'))


def choose_video(streams, limit):
    choices = [s for s in streams if s.includes_video_track and 0 < height(s) <= limit]
    if not choices:
        raise ValueError('Resolution unavailable')
    # Prefer native H.264 at the requested resolution to avoid cloud encoding.
    return max(choices, key=lambda s: (height(s), str(s.video_codec).startswith('avc1'), s.bitrate))


def choose_audio(streams):
    choices = [s for s in streams if s.includes_audio_track and not s.includes_video_track]
    if not choices:
        raise ValueError('Audio unavailable')
    return max(choices, key=lambda s: (bool(getattr(s, 'is_default_audio_track', False)), s.subtype in ('mp4', 'm4a'), s.bitrate))


def execute(action, video_id, mode='video', limit=720):
    if not re.fullmatch(r'[\w-]{11}', video_id) or action not in ('inspect', 'download'):
        raise ValueError('Invalid request')
    from pytubefix import YouTube
    socket.setdefaulttimeout(15)

    received = {}
    def bounded_progress(stream, chunk, remaining):
        received[stream.itag] = received.get(stream.itag, 0) + len(chunk)
        if received[stream.itag] > MAX_BYTES or sum(received.values()) > 2 * MAX_BYTES:
            raise ValueError('File too large')

    yt = YouTube('https://www.youtube.com/watch?v=' + video_id, client='WEB',
                 use_oauth=False, allow_oauth_cache=False, on_progress_callback=bounded_progress)
    streams = list(yt.streams)
    if yt.length > 600 or yt.vid_info.get('videoDetails', {}).get('isLiveContent'):
        raise ValueError('Use a non-live video up to ten minutes')
    info = {'id': yt.video_id, 'title': yt.title, 'description': yt.description or '', 'duration': yt.length}
    if action == 'inspect':
        return {'info': info, 'heights': sorted({height(s) for s in streams if s.includes_video_track and 144 <= height(s) <= 1080}),
                'has_audio': any(s.includes_audio_track for s in streams)}
    chosen = [choose_audio(streams)] if mode in ('audio', 'mp3') else [choose_video(streams, limit)]
    if mode == 'video' and not chosen[0].includes_audio_track:
        chosen.append(choose_audio(streams))
    files = []
    for i, stream in enumerate(chosen):
        ext = 'm4a' if not stream.includes_video_track and stream.subtype == 'mp4' else stream.subtype
        if ext not in ('mp4', 'm4a', 'webm') or stream.filesize_approx > MAX_BYTES:
            raise ValueError('Unsupported or oversized stream')
        filename = f'native-{i}.{ext}'
        stream.download(output_path='.', filename=filename, skip_existing=False, timeout=20, max_retries=0)
        if not 0 < Path(filename).stat().st_size <= MAX_BYTES:
            raise ValueError('Empty or oversized file')
        files.append(filename)
    return {'info': info, 'files': files}


if __name__ == '__main__':
    try:
        print(json.dumps(execute(*sys.argv[1:4], int(sys.argv[4]) if len(sys.argv) > 4 else 720)))
    except Exception as error:
        # Never log playback URLs, ephemeral tokens, captions or raw exceptions.
        print('YouTube anonymous WEB download failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
