FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir . && rm -rf /root/.cache
USER 99:100
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"]
# First start imports the audio pack's index (about a minute); later starts reuse it.
CMD ["uvicorn", "yobikoe.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1", "--no-access-log"]
