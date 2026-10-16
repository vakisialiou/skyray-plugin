"""Builds the add-on's zips from src/, one per server, into the repo root or
the folder given:

    python3 build.py [out_dir]

Each zip holds the whole of src/ as skyray/, the module Blender installs, so
installing one replaces another. They differ only in the add-on's name
(bl_info's, e.g. "Skyray (dev)") and its default server
(constants.DEFAULT_SERVER_URL); SKYRAY_SERVER_URL and the developer
preference still override the server. The source tree is the local build.
"""

import json
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "src"

# zip → (the add-on's name, its default server)
BUILDS = {
    "skyray-local.zip": ("Skyray (local)", "http://localhost:3500"),
    "skyray-dev.zip": ("Skyray (dev)", "https://dev-gateway.skyray.app"),
    "skyray-prod.zip": ("Skyray", "https://gateway.skyray.app"),
}

# src file → the one line a build sets: what comes before its string, and after.
LINES = {
    "__init__.py": r'^(    "name": )"[^"]*"(,)$',
    "constants.py": r'^(DEFAULT_SERVER_URL = )"[^"]*"()$',
}


def _set(text: str, file: str, value: str) -> str:
    """`text` with its one LINES[file] line holding `value` as its string."""
    result, count = re.subn(
        LINES[file],
        lambda match: match.group(1) + json.dumps(value) + match.group(2),
        text,
        flags=re.MULTILINE,
    )
    if count != 1:
        sys.exit(f"build.py: {file} needs exactly one line matching {LINES[file]!r}, found {count}")
    return result


def build(out: pathlib.Path, zip_name: str, name: str, server: str) -> None:
    values = {"__init__.py": name, "constants.py": server}
    # Set before the zip opens: a source the build can't set leaves no zip half-written.
    texts = {file: _set((SRC / file).read_text(encoding="utf-8"), file, value) for file, value in values.items()}
    with zipfile.ZipFile(out / zip_name, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in [SRC, *sorted(SRC.rglob("*"))]:
            if "__pycache__" in path.parts:
                continue
            file = path.relative_to(SRC).as_posix()
            arcname = "skyray" / path.relative_to(SRC)
            if file not in texts:
                archive.write(path, arcname)
                continue
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, texts[file])


def main() -> None:
    out = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    out.mkdir(parents=True, exist_ok=True)
    for zip_name, (name, server) in BUILDS.items():
        build(out, zip_name, name, server)
        print(f"{out / zip_name}: {name}, {server}")


if __name__ == "__main__":
    main()
