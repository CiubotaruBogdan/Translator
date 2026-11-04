#!/usr/bin/env bash
set -euo pipefail

# Explicit commands (inspection, offline checks, maintenance) must not start services.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

APP_DIR=${TRANSLATOR_APP_DIR:-/app}
PORT=${PORT:-8080}
OLLAMA_PORT=${OLLAMA_PORT:-11434}
TRANSLATE_ENGINE=${TRANSLATE_ENGINE:-libretranslate}
export PORT TRANSLATE_ENGINE

# A LAN deployment must never silently start with anonymous access.
if [ -z "${TRANSLATOR_PASSWORD:-}" ]; then
    if [ -z "${TRANSLATOR_PASSWORD_FILE:-}" ] || [ ! -r "$TRANSLATOR_PASSWORD_FILE" ] || [ ! -s "$TRANSLATOR_PASSWORD_FILE" ]; then
        echo 'ERROR: configure TRANSLATOR_PASSWORD_FILE (recommended) or TRANSLATOR_PASSWORD before startup.' >&2
        exit 1
    fi
fi

if [ -z "${OLLAMA_URL:-}" ] || [ "$OLLAMA_URL" = auto ]; then
    OLLAMA_URL="http://host.containers.internal:${OLLAMA_PORT}"
    for host in host.containers.internal host.docker.internal; do
        candidate="http://${host}:${OLLAMA_PORT}"
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 3 "$candidate/api/version" >/dev/null 2>&1; then
            OLLAMA_URL="$candidate"
            break
        fi
    done
fi
export OLLAMA_URL

if [ -r "$APP_DIR/installed_languages.txt" ]; then
    INSTALLED_LANGUAGES=$(cat "$APP_DIR/installed_languages.txt")
else
    INSTALLED_LANGUAGES=${INSTALLED_LANGUAGES:-en,ro}
fi
export INSTALLED_LANGUAGES
LIBRETRANSLATE_URL=${LIBRETRANSLATE_URL:-http://127.0.0.1:5000}
export LIBRETRANSLATE_URL

LIBRE_PID=
APP_PID=
cleanup() {
    trap - EXIT INT TERM
    for pid in "${APP_PID:-}" "${LIBRE_PID:-}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
    done
    for pid in "${APP_PID:-}" "${LIBRE_PID:-}"; do
        if [ -n "$pid" ]; then wait "$pid" 2>/dev/null || true; fi
    done
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# A custom external LibreTranslate URL is allowed; only launch the bundled service
# for the image's internal default. Never claim readiness from HTTP status alone.
if [ "$LIBRETRANSLATE_URL" = http://127.0.0.1:5000 ] || [ "$LIBRETRANSLATE_URL" = http://localhost:5000 ]; then
    libretranslate --host 127.0.0.1 --port 5000 --load-only "$INSTALLED_LANGUAGES" --threads 4 --disable-web-ui &
    LIBRE_PID=$!
fi

start_timeout=${TRANSLATOR_START_TIMEOUT:-120}
if ! [[ "$start_timeout" =~ ^[1-9][0-9]*$ ]]; then
    echo 'ERROR: TRANSLATOR_START_TIMEOUT must be a positive number of seconds.' >&2
    exit 1
fi
ready=0
deadline=$((SECONDS + start_timeout))
while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -n "$LIBRE_PID" ] && ! kill -0 "$LIBRE_PID" 2>/dev/null; then
        echo 'ERROR: LibreTranslate exited during startup.' >&2
        break
    fi
    if curl --fail --silent --show-error --connect-timeout 2 --max-time 3 "$LIBRETRANSLATE_URL/languages" 2>/dev/null |
        python3 -c 'import json,os,sys; data=json.load(sys.stdin); assert isinstance(data,list); codes={x["code"] for x in data}; assert set(os.environ["INSTALLED_LANGUAGES"].split(",")) <= codes' 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    if [ "$TRANSLATE_ENGINE" = libretranslate ]; then
        echo 'ERROR: required LibreTranslate language models are not ready; startup aborted.' >&2
        exit 1
    fi
    echo 'WARNING: optional LibreTranslate is unavailable; selected Ollama engine will be checked by /healthz.' >&2
fi

cd "$APP_DIR"
echo "Starting Translator on port $PORT; required engine: $TRANSLATE_ENGINE."
echo 'LAN access requires username translator and the configured shared password.'
python3 server.py &
APP_PID=$!

# Exit if the required engine dies; do not leave a healthy-looking web process.
set +e
if [ "$TRANSLATE_ENGINE" = libretranslate ] && [ -n "$LIBRE_PID" ]; then
    wait -n "$APP_PID" "$LIBRE_PID"
    status=$?
    if kill -0 "$APP_PID" 2>/dev/null; then
        echo 'ERROR: required LibreTranslate stopped; stopping the web service.' >&2
        status=1
    fi
else
    wait "$APP_PID"
    status=$?
fi
exit "$status"
