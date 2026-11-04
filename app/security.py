"""Shared LAN access and explicitly configured translation-engine destinations."""
import base64
import hashlib
import hmac
import os
import re
from pathlib import Path
import time
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

USERNAME = "translator"
COOKIE_NAME = "translator_session"
SESSION_SECONDS = 8 * 60 * 60


def configured_password():
    """Read the configured secret without logging it; absence fails closed."""
    filename = os.environ.get("TRANSLATOR_PASSWORD_FILE", "")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            return None
    else:
        value = os.environ.get("TRANSLATOR_PASSWORD", "")
    return value if value and len(value) <= 4096 else None


def _signed_session(password, expires):
    value = str(expires)
    signature = hmac.new(password.encode("utf-8"), ("translator-session:" + value).encode(), hashlib.sha256).hexdigest()
    return value + "." + signature


def _cookie_authenticated(headers, password):
    from http.cookies import SimpleCookie, CookieError
    try:
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        token = cookies[COOKIE_NAME].value
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{64}", token):
            return False
        value, _ = token.split(".", 1)
        expires = int(value)
        return time.time() < expires <= time.time() + SESSION_SECONDS + 60 and hmac.compare_digest(token, _signed_session(password, expires))
    except (CookieError, KeyError, ValueError):
        return False


def _basic_authenticated(headers, password):
    try:
        scheme, encoded = headers.get("authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            return False
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, supplied = raw.split(":", 1)
        return hmac.compare_digest(username.encode(), USERNAME.encode()) and hmac.compare_digest(supplied.encode(), password.encode())
    except (ValueError, UnicodeError):
        return False


def _origin_tuple(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _same_origin(scope, headers):
    # Native HTTP clients do not send Origin/Fetch Metadata. Browser requests
    # must originate from this exact application origin, including WebSockets.
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        # Opening a link to the app is safe: it cannot read data back into the
        # referring page. Keep cross-site fetches, frames, WS and writes blocked.
        navigation = (scope["type"] == "http" and scope.get("method") in ("GET", "HEAD")
                      and headers.get("sec-fetch-mode") == "navigate"
                      and headers.get("sec-fetch-dest") == "document"
                      and not headers.get("origin"))
        return navigation
    origin = headers.get("origin")
    if origin is None:
        return True
    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    try:
        return _origin_tuple(origin) == _origin_tuple(scheme + "://" + headers.get("host", ""))
    except ValueError:
        return False


class SharedPasswordMiddleware:
    """HTTP Basic login plus a signed session cookie usable by browser WS/SSE."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") == "/healthz" and scope.get("method") in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        password = configured_password()
        basic = bool(password) and _basic_authenticated(headers, password)
        authenticated = bool(password) and (basic or _cookie_authenticated(headers, password))
        same_origin = _same_origin(scope, headers)
        if not password or not authenticated or not same_origin:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            if not password:
                response = JSONResponse({"detail": "Acces neconfigurat. Configurează parola comună a aplicației prin TRANSLATOR_PASSWORD_FILE sau TRANSLATOR_PASSWORD."}, status_code=503)
            elif not same_origin:
                response = JSONResponse({"detail": "Accesul din altă origine nu este permis."}, status_code=403)
            else:
                response = JSONResponse({"detail": "Autentificare necesară. Utilizator: translator."}, status_code=401,
                                        headers={"WWW-Authenticate": 'Basic realm="Translator", charset="UTF-8"'})
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                output_headers = list(message.get("headers", []))
                output_headers += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin")]
                if basic:
                    token = _signed_session(password, int(time.time()) + SESSION_SECONDS)
                    cookie = f"{COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict"
                    if scope.get("scheme") == "https":
                        cookie += "; Secure"
                    output_headers.append((b"set-cookie", cookie.encode("ascii")))
                message["headers"] = output_headers
            await send(message)
        await self.app(scope, receive, secured_send)


def normalize_engine_url(value):
    """Only an HTTP(S) origin is accepted; credentials/path/query are forbidden."""
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 33 for c in value):
        raise ValueError("URL motor invalid")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or "?" in value or "#" in value or "\\" in value):
        raise ValueError("URL-ul motorului trebuie să fie o origine HTTP(S), fără cale, parametri sau fragment")
    host = parsed.hostname.lower()
    # Equivalent local loopback aliases are used by older saved UI settings.
    if host == "localhost":
        host = "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Port invalid")
    if ":" in host:
        host = "[" + host + "]"
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port != default_port else "")


def approved_engine_url(value, defaults):
    allowed = set()
    extra = os.environ.get("TRANSLATOR_ALLOWED_ENGINE_URLS", "")
    for candidate in [*defaults, *extra.split(",")]:
        if candidate.strip():
            allowed.add(normalize_engine_url(candidate.strip()))
    normalized = normalize_engine_url(value)
    if normalized not in allowed:
        raise ValueError("Motor nepermis. Administratorul trebuie să configureze această adresă în TRANSLATOR_ALLOWED_ENGINE_URLS.")
    return normalized


def bounded_filename(value, max_bytes=120):
    """Keep a display name separately from a portable, bounded storage basename."""
    if not isinstance(value, str) or not value.strip() or value in (".", "..") or any(c in value for c in ("/", "\\", "\0")):
        raise ValueError("Nume de fișier invalid")
    import re
    filename = re.sub(r'[<>:"|?*\x00-\x1f\x7f]', "_", value.strip()).rstrip(". ")
    suffix = Path(filename).suffix.lower()
    if len(suffix.encode("utf-8")) > 16:
        raise ValueError("Extensie de fișier invalidă")
    stem = filename[:-len(suffix)] if suffix else filename
    available = max_bytes - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:available].decode("utf-8", errors="ignore").rstrip(". ") or "document"
    return stem + suffix
