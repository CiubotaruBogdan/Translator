"""Native Windows setup regressions using an in-memory registry adapter.

These checks execute the Python setup logic on Linux; they do not substitute for
running the batch launchers and Windows registry notifications on Windows.
"""
from contextlib import redirect_stdout
import copy
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings

import initializare as setup
from app.security import configured_password


class MemoryRegistry:
    """Keep value types so a failed setup cannot silently change old settings."""
    def __init__(self, user=None, system=None, fail_write=None, failure=None):
        self.user = copy.deepcopy(user or {})
        self.system = copy.deepcopy(system or {})
        self.fail_write = fail_write
        self.failure = failure
        self.writes = []

    def read_user(self, name):
        return self.user.get(name)

    def read_system(self, name):
        return self.system.get(name)

    def write_user(self, name, value):
        self.writes.append((name, value))
        if len(self.writes) == self.fail_write:
            raise self.failure or PermissionError('Synthetic registry write failure')
        if value is None:
            self.user.pop(name, None)
        else:
            self.user[name] = (value, 1)  # REG_SZ, never percent-expanding REG_EXPAND_SZ

    def restore_user(self, name, entry):
        if entry is None:
            self.user.pop(name, None)
        else:
            self.user[name] = entry


def configuration(**overrides):
    values = dict(setup.DEFAULTS)
    values['TRANSLATOR_PASSWORD'] = "synthetic:parolă!%PATH%"
    values.update(overrides)
    return values


