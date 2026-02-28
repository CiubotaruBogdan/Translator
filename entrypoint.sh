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
    OLLAMA_FOUND=0
    OLLAMA_PORT=${OLLAMA_PORT:-11434}

    # Helper function: test if Ollama responds at given IP
    try_ollama() {
        local IP="$1"
        local LABEL="$2"
        echo "      Trying ${LABEL} (${IP}:${OLLAMA_PORT})..."
        if curl -s --connect-timeout 3 "http://${IP}:${OLLAMA_PORT}/api/version" > /dev/null 2>&1; then
            export OLLAMA_URL="http://${IP}:${OLLAMA_PORT}"
            OLLAMA_FOUND=1
            echo "      >>> OK! Ollama found at ${LABEL} (${IP})"
            return 0
        fi
        return 1
    }

    # Method 1: Try host.containers.internal (Podman standard)
    try_ollama "host.containers.internal" "host.containers.internal" 2>/dev/null || true

    # Method 2: Try host.docker.internal (Docker compatible)
    [ $OLLAMA_FOUND -eq 0 ] && try_ollama "host.docker.internal" "host.docker.internal" 2>/dev/null || true

    # Method 3: Try default gateway
    if [ $OLLAMA_FOUND -eq 0 ]; then
        GATEWAY_IP=$(ip route | grep default | awk '{print $3}' 2>/dev/null || echo "")
        [ -n "$GATEWAY_IP" ] && try_ollama "$GATEWAY_IP" "gateway" 2>/dev/null || true
    fi

    # Method 4: Resolve host machine's real IPs via hostname from /etc/hosts
    # Podman writes the host's hostname into /etc/hosts (e.g., "10.88.0.1 host.containers.internal")
    # but also the container's own hostname which maps to the host machine name
    if [ $OLLAMA_FOUND -eq 0 ]; then
        echo "      Trying to discover host IPs via DNS/hostname..."
        # Extract the host machine's FQDN from /etc/hosts (second line with a hostname, not localhost)
        HOST_FQDN=$(awk '!/^#/ && !/localhost/ && !/host\.containers/ && !/host\.docker/ && !/ip6/ && NF>=2 {print $2; exit}' /etc/hosts 2>/dev/null || echo "")

        if [ -n "$HOST_FQDN" ]; then
            echo "      Host FQDN detected: ${HOST_FQDN}"
            # Use nslookup to get all IPv4 addresses for this hostname
            HOST_IPS=$(nslookup "$HOST_FQDN" 2>/dev/null | awk '/^Address:/ && !/#/ {print $2}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || echo "")

            if [ -z "$HOST_IPS" ]; then
                # Fallback: try dig
                HOST_IPS=$(dig +short "$HOST_FQDN" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || echo "")
            fi

            for IP in $HOST_IPS; do
                [ $OLLAMA_FOUND -eq 1 ] && break
                try_ollama "$IP" "host-dns:${HOST_FQDN}" 2>/dev/null || true
            done
        fi
    fi

    # Method 5: Try host.wsl.internal (Windows WSL)
    [ $OLLAMA_FOUND -eq 0 ] && try_ollama "host.wsl.internal" "host.wsl.internal" 2>/dev/null || true

    # Method 6: Try common Docker/VM bridge IPs
    if [ $OLLAMA_FOUND -eq 0 ]; then
        for IP in 172.17.0.1 10.0.2.2 192.168.65.2; do
            [ $OLLAMA_FOUND -eq 1 ] && break
            try_ollama "$IP" "common-bridge" 2>/dev/null || true
        done
    fi

    # Fallback
    if [ $OLLAMA_FOUND -eq 0 ]; then
        export OLLAMA_URL="http://host.containers.internal:${OLLAMA_PORT}"
        echo ""
        echo "      WARNING: Ollama not detected automatically."
        echo "      Default: $OLLAMA_URL"
        echo "      You can set OLLAMA_URL manually from the web interface Settings page."
        echo "      Or restart with: -e OLLAMA_URL=http://YOUR_HOST_IP:11434"
    fi
fi

echo ""
echo "      Final OLLAMA_URL: $OLLAMA_URL"

# -- Network diagnostics --
echo ""
echo "[1/3] Network diagnostics:"
echo "      Container IP:  $(hostname -I 2>/dev/null || echo 'unknown')"
echo "      Gateway:       $(ip route | grep default | awk '{print $3}' 2>/dev/null || echo 'unknown')"
echo "      DNS:           $(cat /etc/resolv.conf 2>/dev/null | grep nameserver | head -1 | awk '{print $2}' || echo 'unknown')"

# Ping tests
echo "      Ping tests:"
for HOST in host.containers.internal host.docker.internal; do
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
