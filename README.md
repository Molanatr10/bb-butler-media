## Public YouTube fallback candidate

YouTube first tries OmegaTech's documented public no-key API (https://api.omegatech.app/docs), then the existing anonymous native and Invidious routes. Only bounded public Google video HTTPS addresses are accepted. The public API currently supplies one original MP4 stream, so it auto-sends without inventing resolution choices; MP3/M4A use the existing guarded audio conversion. The supplied Short was verified locally as a complete 360x640 H.264/AAC video; cloud verification is required before calling it working there. No account key, card or payment is used for this route. Public-provider availability remains best-effort.

# BB Butler media backend

Authenticated media processing for Bmal Bmale Butler. Built with yt-dlp, gallery-dl, FFmpeg and Pillow. No ads, sponsored posts, required channels or third-party Telegram bots.

Deploy the Dockerfile on a Render **Free** web service. Configure `TELEGRAM_BOT_TOKEN`, a random `MEDIA_BACKEND_SECRET` of at least 32 characters, and `ALLOWED_CHAT_ID`. Never put these values in Git. `/health` is public; `/jobs` requires the shared bearer secret.

The Cloudflare bot queues image conversion and public-post downloads, presents resolution choices, translates captions while preserving Farsi, and polls this service for completion. Requests must target the configured group, or a private chat whose ID matches the authenticated Telegram user ID supplied by the bot. This service sends the requested media directly to Telegram and deletes temporary files when processing finishes. Completed in-memory job receipts expire after one hour. Free Render instances sleep and can restart; a restart loses receipts, so delivery cannot be guaranteed exactly once across host failures.

Limits: 10 MB input images, 45 MB per video, 10-minute videos, 20 media items per post, one active processing thread, five queued/running tasks. Multi-image posts use Telegram albums of up to ten items without leaving a lone final image. Private, login-only, restricted or source-blocked posts may fail; no browser cookies are collected automatically.

Link inspection distinguishes images, videos and audio. Images are delivered automatically; video/audio use requester-only download buttons in the Cloudflare bot. Spotify supports clearly labeled previews and matching public recordings, rather than extracting protected Spotify audio. The configured YouTube fallback receives the requested public video ID; it receives no Telegram user or chat IDs. `YOUTUBE_FALLBACK_URL` can override the default public Invidious instance or disable it with an empty value. Public fallbacks can be unavailable or rate-limited.

Authenticated `verify` jobs download and validate media without sending Telegram messages. They return file sizes and hashes, then delete the temporary files, enabling cloud checks without posting samples.

Test with `python -m unittest -v`. Dependency versions are pinned in `requirements.txt`; update yt-dlp/gallery-dl when source sites change.


Latency improvements: metadata runs in a separate bounded executor; inspected source information is cached for ten minutes and reused at download time. Successfully sent public media can reuse Telegram file IDs for one hour, avoiding another download/upload. Receipts are bounded and ephemeral. Authenticated completion callbacks notify the Cloudflare Worker directly; scheduled polling remains recovery. No login cookies are imported.

Public source helpers: Twitter syndication provides text-only posts, images and video variants; Instagram captioned embeds provide anonymously exposed media; TikWM provides a free fallback for TikTok. These helpers receive only the requested public post URL/ID, never Telegram credentials or member IDs. Spotify full audio matches an openly playable public recording on SoundCloud or YouTube and checks artist/title/duration; protected recordings and preview-only matches are rejected. YouTube availability remains dependent on anonymous origin/fallback access.


For eligible public video/image posts, delivery first asks Telegram to fetch the public media URL directly, avoiding server downloads and encoding. Successful sends populate the Telegram file-ID cache. Only an unambiguous HTTP 400 failure before any items were sent falls back to local processing; partial albums and uncertain network outcomes are never replayed automatically. TikTok uses its standard public MP4 for lower transfer latency. Tweet author names link to the original tweet above its text or caption.


Spotify matching also consults Deezer’s public catalog, requiring matching artist/title and a close duration before accepting a catalog record. Deezer previews are labeled as short clips; catalog metadata does not provide full-track audio or access to @DeezerMusicBot’s private library. No Deezer account credentials or DRM-unlocking code are used.

YouTube also tries anonymous WEB/SABR extraction using pinned pytubefix 11.2.1 in a bounded subprocess, with video/audio merging and existing size/duration limits. It uses no user cookies or OAuth. YouTube inspections have their own queue; social direct deliveries and cached resends have a separate pool so long YouTube tasks do not occupy their workers. Metadata and Telegram requests reuse TLS connections. Cloud source acceptance remains dependent on YouTube; local success alone is not a cloud-delivery guarantee.

Validation on 8 Nov 2026: exact Short QyS1O40wvFI downloaded locally as valid 360p H.264/AAC and MP3 through WEB/SABR, but Hostless's IP still receives YouTube's sign-in block. This route is not claimed to fix every cloud download. Final native output duration is checked against inspected metadata, including audio/video stream durations, to reject truncated files. Uncached social audio conversions remain on the regular download pool.