class InitializationTests(unittest.TestCase):
    def test_default_wizard_uses_masked_password_and_native_engine_address(self):
        password = 'synthetic secret:șir!%'
        prompt = Mock(return_value='')
        secret = Mock(side_effect=[password, password])
        output = io.StringIO()
        with redirect_stdout(output):
            result = setup.collect_settings({}, input_fn=prompt, secret_fn=secret)
        self.assertEqual(result['TRANSLATOR_PASSWORD'], password)
        self.assertEqual(result['OLLAMA_URL'], 'http://127.0.0.1:11434')
        self.assertEqual(result['PORT'], '8080')
        self.assertEqual(secret.call_count, 2)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(password, str(prompt.call_args_list))

    def test_existing_password_is_kept_without_echo_or_reentry(self):
        existing = configuration()
        secret = Mock(side_effect=AssertionError('Existing password must not be requested again'))
        output = io.StringIO()
        with redirect_stdout(output):
            result = setup.collect_settings(existing, input_fn=lambda _: '', secret_fn=secret)
        self.assertEqual(result['TRANSLATOR_PASSWORD'], existing['TRANSLATOR_PASSWORD'])
        self.assertNotIn(existing['TRANSLATOR_PASSWORD'], output.getvalue())

    def test_mismatched_password_and_invalid_port_are_retried(self):
        password = 'synthetic-valid-password'
        secrets = Mock(side_effect=[password, 'different-password', password, password])
        ports = iter(['0', 'not-a-port', '8091'])
        def prompt(message):
            return next(ports) if message.startswith('Portul interfetei') else ''
        output = io.StringIO()
        with redirect_stdout(output):
            result = setup.collect_settings({}, input_fn=prompt, secret_fn=secrets)
        self.assertEqual(result['PORT'], '8091')
        self.assertEqual(result['TRANSLATOR_PASSWORD'], password)
        self.assertEqual(secrets.call_count, 4)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn('different-password', output.getvalue())

    def test_cancel_during_wizard_leaves_saved_configuration_untouched(self):
        registry = MemoryRegistry(user={'PORT': ('9000', 1)})
        before = copy.deepcopy(registry.user)
        with patch.object(setup.sys, 'platform', 'win32'), \
                patch.object(setup, 'WindowsEnvironment', return_value=registry), \
                patch.object(setup, 'collect_settings', side_effect=KeyboardInterrupt), \
                patch.object(setup, 'persist_settings', wraps=setup.persist_settings) as persist, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main([]), 130)
        persist.assert_not_called()
        self.assertEqual(registry.user, before)

    def test_password_input_refuses_a_console_that_would_echo_the_secret(self):
        def insecure_console(_prompt):
            warnings.warn('Cannot control echo on the terminal.', setup.getpass.GetPassWarning)
            self.fail('The insecure input fallback must never execute')
        with patch.object(setup.getpass, 'getpass', side_effect=insecure_console), \
                self.assertRaises(setup.SetupError):
            setup.masked_password('Parola: ')

    def test_engine_url_rejects_paths_credentials_and_invalid_ports(self):
        invalid = [
            'file:///etc/passwd', 'http://user:secret@localhost:11434',
            'http://localhost:11434/api/tags', 'http://localhost:11434?x=1',
            'http://localhost:11434#x', 'http://localhost:0',
            'http://localhost:99999', 'http://localhost\\other',
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, setup.SetupError)):
                setup.validate_origin(value)
        self.assertEqual(setup.validate_origin('http://localhost:11434/'), 'http://127.0.0.1:11434')

    def test_invalid_settings_fail_before_they_can_be_saved(self):
        for name, value in [('PORT', '0'), ('PORT', '65536'), ('PORT', '8080;whoami'),
                            ('FILE_RETENTION_HOURS', '0'), ('FILE_RETENTION_HOURS', 'nan'),
                            ('TRANSLATE_ENGINE', 'unknown'), ('OLLAMA_MODEL', ''),
                            ('TRANSLATOR_PASSWORD', 'short')]:
            with self.subTest(name=name, value=value), self.assertRaises((ValueError, setup.SetupError)):
                setup.validate_settings(configuration(**{name: value}))

    def test_save_removes_stale_user_password_file_and_keeps_password_literal(self):
        registry = MemoryRegistry(user={
            'TRANSLATOR_PASSWORD_FILE': ('C:\\missing\\password.txt', 2),
            'UNRELATED': ('keep-me', 2),
        })
        values = configuration()
        setup.persist_settings(registry, values)
        self.assertNotIn('TRANSLATOR_PASSWORD_FILE', registry.user)
        self.assertEqual(registry.user['TRANSLATOR_PASSWORD'], (values['TRANSLATOR_PASSWORD'], 1))
        self.assertEqual(registry.user['UNRELATED'], ('keep-me', 2))
        with patch.dict(os.environ, setup.load_environment(registry, {}), clear=True):
            self.assertEqual(configured_password(), values['TRANSLATOR_PASSWORD'])

    def test_system_password_file_blocks_setup_without_mutating_user_values(self):
        registry = MemoryRegistry(user={'PORT': ('9000', 1)}, system={
            'TRANSLATOR_PASSWORD_FILE': ('C:\\system\\password.txt', 1),
        })
        before = copy.deepcopy(registry.user)
        with self.assertRaises(setup.SetupError):
            setup.persist_settings(registry, configuration())
        self.assertEqual(registry.user, before)
        self.assertEqual(registry.writes, [])

    def test_registry_failure_restores_previous_values_and_types(self):
        registry = MemoryRegistry(user={
            'PORT': ('9000', 2), 'OLLAMA_URL': ('http://old:11434', 2),
            'TRANSLATOR_PASSWORD_FILE': ('%APPDATA%\\translator.secret', 2),
            'UNRELATED': ('preserve', 1),
        }, fail_write=4)
        before = copy.deepcopy(registry.user)
        with self.assertRaises(setup.SetupError):
            setup.persist_settings(registry, configuration())
        self.assertEqual(registry.user, before)

    def test_interrupt_during_registry_write_rolls_back_partial_updates(self):
        registry = MemoryRegistry(user={
            'PORT': ('9000', 2), 'TRANSLATOR_PASSWORD': ('old-synthetic-password', 1),
        }, fail_write=3, failure=KeyboardInterrupt())
        before = copy.deepcopy(registry.user)
        with self.assertRaises(KeyboardInterrupt):
            setup.persist_settings(registry, configuration())
        self.assertEqual(registry.user, before)

    def test_start_reloads_registry_instead_of_stale_parent_process_values(self):
        values = configuration(PORT='8092')
        registry = MemoryRegistry(user={name: (value, 1) for name, value in values.items()},
                                  system={'FILE_RETENTION_HOURS': ('48', 1)})
        registry.user.pop('FILE_RETENTION_HOURS')
        current = setup.load_environment(registry, {
            'TRANSLATOR_PASSWORD': 'stale-password',
            'TRANSLATOR_PASSWORD_FILE': 'stale-file',
            'PORT': '8080', 'FILE_RETENTION_HOURS': '24', 'UNRELATED': 'inherited',
        })
        self.assertEqual(current['PORT'], '8092')
        self.assertEqual(current['FILE_RETENTION_HOURS'], '48')
        self.assertEqual(current['TRANSLATOR_PASSWORD'], values['TRANSLATOR_PASSWORD'])
        self.assertFalse(current.get('TRANSLATOR_PASSWORD_FILE'))
        self.assertEqual(current['UNRELATED'], 'inherited')
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        output = io.StringIO()
        with redirect_stdout(output):
            code = setup.launch_application(current, runner=runner)
        self.assertEqual(code, 0)
        argv = runner.call_args.args[0]
        self.assertEqual(Path(argv[-1]).name, 'server.py')
        self.assertEqual(Path(runner.call_args.kwargs['cwd']).name, 'app')
        self.assertEqual(runner.call_args.kwargs['env']['PORT'], '8092')
        self.assertNotIn(values['TRANSLATOR_PASSWORD'], str(argv))
        self.assertNotIn(values['TRANSLATOR_PASSWORD'], output.getvalue())
        self.assertFalse(runner.call_args.kwargs.get('shell', False))


if __name__ == '__main__':
    unittest.main()
