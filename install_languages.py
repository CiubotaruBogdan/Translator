"""
Download and install Argos Translate language models.

Usage:
    python3 install_languages.py [lang1,lang2,lang3,...]

    - English (en) is always included as pivot language
    - Romanian (ro) is always included as default
    - Additional languages are passed as comma-separated codes
    - All translation pairs go through English as pivot

Example:
    python3 install_languages.py en,ro,fr,de,es
    python3 install_languages.py en,ro   (default: just English + Romanian)
"""
import sys
import argostranslate.package

# ============================================================
# Available languages in Argos Translate
# ============================================================
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

# ============================================================
# Parse language list
# ============================================================
if len(sys.argv) > 1:
    requested = [l.strip().lower() for l in sys.argv[1].split(",") if l.strip()]
else:
    requested = ["en", "ro"]

# Always include English (pivot) and Romanian (default)
if "en" not in requested:
    requested.insert(0, "en")
if "ro" not in requested:
    requested.insert(1, "ro")

# Validate
valid_langs = []
for code in requested:
    if code in AVAILABLE_LANGUAGES:
        valid_langs.append(code)
    else:
        print(f"WARNING: Language code '{code}' not recognized, skipping.")

print(f"Languages to install: {', '.join(f'{c} ({AVAILABLE_LANGUAGES[c]})' for c in valid_langs)}")
print(f"Total: {len(valid_langs)} languages")
print()

# ============================================================
# Build translation pairs (all through English as pivot)
# ============================================================
pairs = set()
for lang in valid_langs:
    if lang != "en":
        pairs.add(("en", lang))  # English -> X
        pairs.add((lang, "en"))  # X -> English

print(f"Translation pairs to install: {len(pairs)}")
for src, tgt in sorted(pairs):
    print(f"  {src} ({AVAILABLE_LANGUAGES.get(src, '?')}) -> {tgt} ({AVAILABLE_LANGUAGES.get(tgt, '?')})")
print()

# ============================================================
# Download and install
# ============================================================
print("Updating package index...")
argostranslate.package.update_package_index()
pkgs = argostranslate.package.get_available_packages()

installed = 0
failed = 0
for src, tgt in sorted(pairs):
    found = False
    for p in pkgs:
        if p.from_code == src and p.to_code == tgt:
            print(f"Installing {src} -> {tgt} ({AVAILABLE_LANGUAGES.get(src, '?')} -> {AVAILABLE_LANGUAGES.get(tgt, '?')})...")
            try:
                p.install()
                installed += 1
                found = True
            except Exception as e:
                print(f"  ERROR: {e}")
                failed += 1
                found = True
            break
    if not found:
        print(f"WARNING: Package {src} -> {tgt} not found in index")
        failed += 1

print()
print(f"Installation complete: {installed} pairs installed, {failed} failed/missing")

# Save installed languages to a file for runtime reference
langs_file = "/app/installed_languages.txt" if __name__ == "__main__" else "installed_languages.txt"
try:
    with open(langs_file, "w") as f:
        f.write(",".join(valid_langs))
    print(f"Language list saved to {langs_file}")
except:
    pass

print("Done!")
