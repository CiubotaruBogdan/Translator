#!/bin/bash
set -e

echo "============================================================"
echo "  Traducator Offline - Container Starting"
echo "============================================================"

# -- Auto-detect host IP for Ollama --
echo ""
echo "[0/3] Detecting host IP for Ollama connection..."

# If OLLAMA_URL is already set explicitly, use it
if [ -n "$OLLAMA_URL" ] && [ "$OLLAMA_URL" != "auto" ]; then
    echo "      OLLAMA_URL set explicitly: $OLLAMA_URL"
else
    # Try multiple methods to find the host IP
    OLLAMA_FOUND=0
    OLLAMA_PORT=${OLLAMA_PORT:-11434}

    # Method 1: Try host.containers.internal
    echo "      Trying host.containers.internal:${OLLAMA_PORT}..."
    if curl -s --connect-timeout 3 "http://host.containers.internal:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
        export OLLAMA_URL="http://host.containers.internal:${OLLAMA_PORT}"
        OLLAMA_FOUND=1
        echo "      OK! Ollama found at host.containers.internal"
    fi

    # Method 2: Try host.docker.internal
    if [ $OLLAMA_FOUND -eq 0 ]; then
        echo "      Trying host.docker.internal:${OLLAMA_PORT}..."
        if curl -s --connect-timeout 3 "http://host.docker.internal:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
            export OLLAMA_URL="http://host.docker.internal:${OLLAMA_PORT}"
            OLLAMA_FOUND=1
            echo "      OK! Ollama found at host.docker.internal"
        fi
    fi

    # Method 3: Try default gateway IP
    if [ $OLLAMA_FOUND -eq 0 ]; then
        GATEWAY_IP=$(ip route | grep default | awk '{print $3}' 2>/dev/null || echo "")
        if [ -n "$GATEWAY_IP" ]; then
            echo "      Trying gateway ${GATEWAY_IP}:${OLLAMA_PORT}..."
            if curl -s --connect-timeout 3 "http://${GATEWAY_IP}:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
                export OLLAMA_URL="http://${GATEWAY_IP}:${OLLAMA_PORT}"
                OLLAMA_FOUND=1
                echo "      OK! Ollama found at gateway ${GATEWAY_IP}"
            fi
        fi
    fi

    # Method 4: Try host.wsl.internal (Windows WSL specific)
    if [ $OLLAMA_FOUND -eq 0 ]; then
        echo "      Trying host.wsl.internal:${OLLAMA_PORT}..."
        if curl -s --connect-timeout 3 "http://host.wsl.internal:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
            export OLLAMA_URL="http://host.wsl.internal:${OLLAMA_PORT}"
            OLLAMA_FOUND=1
            echo "      OK! Ollama found at host.wsl.internal"
        fi
    fi

    # Method 5: Try common local IPs
    if [ $OLLAMA_FOUND -eq 0 ]; then
        for IP in 172.17.0.1 10.0.2.2 192.168.65.2; do
            echo "      Trying ${IP}:${OLLAMA_PORT}..."
            if curl -s --connect-timeout 2 "http://${IP}:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
                export OLLAMA_URL="http://${IP}:${OLLAMA_PORT}"
                OLLAMA_FOUND=1
                echo "      OK! Ollama found at ${IP}"
                break
            fi
        done
    fi

    if [ $OLLAMA_FOUND -eq 0 ]; then
        export OLLAMA_URL="http://host.containers.internal:${OLLAMA_PORT}"
        echo "      WARNING: Ollama not detected automatically."
        echo "      Default: $OLLAMA_URL"
        echo "      You can configure Ollama URL from the web interface Settings page."
    fi
fi

echo "      Final OLLAMA_URL: $OLLAMA_URL"

# -- Network diagnostics --
echo ""
echo "[1/3] Network diagnostics:"
echo "      Container IP:  $(hostname -I 2>/dev/null || echo 'unknown')"
echo "      Gateway:       $(ip route | grep default | awk '{print $3}' 2>/dev/null || echo 'unknown')"
echo "      DNS:           $(cat /etc/resolv.conf 2>/dev/null | grep nameserver | head -1 | awk '{print $2}' || echo 'unknown')"

# Ping tests
echo "      Ping tests:"
for HOST in host.containers.internal host.docker.internal host.wsl.internal; do
    if ping -c 1 -W 2 "$HOST" > /dev/null 2>&1; then
        RESOLVED_IP=$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' || echo "?")
        echo "        $HOST -> OK (${RESOLVED_IP})"
    else
        echo "        $HOST -> UNREACHABLE"
    fi
done

GATEWAY_IP=$(ip route | grep default | awk '{print $3}' 2>/dev/null || echo "")
if [ -n "$GATEWAY_IP" ]; then
    if ping -c 1 -W 2 "$GATEWAY_IP" > /dev/null 2>&1; then
        echo "        gateway ($GATEWAY_IP) -> OK"
    else
        echo "        gateway ($GATEWAY_IP) -> UNREACHABLE"
    fi
fi

# -- Start LibreTranslate in background --
echo ""
echo "[2/3] Starting LibreTranslate on port 5000..."
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
echo ""
echo "[3/3] Starting web server on port $PORT..."
echo "============================================================"
echo "  Web UI:         http://0.0.0.0:$PORT"
echo "  LibreTranslate: http://127.0.0.1:5000 (internal)"
echo "  Ollama:         $OLLAMA_URL"
echo "  Cleanup:        daily at midnight (automatic)"
echo "============================================================"

cd /app
exec python3 server.py
