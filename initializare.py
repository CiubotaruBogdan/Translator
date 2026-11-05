"""Interactive Windows setup for running app/server.py directly with Python.

Only the current user's environment is changed. This helper needs no third-party
packages, never puts the password in a shell command, and does not install engines.
"""
import argparse
import getpass
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import warnings


ROOT = Path(__file__).resolve().parent
PASSWORD_NAME = "TRANSLATOR_PASSWORD"
FILE_NAME = "TRANSLATOR_PASSWORD_FILE"
DEFAULTS = {
    "TRANSLATE_ENGINE": "ollama",
    "OLLAMA_URL": "http://127.0.0.1:11434",
    "OLLAMA_MODEL": "translategemma",
    "LIBRETRANSLATE_URL": "http://127.0.0.1:5000",
    "PORT": "8080",
    "INSTALLED_LANGUAGES": "en,ro",
    "FILE_RETENTION_HOURS": "24",
    "TRANSLATOR_ALLOWED_ENGINE_URLS": "",
}
MANAGED_NAMES = (*DEFAULTS, PASSWORD_NAME, FILE_NAME)


class SetupError(Exception):
    """A user-facing error whose message contains no credential values."""


def validate_origin(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 33 for c in value):
        raise ValueError("Adresa trebuie sa fie o origine HTTP(S), fara spatii.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(c in value for c in ("?", "#", "\\"))):
            raise ValueError
        host = parsed.hostname.lower()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("Adresa trebuie sa fie HTTP(S), fara cale, parametri sau parola, cu port valid.") from None
    if host == "localhost":
        host = "127.0.0.1"
    if ":" in host:
        host = "[" + host + "]"
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None and port != default_port else "")


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 4096 or any(c in value for c in ("\r", "\n", "\0")):
        raise ValueError("Parola trebuie sa aiba intre 12 si 4096 de caractere, fara newline sau NUL.")
    return value


def validate_setting(name, value):
    if name == PASSWORD_NAME:
        return validate_password(value)
    value = value.strip()
    if name in ("OLLAMA_URL", "LIBRETRANSLATE_URL"):
        return validate_origin(value)
    if name == "TRANSLATE_ENGINE":
        if value.lower() not in ("ollama", "libretranslate"):
            raise ValueError("Alege ollama sau libretranslate.")
        return value.lower()
    if name == "PORT":
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ValueError("Portul trebuie sa fie un numar intre 1 si 65535.")
        return str(int(value))
    if name == "FILE_RETENTION_HOURS":
        try:
            hours = float(value)
        except ValueError:
            hours = 0
        if not math.isfinite(hours) or hours < 1:
            raise ValueError("Retentia trebuie sa fie un numar de ore de minimum 1.")
        return str(int(hours)) if hours.is_integer() else str(hours)
    if name == "OLLAMA_MODEL":
        if not value or len(value) > 256 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Introdu numele modelului Ollama, fara spatii (ex. translategemma).")
        return value
    if name == "INSTALLED_LANGUAGES":
        codes = [code.strip().lower() for code in value.split(",")]
        if not codes or any(not re.fullmatch(r"[a-z]{2,3}", code) for code in codes):
            raise ValueError("Introdu codurile limbilor separate prin virgula (ex. en,ro).")
        return ",".join(dict.fromkeys(codes))
    if name == "TRANSLATOR_ALLOWED_ENGINE_URLS":
        if not value:
            return ""
        return ",".join(dict.fromkeys(validate_origin(part.strip()) for part in value.split(",")))
    raise ValueError("Setare necunoscuta.")


def validate_settings(settings):
    return {name: validate_setting(name, settings[name]) for name in (*DEFAULTS, PASSWORD_NAME)}


def masked_password(prompt):
    # getpass otherwise falls back to visible stdin if no secure console exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SetupError("Nu exista o consola pentru parola mascata. Porneste initializare.bat.") from None


def ask_yes_no(prompt, input_fn=input, default=False):
    while True:
        answer = input_fn(prompt + (" [D/n]: " if default else " [d/N]: ")).strip().lower()
        if not answer:
            return default
        if answer in ("d", "da", "y", "yes"):
            return True
        if answer in ("n", "nu", "no"):
            return False
        print("Raspunde cu d (da) sau n (nu).")


def collect_settings(existing, input_fn=input, secret_fn=masked_password):
    password = existing.get(PASSWORD_NAME, "")
    try:
        validate_password(password)
        keep = ask_yes_no("Pastrezi parola comuna existenta?", input_fn, default=True)
    except ValueError:
        keep = False
    if not keep:
        while True:
            password = secret_fn("Parola pentru utilizatorul translator (minimum 12 caractere): ")
            confirmation = secret_fn("Confirma parola: ")
            try:
                validate_password(password)
                if password != confirmation:
                    raise ValueError("Parolele nu coincid.")
                break
            except ValueError as exc:
                print(str(exc))
    prompts = {
        "TRANSLATE_ENGINE": "Motor implicit (ollama/libretranslate)",
        "OLLAMA_URL": "Adresa Ollama (necesara si pentru verificarea traducerilor)",
        "OLLAMA_MODEL": "Numele modelului de traducere instalat in Ollama",
        "LIBRETRANSLATE_URL": "Adresa LibreTranslate",
        "PORT": "Portul interfetei web",
        "INSTALLED_LANGUAGES": "Limbi instalate in LibreTranslate, separate prin virgula",
        "FILE_RETENTION_HOURS": "Retentie fisiere inactive, in ore (curatare zilnica)",
        "TRANSLATOR_ALLOWED_ENGINE_URLS": "Alte adrese de motoare permise, separate prin virgula (- pentru niciuna)",
    }
    settings = {PASSWORD_NAME: password}
    for name, fallback in DEFAULTS.items():
        try:
            default = validate_setting(name, existing.get(name, fallback))
        except (ValueError, AttributeError):
            default = fallback
        while True:
            entered = input_fn(f"{prompts[name]} [{default or 'niciuna'}]: ").strip()
            value = entered or default
            if name == "TRANSLATOR_ALLOWED_ENGINE_URLS" and entered == "-":
                value = ""
            try:
                settings[name] = validate_setting(name, value)
                break
            except ValueError as exc:
                print(str(exc))
    return settings


