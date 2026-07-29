# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 1000 ticketarr \
 && mkdir -p /config \
 && chown -R ticketarr:ticketarr /config

# tesseract-ocr powers the Regal ticket OCR in parsers/regal.py.
# On Debian slim the eng traineddata is pulled in as a Recommends of
# tesseract-ocr, but we install it explicitly to survive
# --no-install-recommends changes in future base images.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY ticketarr ./ticketarr
RUN pip install --no-deps .

USER ticketarr
VOLUME ["/config"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
                    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["python", "-m", "ticketarr"]
