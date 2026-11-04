"""Build/startup regressions. No real model downloads or containers are needed."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('translator_installer', ROOT / 'install_languages.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ModelInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manifest = Path(self.temp.name) / 'installed_languages.txt'
        self.installed = []
        self.calls = []
        self.package = ModuleType('argostranslate.package')
        self.package.update_package_index = lambda: self.calls.append('index')
        self.package.get_installed_packages = lambda: self.installed
        self.package.get_available_packages = lambda: [self.model(a, b) for a, b in [('en', 'ro'), ('ro', 'en')]]
        translate = ModuleType('argostranslate.translate')
        translate.translate = lambda text, a, b: self.calls.append((a, b)) or 'translation'
        argos = ModuleType('argostranslate')
        argos.__path__ = []
        argos.package = self.package
        argos.translate = translate
        modules = {'argostranslate': argos, 'argostranslate.package': self.package,
                   'argostranslate.translate': translate}
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.translate = translate

    def model(self, source, target):
        model = SimpleNamespace(from_code=source, to_code=target)
        model.install = lambda: self.installed.append(model)
        return model

    def run_installer(self, *args):
        return installer.main([*args, '--output', str(self.manifest)])

    def test_failed_download_fails_and_does_not_advertise_languages(self):
        def unavailable():
            raise OSError('download unavailable')
        model = self.model('en', 'ro')
        model.install = unavailable
        self.package.get_available_packages = lambda: [model]
        with self.assertRaisesRegex(OSError, 'download unavailable'):
            self.run_installer('en,ro')
        self.assertFalse(self.manifest.exists())

    def test_missing_required_direction_fails(self):
        self.package.get_available_packages = lambda: [self.model('en', 'ro')]
        with self.assertRaisesRegex(RuntimeError, 'ro->en'):
            self.run_installer('en,ro')
        self.assertFalse(self.manifest.exists())

    def test_success_requires_both_directions_and_warmup(self):
        self.assertEqual(self.run_installer('en,ro'), 0)
        self.assertEqual(self.manifest.read_text(), 'en,ro')
        self.assertIn(('en', 'ro'), self.calls)
        self.assertIn(('ro', 'en'), self.calls)
        self.calls.clear()
        self.assertEqual(self.run_installer('--verify-only'), 0)
        self.assertNotIn('index', self.calls)
        self.assertEqual(set(self.calls), {('en', 'ro'), ('ro', 'en')})

    def test_offline_verification_rejects_missing_lazy_cache(self):
        self.installed = [self.model('en', 'ro'), self.model('ro', 'en')]
        self.manifest.write_text('en,ro')
        self.translate.translate = lambda *args: socket.create_connection(('unused.invalid', 443))
        with self.assertRaisesRegex(RuntimeError, 'Offline verification attempted'):
            self.run_installer('--verify-only')

    def test_empty_translation_cannot_pass_build(self):
        self.translate.translate = lambda *args: ''
        with self.assertRaisesRegex(RuntimeError, 'Empty warmup translation'):
            self.run_installer('en,ro')
        self.assertFalse(self.manifest.exists())

    def test_unknown_language_is_not_silently_omitted(self):
        with self.assertRaisesRegex(ValueError, 'Unknown language'):
            self.run_installer('en,ro,unknown')


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env.pop('TRANSLATOR_PASSWORD', None)
        self.env.pop('TRANSLATOR_PASSWORD_FILE', None)
        self.env.update(TRANSLATOR_APP_DIR=str(self.root), OLLAMA_URL='http://configured:11434',
                        TRANSLATOR_START_TIMEOUT='1', TRANSLATE_ENGINE='libretranslate',
                        LIBRETRANSLATE_URL='http://127.0.0.1:5000', INSTALLED_LANGUAGES='en,ro')
        self.env['PATH'] = str(self.root) + os.pathsep + self.env['PATH']

    def stub(self, name, body):
        path = self.root / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o700)

    def execute(self, *args):
        return subprocess.run(['bash', str(ROOT / 'entrypoint.sh'), *args], env=self.env,
                              capture_output=True, text=True, timeout=8)

    def test_explicit_command_does_not_start_services_or_require_password(self):
        result = self.execute(sys.executable, '-c', 'print("inspection-command")')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'inspection-command')

    def test_service_start_without_password_fails_closed(self):
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('configure TRANSLATOR_PASSWORD_FILE', result.stderr)

    def test_dead_required_engine_does_not_start_app(self):
        self.env['TRANSLATOR_PASSWORD'] = 'test-only-password'
        self.stub('libretranslate', 'exit 1')
        self.stub('curl', 'exit 22')
        (self.root / 'server.py').write_text('raise RuntimeError("web server must not start")')
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('required LibreTranslate language models are not ready', result.stderr)
        self.assertNotIn('web server must not start', result.stderr)

    def test_http_success_without_languages_is_not_ready(self):
        self.env['TRANSLATOR_PASSWORD'] = 'test-only-password'
        self.stub('libretranslate', 'exec sleep 30')
        self.stub('curl', "printf '[]'")
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('required LibreTranslate language models are not ready', result.stderr)


class ContainerInputsTests(unittest.TestCase):
    def test_container_has_no_broad_app_copy(self):
        # Protect the build boundary independently of Git ignore behavior.
        instructions = (ROOT / 'Containerfile').read_text().splitlines()
        copies = [line.split() for line in instructions if line.startswith('COPY ')]
        app_sources = [parts[1] for parts in copies if parts[1].startswith('app/')]
        self.assertEqual(set(app_sources), {'app/*.py', 'app/static/'})
        self.assertNotIn('app/', app_sources)
        for name in ('.dockerignore', '.containerignore'):
            rules = [line for line in (ROOT / name).read_text().splitlines() if line and not line.startswith('#')]
            self.assertEqual(rules[0], '*')
            self.assertNotIn('!app/uploads', rules)
            self.assertNotIn('!app/outputs', rules)

    def test_export_requires_offline_inference_before_save(self):
        export = (ROOT / 'builder.bat').read_text().split('\n:DO_EXPORT\n', 1)[1]
        self.assertLess(export.index('--network none'), export.index('podman save'))
        self.assertIn('--verify-only', export)


if __name__ == '__main__':
    unittest.main()
