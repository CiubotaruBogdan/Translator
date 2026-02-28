# ============================================================
# Traducator Offline - Container Image
# Server web + LibreTranslate cu modele ro/en/fr
# ============================================================
# Build:  podman build -t traducator-offline .
# Export: podman save traducator-offline -o traducator-offline.tar
# Run:    podman run -d -p 8080:8080 traducator-offline
# ============================================================

FROM python:3.11-slim

LABEL maintainer="Traducator Offline"
LABEL description="Offline document translator with LibreTranslate + Web UI"

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

# -- Download language models: Romanian, English, French --
COPY install_languages.py /tmp/install_languages.py
RUN python3 /tmp/install_languages.py && rm /tmp/install_languages.py

# -- Copy application --
COPY app/ /app/

# -- Create working directories --
RUN mkdir -p /app/uploads /app/outputs

# -- Copy entrypoint --
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# -- Environment variables --
ENV PORT=8080
ENV OLLAMA_URL=auto
ENV OLLAMA_PORT=11434
ENV LIBRETRANSLATE_URL=http://127.0.0.1:5000
ENV TRANSLATE_ENGINE=libretranslate

# -- Expose port --
EXPOSE 8080

# -- Health check --
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/status || exit 1

# -- Entrypoint --
ENTRYPOINT ["/entrypoint.sh"]
