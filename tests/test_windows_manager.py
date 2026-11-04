"""Manager credential transport plus static Windows lifecycle safety checks.

The batch/PowerShell scripts need a Windows/Podman smoke test as well; these
checks deliberately do not claim to execute Windows commands on Linux.
"""
import base64
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import manager_client


class ManagerClientTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.response = {"libretranslate": {"connected": True}, "ollama": {"connected": False}}
        self.status = 200
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                parent.received.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(parent.status)
                if parent.status == 302:
                    self.send_header("Location", "/redirect-target")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(parent.response).encode())
            do_POST = do_GET
        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.addCleanup(self.thread.join, .2)
        self.environ = patch.dict(os.environ, {
            "PORT": str(self.http.server_port), "TRANSLATOR_PASSWORD": "",
            "TRANSLATOR_PASSWORD_FILE": "", "TRANSLATE_ENGINE": "libretranslate",
            "HTTP_PROXY": "http://127.0.0.1:1",
        })
        self.environ.start(); self.addCleanup(self.environ.stop)

    def run_command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = manager_client.main(list(args))
        return code, output.getvalue()

    def test_secret_file_authenticates_without_echoing_secret(self):
        password = "synthetic:'!parola-șir"
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "password"
            file.write_text(password + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"TRANSLATOR_PASSWORD_FILE": str(file),
                                        "TRANSLATOR_PASSWORD": "ignored-environment-password"}):
                code, output = self.run_command("status")
        self.assertEqual(code, 0)
        expected = "Basic " + base64.b64encode(("translator:" + password).encode()).decode()
        self.assertEqual(self.received, [("GET", "/api/status", expected)])
        self.assertNotIn(password, output)
        self.assertNotIn(expected, output)

    def test_missing_secret_fails_without_request(self):
        code, output = self.run_command("cleanup")
        self.assertEqual(code, 1)
        self.assertEqual(self.received, [])

    def test_cleanup_uses_authenticated_post(self):
        with patch.dict(os.environ, {"TRANSLATOR_PASSWORD": "synthetic-test-password"}):
            self.response = {"deleted": 2, "skipped": 1}
            code, output = self.run_command("cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(self.received[0][:2], ("POST", "/api/files/cleanup"))
        self.assertIn('"skipped": 1', output)

    def test_redirect_does_not_forward_credentials(self):
        self.status = 302
        with patch.dict(os.environ, {"TRANSLATOR_PASSWORD": "synthetic-test-password"}):
            code, output = self.run_command("network")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.received), 1)
        self.assertIn("HTTP 302", output)
        self.assertNotIn("synthetic-test-password", output)

    def test_disconnected_required_engine_reports_failure(self):
        with patch.dict(os.environ, {"TRANSLATOR_PASSWORD": "synthetic-test-password", "TRANSLATE_ENGINE": "ollama"}):
            code, _ = self.run_command("status")
        self.assertEqual(code, 1)


class ManagerLifecycleSourceTests(unittest.TestCase):
    def test_start_preserves_existing_container_and_uses_persistent_storage(self):
        source = (Path(__file__).parents[1] / "traducator_manager.bat").read_text()
        start = source[source.index(":START\n"):source.index(":CHECK\n")]
        self.assertNotIn("podman rm ", start)
        self.assertNotIn("podman stop ", start)
        self.assertIn("podman start %CONTAINER_NAME%", start)
        self.assertIn("if not \"!MANAGED_VERSION!\"==\"2\" goto START_LEGACY", start)
        self.assertIn("-v %UPLOAD_VOLUME%:/app/uploads", start)
        self.assertIn("-v %OUTPUT_VOLUME%:/app/outputs", start)
        self.assertIn("--secret %SECRET_NAME%", start)
        self.assertNotIn("set /p \"PASSWORD", source)


if __name__ == "__main__":
    unittest.main()
