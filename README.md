# BB Butler media backend

Authenticated media processing for Bmal Bmale Butler. Built with yt-dlp, gallery-dl, FFmpeg and Pillow. No ads, sponsored posts, required channels or third-party Telegram bots.

Deploy the Dockerfile on a Render **Free** web service. Configure `TELEGRAM_BOT_TOKEN`, a random `MEDIA_BACKEND_SECRET` of at least 32 characters, and `ALLOWED_CHAT_ID`. Never put these values in Git. `/health` is public; `/jobs` requires the shared bearer secret.

The Cloudflare bot queues image conversion and public-post downloads, presents resolution choices, translates captions while preserving Farsi, and polls this service for completion. Requests must target the configured group, or a private chat whose ID matches the authenticated Telegram user ID supplied by the bot. This service sends the requested media directly to Telegram and deletes temporary files when processing finishes. Completed in-memory job receipts expire after one hour. Free Render instances sleep and can restart; a restart loses receipts, so delivery cannot be guaranteed exactly once across host failures.

Limits: 10 MB input images, 45 MB per video, 10-minute videos, 20 media items per post, one active processing thread, five queued/running tasks. Multi-image posts use Telegram albums of up to ten items without leaving a lone final image. Private, login-only, restricted or source-blocked posts may fail; no browser cookies are collected automatically.

Link inspection distinguishes images, videos and audio. Images are delivered automatically; video/audio use requester-only download buttons in the Cloudflare bot. Spotify supports clearly labeled previews and matching public recordings, rather than extracting protected Spotify audio. The configured YouTube fallback receives the requested public video ID; it receives no Telegram user or chat IDs. `YOUTUBE_FALLBACK_URL` can override the default public Invidious instance or disable it with an empty value. Public fallbacks can be unavailable or rate-limited.

Authenticated `verify` jobs download and validate media without sending Telegram messages. They return file sizes and hashes, then delete the temporary files, enabling cloud checks without posting samples.

Test with `python -m unittest -v`. Dependency versions are pinned in `requirements.txt`; update yt-dlp/gallery-dl when source sites change.
