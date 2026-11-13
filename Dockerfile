# Cloud Run Job: FFmpeg/rclone process one Drive audio at a time.
FROM rclone/rclone:1.74.0 AS rclone

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[cloud]' && groupadd -r app && useradd -r -g app app
USER app
ENTRYPOINT ["python", "-m", "scribeflow.cloud_entry"]
