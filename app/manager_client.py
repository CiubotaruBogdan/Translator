"""Authenticated localhost requests for the Windows/Podman manager.

Invoked through `podman exec`; credentials stay inside the container and never
appear in process arguments or output. Only fixed application endpoints are used.
"""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path, method="GET"):
    filename = os.environ.get("TRANSLATOR_PASSWORD_FILE", "")
    if filename:
        password = Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
    else:
        password = os.environ.get("TRANSLATOR_PASSWORD", "")
    if not password:
        raise RuntimeError("Parola aplicatiei nu este configurata in container.")
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise RuntimeError("Portul aplicatiei este invalid.")
    credentials = base64.b64encode(("translator:" + password).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        headers={"Authorization": "Basic " + credentials},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=20) as response:
        return json.load(response)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("status", "cleanup", "network"):
        print("Usage: manager_client.py status|cleanup|network", file=sys.stderr)
        return 2
    try:
        action = args[0]
        if action == "status":
            state = request("/api/status")
            libre = bool(state.get("libretranslate", {}).get("connected"))
            ollama = bool(state.get("ollama", {}).get("connected"))
            print("Web: conectat si autentificat")
            print("LibreTranslate: " + ("conectat" if libre else "deconectat"))
            print("Ollama: " + ("conectat" if ollama else "deconectat"))
            engine = os.environ.get("TRANSLATE_ENGINE", "libretranslate")
            if engine not in ("libretranslate", "ollama"):
                raise RuntimeError("Motorul selectat este invalid.")
            return 0 if (libre if engine == "libretranslate" else ollama) else 1
        if action == "cleanup":
            result = request("/api/files/cleanup", "POST")
        else:
            result = request("/api/network-diag")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        # Never echo response bodies or request headers containing authentication.
        print(f"Cererea a esuat (HTTP {exc.code}). Verifica parola si serviciile.", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        print("Nu s-a putut contacta aplicatia sau citi configuratia locala.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
