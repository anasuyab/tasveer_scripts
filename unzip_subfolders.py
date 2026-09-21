#!/usr/bin/env python3
"""
Recursively walk a folder, and in every sub folder that contains a .zip file,
extract the zip(s) in place using the system `unzip` command:

    unzip <file.zip> -x "__MACOSX/*" "*/._*" "._*" -d <folder containing the zip>

Sub folders with no zip file are skipped (and logged).
Errors and skipped folders are written to a log file.

Usage:
    python unzip_subfolders.py /path/to/folder
    python unzip_subfolders.py /path/to/folder --max-depth 4 --log my_run.log
    python unzip_subfolders.py /path/to/folder --dry-run
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Patterns passed to unzip's -x option. We pass them as separate list items
# (no shell), so they reach unzip literally and unzip does the matching itself.
EXCLUDE_PATTERNS = ["__MACOSX/*", "*/._*", "._*"]


def setup_logging(log_path: Path, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("unzip_subfolders")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def find_folders(root: Path, max_depth: int, include_root: bool, log: logging.Logger):
    """
    Return every folder under `root` down to `max_depth` levels.
    Depth 1 = immediate sub folders of root, depth 2 = their sub folders, etc.
    The list is built up front so folders created by extraction are not visited.
    """
    folders = []

    def on_error(err: OSError):
        log.error("Cannot read directory %s: %s", err.filename, err)

    for dirpath, dirnames, _ in os.walk(root, onerror=on_error):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)

        # Never descend into macOS resource-fork junk folders
        dirnames[:] = sorted(d for d in dirnames if d != "__MACOSX")
        if depth >= max_depth:
            dirnames[:] = []

        if depth == 0 and not include_root:
            continue
        folders.append(current)

    return folders


def find_zips(folder: Path, log: logging.Logger):
    """Zip files directly inside `folder` (not recursive), ignoring ._ files."""
    try:
        return sorted(
            p
            for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".zip"
            and not p.name.startswith("._")
        )
    except OSError as e:
        log.error("Cannot list %s: %s", folder, e)
        return []


def unzip_file(zip_path: Path, overwrite: bool, dry_run: bool, log: logging.Logger) -> bool:
    """Run unzip on one zip file, extracting next to it. Returns True on success."""
    dest = zip_path.parent
    cmd = [
        "unzip",
        "-o" if overwrite else "-n",  # never prompt: overwrite, or skip existing files
        str(zip_path),
        "-x",
        *EXCLUDE_PATTERNS,
        "-d",
        str(dest),
    ]

    if dry_run:
        log.info("[DRY RUN] would run: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
        return True

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # make sure unzip can never hang waiting for input
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError as e:
        log.error("Failed to run unzip on %s: %s", zip_path, e)
        return False

    stdout, stderr = result.stdout.strip(), result.stderr.strip()

    # unzip exit codes: 0 = ok, 1 = ok but with warnings, anything else = error
    if result.returncode == 0:
        log.info("Extracted: %s", zip_path)
        log.debug("unzip output for %s:\n%s", zip_path, stdout)
        return True
    if result.returncode == 1:
        log.warning("Extracted with warnings: %s\n%s", zip_path, "\n".join(filter(None, [stdout, stderr])))
        return True

    log.error(
        "unzip failed (exit code %d) for %s\n%s",
        result.returncode,
        zip_path,
        "\n".join(filter(None, [stdout, stderr])),
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unzip any zip file found in the sub folders of a directory, in place."
    )
    parser.add_argument("folder", type=Path, help="Root folder to scan")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="How many levels of sub folders to search (default: 3)",
    )
    parser.add_argument(
        "--include-root",
        action="store_true",
        help="Also look for zip files directly inside the root folder",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files when extracting (default: skip files that already exist)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path(f"unzip_log_{datetime.now():%Y%m%d_%H%M%S}.log"),
        help="Log file path (default: unzip_log_<timestamp>.log in the current directory)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without extracting")
    parser.add_argument("--verbose", action="store_true", help="Also write unzip's normal output to the log")
    args = parser.parse_args()

    log = setup_logging(args.log, args.verbose)

    root = args.folder.expanduser().resolve()
    if not root.is_dir():
        log.error("Not a directory: %s", root)
        return 2
    if shutil.which("unzip") is None:
        log.error("The 'unzip' command was not found on this system's PATH.")
        return 2

    log.info("Scanning %s (max depth %d)%s", root, args.max_depth, " [DRY RUN]" if args.dry_run else "")

    folders = find_folders(root, args.max_depth, args.include_root, log)

    n_folders = len(folders)
    n_skipped = 0
    n_zips = 0
    n_ok = 0
    n_failed = 0

    for folder in folders:
        zips = find_zips(folder, log)
        if not zips:
            n_skipped += 1
            log.info("SKIPPED (no zip file): %s", folder)
            continue

        for zip_path in zips:
            n_zips += 1
            if unzip_file(zip_path, args.overwrite, args.dry_run, log):
                n_ok += 1
            else:
                n_failed += 1

    log.info("-" * 60)
    log.info("Folders checked:        %d", n_folders)
    log.info("Folders skipped:        %d (no zip file)", n_skipped)
    log.info("Zip files found:        %d", n_zips)
    log.info("Extracted successfully: %d", n_ok)
    log.info("Failed:                 %d", n_failed)
    log.info("Log written to: %s", args.log.resolve())

    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
