# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG VERSION=0.2.0
ARG GIT_COMMIT=unknown
ARG SOURCE_URL=https://github.com/Query-farm/vgi-nba

LABEL org.opencontainers.image.title="vgi-nba" \
      org.opencontainers.image.description="Read-only NBA, WNBA, and G League statistics connector for VGI" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system vgi && useradd --system --gid vgi --home-dir /app vgi
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY vgi_nba ./vgi_nba
RUN python -m pip install --no-cache-dir .

USER vgi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=8s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["vgi-nba-http", "--host", "0.0.0.0", "--port", "8000", "--log-format", "json"]
