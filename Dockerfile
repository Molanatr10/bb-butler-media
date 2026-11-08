FROM node:24-bookworm-slim AS node
FROM python:3.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py youtube_native.py youtube_public.py youtube_audio_public.py .
RUN useradd --create-home app
USER app
ENV PORT=10000 PYTHONUNBUFFERED=1
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --no-access-log"]
