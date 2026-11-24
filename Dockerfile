# Cloud Run Job: FFmpeg/rclone process one Drive audio at a time.
FROM rclone/rclone:1.74.0 AS rclone
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
RUN uv sync --locked --extra cloud --no-dev --no-editable --python /usr/local/bin/python \
    && groupadd -r app && useradd -r -g app app
ENV PATH="/opt/venv/bin:$PATH"
USER app
ENTRYPOINT ["python", "-m", "scribeflow.cloud_entry"]