class WindowsEnvironment:
    def __init__(self):
        import winreg
        self.registry = winreg

    def _read(self, hive, path, name):
        try:
            with self.registry.OpenKey(hive, path) as key:
                return self.registry.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def read_user(self, name):
        return self._read(self.registry.HKEY_CURRENT_USER, "Environment", name)

    def read_system(self, name):
        return self._read(self.registry.HKEY_LOCAL_MACHINE,
                          r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", name)

    def write_user(self, name, value):
        self.restore_user(name, None if value is None else (value, self.registry.REG_SZ))

    def restore_user(self, name, entry):
        with self.registry.CreateKeyEx(self.registry.HKEY_CURRENT_USER, "Environment", 0,
                                       self.registry.KEY_SET_VALUE) as key:
            if entry is None:
                try:
                    self.registry.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                self.registry.SetValueEx(key, name, 0, entry[1], entry[0])


def check_file_conflict(registry):
    entry = registry.read_system(FILE_NAME)
    if entry and entry[0]:
        raise SetupError("TRANSLATOR_PASSWORD_FILE exista in System variables si are prioritate. "
                         "Elimina acea variabila din System variables daca alegi parola directa, "
                         "apoi reia initializarea. Scriptul nu modifica System variables.")


def persist_settings(registry, settings):
    settings = validate_settings(settings)
    check_file_conflict(registry)
    updates = {**settings, FILE_NAME: None}
    before = {name: registry.read_user(name) for name in updates}
    try:
        for name, value in updates.items():
            registry.write_user(name, value)
    except BaseException as error:
        restored = True
        for name, entry in before.items():
            try:
                registry.restore_user(name, entry)
            except Exception:
                restored = False
        if isinstance(error, (KeyboardInterrupt, SystemExit)) and restored:
            raise
        message = "Salvarea variabilelor a esuat. "
        message += ("Valorile anterioare au fost restaurate." if restored else
                    "Restaurarea este incompleta; verifica User variables inainte de pornire.")
        raise SetupError(message) from None


def load_environment(registry, base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    for name in MANAGED_NAMES:
        entry = registry.read_user(name)
        if entry is None:
            entry = registry.read_system(name)
        if entry is None:
            env.pop(name, None)  # Remove stale inherited credentials and addresses.
        elif isinstance(entry[0], str):
            env[name] = entry[0]
        else:
            raise SetupError("O variabila a aplicatiei nu este text. Reia initializare.bat.")
    for name, value in DEFAULTS.items():
        env.setdefault(name, value)
    return env


def notify_environment():
    import ctypes
    try:
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                         ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                         ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_void_p
        result = ctypes.c_size_t()
        return bool(send(0xFFFF, 0x001A, 0, "Environment", 2, 2000, ctypes.byref(result)))
    except (AttributeError, OSError):
        return False


def launch_application(env, runner=subprocess.run):
    if env.get(FILE_NAME):
        raise SetupError("TRANSLATOR_PASSWORD_FILE este inca setata. Reia initializare.bat "
                         "pentru configurarea parolei directe.")
    try:
        settings = validate_settings(env)
    except (KeyError, ValueError):
        raise SetupError("Configuratia lipseste sau este invalida. Ruleaza initializare.bat.") from None
    child_env = {**env, **settings}
    child_env.pop(FILE_NAME, None)
    server = ROOT / "app" / "server.py"
    if not server.is_file():
        raise SetupError("Lipseste app/server.py. Pastreaza scripturile in directorul proiectului.")
    print(f"Deschide http://localhost:{settings['PORT']} in browser. Utilizator: translator.")
    print("Motorul ales trebuie pornit separat. Opreste aplicatia cu Ctrl+C.")
    return runner([sys.executable, str(server)], cwd=str(server.parent), env=child_env).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true", help="Porneste aplicatia cu variabilele salvate, fara intrebari.")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("Acest utilitar este pentru Windows. Nu au fost modificate variabile.", file=sys.stderr)
        return 1
    if sys.version_info < (3, 11):
        print("Este necesar Python 3.11 sau mai nou.", file=sys.stderr)
        return 1
    try:
        registry = WindowsEnvironment()
        if args.start:
            return launch_application(load_environment(registry))
        print("Initializare Translator pentru pornire directa cu Python.")
        print("Variabilele vor fi salvate in User variables, pentru contul Windows curent.")
        print("Parola este mascata la introducere, dar ramane stocata in profilul Windows.")
        print("Python, dependentele si motoarele/modelele trebuie instalate separat; vezi README.md.")
        check_file_conflict(registry)
        if registry.read_user(FILE_NAME):
            print("TRANSLATOR_PASSWORD_FILE din User variables va fi eliminata; se va folosi parola directa.")
        settings = collect_settings(load_environment(registry))
        persist_settings(registry, settings)
        notify_environment()
        print("Configuratia a fost salvata. Nicio parola nu este afisata sau scrisa in repository.")
        print("La urmatoarea utilizare porneste pornire.bat; inchide mai intai orice server deja pornit.")
        if ask_yes_no("Pornesti aplicatia acum?", default=True):
            return launch_application(load_environment(registry))
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nOperatie intrerupta.")
        return 130
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Operatia Windows a esuat. Verifica accesul la profil si instalarea Python.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
