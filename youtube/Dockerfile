FROM python:3.13-slim
# ffmpeg — для yt-dlp (вынуть звук из контейнера webm без перекодирования)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --root-user-action=ignore yt-dlp ytmusicapi mutagen
COPY fallback.py /app/fallback.py
CMD ["python", "-u", "/app/fallback.py", "--loop"]
