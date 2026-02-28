# ============================================================
# Traducator Offline - Container Image
# Server web + LibreTranslate cu modele de limbă selectabile
# ============================================================
# Build (default: en,ro):
#   podman build -t traducator-offline .
#
# Build with extra languages:
#   podman build -t traducator-offline --build-arg LANGUAGES=en,ro,fr,de,es .
#
# Export: podman save traducator-offline -o traducator-offline.tar
# Run:    podman run -d -p 8080:8080 traducator-offline
# ============================================================

FROM python:3.11-slim

LABEL maintainer="Traducator Offline"
LABEL description="Offline document translator with LibreTranslate + Web UI"

# -- Build argument: comma-separated language codes --
# English (en) and Romanian (ro) are always included
# Available: sq,ar,az,eu,bn,bg,ca,zt,zh,cs,da,nl,en,eo,et,fi,fr,gl,de,el,
#            he,hi,hu,id,ga,it,ja,ko,ky,lv,lt,ms,nb,fa,pl,pt,pb,ro,ru,
#            sk,sl,es,sv,tl,th,tr,uk,ur,vi
ARG LANGUAGES=en,ro

# -- System dependencies (Tesseract OCR + poppler for PDF + LibreOffice headless + network tools) --
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    iproute2 \
    netcat-openbsd \
    dnsutils \
    tesseract-ocr \
    tesseract-ocr-ron \
    tesseract-ocr-fra \
    tesseract-ocr-eng \
    poppler-utils \
    libreoffice-core \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# -- Python dependencies for web server --
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    python-docx \
    pdfplumber \
    aiohttp \
    psutil \
    websockets \
    pdf2docx \
    pdf2image \
    pytesseract \
    Pillow

# -- LibreTranslate + Argos Translate --
RUN pip install --no-cache-dir libretranslate

# -- Download language models based on LANGUAGES build arg --
COPY install_languages.py /tmp/install_languages.py
RUN python3 /tmp/install_languages.py "${LANGUAGES}" && rm /tmp/install_languages.py

# -- Copy application --
COPY app/ /app/

# -- Create working directories --
RUN mkdir -p /app/uploads /app/outputs

# -- Copy entrypoint (fix Windows CRLF line endings if present) --
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# -- Environment variables --
ENV PORT=8080
ENV OLLAMA_URL=auto
ENV OLLAMA_PORT=11434
ENV LIBRETRANSLATE_URL=http://127.0.0.1:5000
ENV TRANSLATE_ENGINE=libretranslate
ENV INSTALLED_LANGUAGES=${LANGUAGES}

# -- Expose port --
EXPOSE 8080

# -- Health check --
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/status || exit 1

# -- Entrypoint --
ENTRYPOINT ["/entrypoint.sh"]
