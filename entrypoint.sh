#!/bin/bash
set -e

echo "============================================================"
echo "  Traducator Offline - Container Starting"
echo "============================================================"

# -- Start LibreTranslate in background --
echo "[1/2] Starting LibreTranslate on port 5000..."
libretranslate --host 127.0.0.1 --port 5000 --load-only en,ro,fr --threads 4 --disable-web-ui &
LIBRE_PID=$!

# Wait for LibreTranslate to be ready
echo "      Waiting for LibreTranslate to initialize..."
for i in $(seq 1 60); do
    if curl -s http://127.0.0.1:5000/languages > /dev/null 2>&1; then
        echo "      LibreTranslate is ready!"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "      WARNING: LibreTranslate did not start within 60s, continuing anyway..."
    fi
    sleep 1
done

# -- Start web server --
PORT=${PORT:-8080}
echo "[2/2] Starting web server on port $PORT..."
echo "============================================================"
echo "  Web UI:         http://0.0.0.0:$PORT"
echo "  LibreTranslate: http://127.0.0.1:5000 (internal)"
echo "  Ollama:         configure via OLLAMA_URL env var"
echo "  Cleanup:        daily at midnight (automatic)"
echo "============================================================"

cd /app
exec python3 server.py
