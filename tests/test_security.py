"""Access, SSRF and file-staging regressions; no live services or secrets used."""
import base64
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import security, server

PASSWORD = "synthetic-audit-password"
AUTH = "Basic " + base64.b64encode(("translator:" + PASSWORD).encode()).decode()


async def request_asgi(app, path="/", method="GET", headers=None, websocket=False):
    values = {"host": "translator.test", **(headers or {})}
    scope = {"type": "websocket" if websocket else "http", "asgi": {"version": "3.0"},
             "http_version": "1.1", "method": method, "scheme": "ws" if websocket else "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in values.items()],
             "server": ("translator.test", 80), "client": ("192.0.2.10", 1234)}
    messages = []
    async def receive():
        return {"type": "websocket.connect"} if websocket else {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    return messages


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TRANSLATOR_PASSWORD": PASSWORD, "TRANSLATOR_PASSWORD_FILE": ""})
        env.start(); self.addCleanup(env.stop)

    async def test_unauthenticated_data_and_destructive_routes_are_denied(self):
        for method, path in [("GET", "/api/prompts"), ("GET", "/api/files"), ("POST", "/api/files/cleanup"), ("DELETE", "/api/jobs/example")]:
            messages = await request_asgi(server.app, path, method)
            self.assertEqual(messages[0]["status"], 401)
            self.assertIn(b"www-authenticate", dict(messages[0]["headers"]))
            self.assertNotIn(b"access-control-allow-origin", dict(messages[0]["headers"]))

    async def test_missing_secret_fails_closed(self):
        with patch.dict(os.environ, {"TRANSLATOR_PASSWORD": ""}):
            messages = await request_asgi(server.app, "/api/prompts")
        self.assertEqual(messages[0]["status"], 503)

    async def test_correct_password_cookie_and_websocket(self):
        async def endpoint(scope, receive, send):
            if scope["type"] == "websocket":
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 1000})
            else:
                await JSONResponse({"ok": True})(scope, receive, send)
        app = security.SharedPasswordMiddleware(endpoint)
        response = await request_asgi(app, headers={"authorization": AUTH, "origin": "http://translator.test"})
        self.assertEqual(response[0]["status"], 200)
        cookie = dict(response[0]["headers"])[b"set-cookie"].decode()
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)
        cookie = cookie.split(";", 1)[0]
        response = await request_asgi(app, headers={"cookie": cookie})
        self.assertEqual(response[0]["status"], 200)
        ws = await request_asgi(app, websocket=True, headers={"cookie": cookie, "origin": "http://translator.test"})
        self.assertEqual(ws[0]["type"], "websocket.accept")
        denied = await request_asgi(app, websocket=True)
        self.assertEqual(denied[0], {"type": "websocket.close", "code": 1008})
        tampered = await request_asgi(app, headers={"cookie": cookie[:-1] + ("0" if cookie[-1] != "0" else "1")})
        self.assertEqual(tampered[0]["status"], 401)

    async def test_cross_origin_requests_are_rejected_even_with_credentials(self):
        for headers in ({"origin": "http://other.test"}, {"sec-fetch-site": "cross-site"}, {"origin": "null"}):
            messages = await request_asgi(server.app, "/api/files/cleanup", "POST", {"authorization": AUTH, **headers})
            self.assertEqual(messages[0]["status"], 403)
        ws = await request_asgi(server.app, "/ws/dashboard", websocket=True,
                                headers={"authorization": AUTH, "origin": "http://other.test"})
        self.assertEqual(ws[0]["type"], "websocket.close")

    async def test_malformed_non_ascii_cookie_is_rejected_without_server_error(self):
        token = f'{int(time.time()) + 300}.é'
        # Browsers may octal-escape a quoted cookie character.
        for value in (token, token.replace('é', r'\351')):
            messages = await request_asgi(server.app, "/api/prompts", headers={"cookie": f'translator_session="{value}"'})
            self.assertEqual(messages[0]["status"], 401)

    async def test_opening_application_link_allows_login_but_not_embedded_requests(self):
        navigation = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
        response = await request_asgi(server.app, headers=navigation)
        self.assertEqual(response[0]["status"], 401)
        response = await request_asgi(server.app, headers={**navigation, "authorization": AUTH})
        self.assertEqual(response[0]["status"], 200)
        response = await request_asgi(server.app, headers={**navigation, "sec-fetch-dest": "iframe", "authorization": AUTH})
        self.assertEqual(response[0]["status"], 403)

    async def test_health_is_public_minimal_and_requires_configured_engine(self):
        with patch.object(server, "DEFAULT_ENGINE", "libretranslate"), \
                patch.object(server, "api_libretranslate", AsyncMock(return_value={"connected": False, "languages": []})):
            messages = await request_asgi(server.app, "/healthz")
        self.assertEqual(messages[0]["status"], 503)
        self.assertEqual(json.loads(messages[1]["body"]), {"ready": False})
        with patch.object(server, "DEFAULT_ENGINE", "libretranslate"), \
                patch.object(server, "_get_installed_languages", return_value=["en", "ro"]), \
                patch.object(server, "api_libretranslate", AsyncMock(return_value={"connected": True, "languages": [{"code":"en"}, {"code":"ro"}]})):
            messages = await request_asgi(server.app, "/healthz")
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(json.loads(messages[1]["body"]), {"ready": True})

    def test_password_file_is_supported_without_logging(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "password"
            path.write_text("file-secret\n")
            with patch.dict(os.environ, {"TRANSLATOR_PASSWORD_FILE": str(path)}):
                self.assertEqual(security.configured_password(), "file-secret")
            with patch.dict(os.environ, {"TRANSLATOR_PASSWORD_FILE": str(path) + "-missing"}):
                self.assertIsNone(security.configured_password())


class EngineAndFilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"; self.outputs = self.root / "outputs"
        self.uploads.mkdir(); self.outputs.mkdir()
        for name, value in [("UPLOAD_DIR", self.uploads), ("OUTPUT_DIR", self.outputs), ("jobs", {})]:
            p = patch.object(server, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"TRANSLATOR_ALLOWED_ENGINE_URLS": ""}); p.start(); self.addCleanup(p.stop)

    async def test_engine_destination_and_fragment_bypass_are_rejected_before_network(self):
        with patch.object(server.aiohttp, "ClientSession") as session:
            for url in ("http://127.0.0.1:54321", "http://127.0.0.1:5000/internal-config#", "http://user:pass@127.0.0.1:5000", "http://127.0.0.1:5000/?x=1"):
                with self.assertRaises(HTTPException) as error:
                    await server.api_libretranslate(url)
                self.assertEqual(error.exception.status_code, 400)
            session.assert_not_called()
        self.assertEqual(security.approved_engine_url("http://localhost:5000", ["http://127.0.0.1:5000"]), "http://127.0.0.1:5000")
        with patch.dict(os.environ, {"TRANSLATOR_ALLOWED_ENGINE_URLS": "http://engine.test:11434"}):
            self.assertEqual(server.validate_engine_url("http://engine.test:11434"), "http://engine.test:11434")

    async def test_probe_disables_redirects_and_does_not_reflect_arbitrary_json(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {"private": "synthetic"}
            def get(inner, url, **kwargs):
                self.assertIs(kwargs.get("allow_redirects"), False)
                return inner
        with patch.object(server.aiohttp, "ClientSession", return_value=Response()):
            result = await server.api_libretranslate(server.DEFAULT_LIBRETRANSLATE_URL)
        self.assertFalse(result["connected"])
        self.assertEqual(result["languages"], [])

    async def test_allowed_engine_cannot_redirect_to_unrelated_service_path(self):
        paths = []
        async def handler(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            path = header.split(b" ", 2)[1].decode()
            paths.append(path)
            if path == "/languages":
                response = b"HTTP/1.1 302 Found\r\nLocation: /private-config\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            else:
                body = b'{"private":"synthetic"}'
                response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        listener = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with listener:
            url = "http://127.0.0.1:" + str(listener.sockets[0].getsockname()[1])
            with patch.dict(os.environ, {"TRANSLATOR_ALLOWED_ENGINE_URLS": url}):
                result = await server.api_libretranslate(url)
        self.assertFalse(result["connected"])
        self.assertEqual(paths, ["/languages"])

    async def test_invalid_document_pairs_leave_no_staged_files_or_jobs(self):
        for _ in range(3):
            original = UploadFile(file=io.BytesIO(b"invalid"), filename="source.docx")
            translated = UploadFile(file=io.BytesIO(b"invalid"), filename="translated.docx")
            with self.assertRaises(HTTPException):
                await server.api_verify_documents(SimpleNamespace(client=None), original, translated, "en", "ro")
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertEqual(server.jobs, {})

    async def test_staged_verification_files_are_protected_during_extraction(self):
        def extract(path):
            deleted, skipped = server._cleanup_files(older_than=None)
            self.assertEqual(deleted, 0)
            self.assertGreaterEqual(skipped, 2)
            self.assertTrue(path.exists())
            return ["Source or translation"], None
        original = UploadFile(file=io.BytesIO(b"Source"), filename="source.txt")
        translated = UploadFile(file=io.BytesIO(b"Translation"), filename="translation.txt")
        with patch.object(server, "extract_review_segments", side_effect=extract):
            response = await server.api_verify_documents(SimpleNamespace(client=None), original, translated, "en", "ro")
        self.assertIn(response["job_id"], server.jobs)
        self.assertEqual(len(list(self.uploads.iterdir())), 2)
        self.assertEqual(len(list(self.outputs.iterdir())), 1)

    async def test_long_filename_is_bounded_and_display_name_preserved(self):
        filename = "a" * 247 + ".txt"
        file = UploadFile(file=io.BytesIO(b"Text"), filename=filename)
        with patch.object(server, "start_pipeline") as start:
            response = await server.api_translate(SimpleNamespace(client=None), file, "en", "ro", "model", "", 1, 4096,
                                                  "libretranslate", server.DEFAULT_LIBRETRANSLATE_URL, "false", 1, "30m", 500, "false")
        job = server.jobs[response["job_id"]]
        self.assertEqual(job.to_dict()["filename"], filename)
        self.assertLessEqual(len(job.filename.encode()), 120)
        self.assertTrue((self.uploads / f"{job.job_id}_{job.filename}").exists())
        start.assert_called_once_with(job)

    async def test_write_failure_creates_no_ghost_job(self):
        file = UploadFile(file=io.BytesIO(b"Text"), filename="source.txt")
        with patch.object(Path, "write_bytes", side_effect=OSError("storage failure")):
            with self.assertRaises(HTTPException):
                await server.api_translate(SimpleNamespace(client=None), file, "en", "ro", "model", "", 1, 4096,
                                           "libretranslate", server.DEFAULT_LIBRETRANSLATE_URL, "false", 1, "30m", 500, "false")
        self.assertEqual(server.jobs, {})

    async def test_retained_file_download_and_delete_reject_unsafe_paths(self):
        path = self.outputs / "retained.txt"; path.write_text("Retained result")
        download = await server.api_download_file("output", "retained.txt")
        self.assertEqual(Path(download.path), path)
        for name in ("..", "../outside", "folder/file", "folder\\file"):
            with self.assertRaises(HTTPException):
                await server.api_download_file("output", name)
        (self.outputs / "link.txt").symlink_to(path)
        with self.assertRaises(HTTPException):
            await server.api_download_file("output", "link.txt")
        with patch.object(server, "is_file_active", return_value=True):
            with self.assertRaises(HTTPException) as error:
                await server.api_delete_file("output", "retained.txt")
            self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
