"""Install required Argos models and prove translation works without network access.

The build invokes normal installation, then --verify-only in a fresh process.
The builder also runs --verify-only with --network none before exporting.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import socket
import sys
from unittest.mock import patch

AVAILABLE_LANGUAGES = {
    "sq": "Albanian",    "ar": "Arabic",      "az": "Azerbaijani",
    "eu": "Basque",      "bn": "Bengali",     "bg": "Bulgarian",
    "ca": "Catalan",     "zt": "Chinese (traditional)", "zh": "Chinese",
    "cs": "Czech",       "da": "Danish",      "nl": "Dutch",
    "en": "English",     "eo": "Esperanto",   "et": "Estonian",
    "fi": "Finnish",     "fr": "French",      "gl": "Galician",
    "de": "German",      "el": "Greek",       "he": "Hebrew",
    "hi": "Hindi",       "hu": "Hungarian",   "id": "Indonesian",
    "ga": "Irish",       "it": "Italian",     "ja": "Japanese",
    "ko": "Korean",      "ky": "Kyrgyz",      "lv": "Latvian",
    "lt": "Lithuanian",  "ms": "Malay",       "nb": "Norwegian",
    "fa": "Persian",     "pl": "Polish",      "pt": "Portuguese",
    "pb": "Portuguese (Brazil)", "ro": "Romanian", "ru": "Russian",
    "sk": "Slovak",      "sl": "Slovenian",   "es": "Spanish",
    "sv": "Swedish",     "tl": "Tagalog",     "th": "Thai",
    "tr": "Turkish",     "uk": "Ukrainian",   "ur": "Urdu",
    "vi": "Vietnamese",
}


def requested_languages(value):
    languages = list(dict.fromkeys(["en", "ro"] + [x.strip().lower() for x in value.split(",") if x.strip()]))
    invalid = sorted(set(languages) - AVAILABLE_LANGUAGES.keys())
    if invalid:
        raise ValueError("Unknown language codes: " + ", ".join(invalid))
    return languages


def required_pairs(languages):
    return {(a, b) for lang in languages if lang != "en"
            for a, b in (("en", lang), (lang, "en"))}


def verify_installed(package, pairs):
    installed = {(p.from_code, p.to_code) for p in package.get_installed_packages()}
    missing = sorted(pairs - installed)
    if missing:
        raise RuntimeError("Missing translation models: " + ", ".join(f"{a}->{b}" for a, b in missing))


def warmup_translations(pairs, translate):
    # Exercise every direction so lazy sentence/tokenizer models are cached too.
    samples = {"en": "Hello world. Thank you.", "ro": "Bună ziua. Mulțumesc."}
    for source, target in sorted(pairs):
        result = translate(samples.get(source, "Hello world."), source, target)
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError(f"Empty warmup translation: {source}->{target}")
        print(f"Translation verified: {source}->{target}")


def deny_network(*args, **kwargs):
    raise RuntimeError("Offline verification attempted a network connection; a model/cache is missing.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("languages", nargs="?")
    parser.add_argument("--verify-only", action="store_true", help="Require cached models and forbid network connections")
    parser.add_argument("--output", type=Path, default=Path("/app/installed_languages.txt"))
    args = parser.parse_args(argv)
    value = args.languages
    if value is None:
        value = args.output.read_text().strip() if args.verify_only else "en,ro"
    languages = requested_languages(value)
    pairs = required_pairs(languages)

    # Import and exercise dependencies under the network guard in verify mode.
    with ExitStack() as stack:
        if args.verify_only:
            stack.enter_context(patch.object(socket.socket, "connect", deny_network))
            stack.enter_context(patch.object(socket.socket, "connect_ex", deny_network))
            stack.enter_context(patch.object(socket, "create_connection", deny_network))
        import argostranslate.package as package
        if not args.verify_only:
            package.update_package_index()
            available = {(p.from_code, p.to_code): p for p in package.get_available_packages()}
            installed = {(p.from_code, p.to_code) for p in package.get_installed_packages()}
            for source, target in sorted(pairs - installed):
                model = available.get((source, target))
                if model is None:
                    raise RuntimeError(f"Required model absent from index: {source}->{target}")
                print(f"Installing {source}->{target}...")
                model.install()  # An installation error must fail the build.
        verify_installed(package, pairs)
        from argostranslate.translate import translate
        warmup_translations(pairs, translate)

    if not args.verify_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(",".join(languages), encoding="utf-8")
        temporary.replace(args.output)
    print("Offline translation verified." if args.verify_only else "Models and sentence caches installed and verified.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
