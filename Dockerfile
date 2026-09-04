FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAC_DEPLOYMENT_MODE=production \
    MAC_ALLOW_CLIENT_API_KEY=true \
    MAC_WEB_HOST=0.0.0.0 \
    MAC_WEB_PORT=8000 \
    MAC_MAX_ACTIVE_JOBS=1

WORKDIR /opt/application

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libgl1 \
        libgomp1 \
        libsm6 \
        libspatialindex6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/application

# Aider 0.82.3 pins NumPy 1.x even though this project and build123d need
# NumPy 2.x. Install Aider's dependency graph first, then upgrade NumPy and
# install the CAD packages. This mirrors the project's verified local setup.
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install "aider-chat==0.82.3" \
    && python -m pip install --upgrade "numpy>=2,<2.3" \
    && python -m pip install ./packages/cadpy \
    && python -m pip install ".[mesh,science,web]" \
    && python -c "import aider, build123d, cadpy, fastapi, trimesh, uvicorn"

RUN chmod +x /opt/application/deploy/run.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["/opt/application/deploy/run.sh"]
