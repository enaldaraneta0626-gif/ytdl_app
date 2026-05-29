FROM python:3.12-slim

# ffmpeg is required by yt-dlp for merging video+audio and extracting mp3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render provides $PORT at runtime. Single worker: download jobs and files
# are held in-process, so polling must hit the same worker.
ENV PORT=10000
CMD gunicorn app:app --workers 1 --timeout 300 --bind 0.0.0.0:$PORT
