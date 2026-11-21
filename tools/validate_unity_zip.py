#!/usr/bin/env python3
"""Validate a Unity project ZIP without extracting or executing project code."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
import zipfile
from pathlib import Path

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_METADATA_BYTES = 1024 * 1024


def _normalise_member(info: zipfile.ZipInfo) -> str:
    """Return a safe, normalised ZIP path or raise ValueError."""
    raw = info.filename
    if not raw or "\x00" in raw:
        raise ValueError("Archive contains an empty or invalid path.")

    name = raw.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError("Archive contains an absolute path.")

    parts = name.split("/")
    if info.is_dir() and parts and parts[-1] == "":
        parts.pop()
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Archive contains an unsafe path.")

    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise ValueError("Archive contains a symbolic link.")

    return "/".join(parts)


def validate_unity_zip(zip_path: Path) -> dict[str, object]:
    """Return a JSON-serialisable validation report for a Unity project ZIP."""
    errors: list[str] = []
    warnings: list[str] = []
    result: dict[str, object] = {
        "ok": False,
        "project_root": None,
        "unity_version": None,
        "errors": errors,
        "warnings": warnings,
    }

    try:
        if not zip_path.is_file():
            errors.append("The selected ZIP file was not found.")
            return result
        if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
            errors.append("The ZIP exceeds the 500 MB upload limit.")
            return result

        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            if not entries:
                errors.append("The ZIP is empty.")
                return result
            if len(entries) > MAX_ENTRIES:
                errors.append("The ZIP contains too many files.")
                return result

            total_uncompressed = 0
            files: dict[str, zipfile.ZipInfo] = {}
            seen: set[str] = set()

            for info in entries:
                name = _normalise_member(info)
                if name in seen:
                    errors.append("The ZIP contains duplicate file paths.")
                    break
                seen.add(name)

                if info.flag_bits & 0x1:
                    errors.append("Encrypted ZIP entries are not supported.")
                    break

                total_uncompressed += info.file_size
                if total_uncompressed > MAX_EXPANDED_BYTES:
                    errors.append("The ZIP expands beyond the 2 GB safety limit.")
                    break

                if not info.is_dir():
                    files[name] = info

            if errors:
                return result

            version_paths = [
                name
                for name in files
                if name == "ProjectSettings/ProjectVersion.txt"
                or name.endswith("/ProjectSettings/ProjectVersion.txt")
            ]
            if len(version_paths) != 1:
                errors.append(
                    "Expected exactly one ProjectSettings/ProjectVersion.txt file."
                )
                return result

            version_path = version_paths[0]
            suffix = "ProjectSettings/ProjectVersion.txt"
            root = version_path[: -len(suffix)].rstrip("/")
            prefix = f"{root}/" if root else ""
            result["project_root"] = root or "."

            assets_prefix = f"{prefix}Assets/"
            if not any(name.startswith(assets_prefix) for name in files):
                errors.append("The project is missing files inside its Assets folder.")

            manifest_path = f"{prefix}Packages/manifest.json"
            manifest_info = files.get(manifest_path)
            if manifest_info is None:
                errors.append("The project is missing Packages/manifest.json.")
            elif manifest_info.file_size > MAX_METADATA_BYTES:
                errors.append("Packages/manifest.json is unexpectedly large.")
            else:
                try:
                    manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
                    if not isinstance(manifest, dict):
                        errors.append("Packages/manifest.json must contain a JSON object.")
                except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
                    errors.append("Packages/manifest.json is not valid UTF-8 JSON.")

            version_info = files.get(version_path)
            if version_info is None or version_info.file_size > MAX_METADATA_BYTES:
                errors.append("ProjectVersion.txt is missing or unexpectedly large.")
            else:
                try:
                    version_text = archive.read(version_info).decode("utf-8", errors="replace")
                    match = re.search(
                        r"^m_EditorVersion:\s*([^\s]+)", version_text, re.MULTILINE
                    )
                    if match:
                        result["unity_version"] = match.group(1)
                    else:
                        errors.append("Could not read the Unity editor version.")
                except (RuntimeError, zipfile.BadZipFile):
                    errors.append("Could not read ProjectVersion.txt.")

            if not any(
                name.startswith(assets_prefix) and name.endswith(".unity")
                for name in files
            ):
                warnings.append(
                    "No Unity scene was found under Assets; the project may not be playable yet."
                )

    except zipfile.BadZipFile:
        errors.append("The selected file is not a valid ZIP archive.")
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Could not safely inspect the ZIP: {exc}")

    result["ok"] = not errors
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether a ZIP contains a basic Unity project structure."
    )
    parser.add_argument("zip_file", type=Path, help="Path to the Unity project ZIP")
    args = parser.parse_args()

    result = validate_unity_zip(args.zip_file)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
