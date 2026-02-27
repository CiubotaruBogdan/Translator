"""Download and install Argos Translate language models."""
import argostranslate.package

print("Updating package index...")
argostranslate.package.update_package_index()

pkgs = argostranslate.package.get_available_packages()

pairs = [
    ("en", "ro"), ("ro", "en"),
    ("en", "fr"), ("fr", "en"),
    ("ro", "fr"), ("fr", "ro"),
]

for src, tgt in pairs:
    found = False
    for p in pkgs:
        if p.from_code == src and p.to_code == tgt:
            print(f"Installing {src} -> {tgt}...")
            p.install()
            found = True
            break
    if not found:
        print(f"WARNING: {src} -> {tgt} not found directly")

print("Language models installed successfully!")
