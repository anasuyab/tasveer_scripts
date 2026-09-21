#!/usr/bin/env python3
"""
Walk a directory tree (max depth 3 by default), find ASSETMAP / ASSETMAP.xml
files, read the AnnotationText from each, rename the folder containing the
ASSETMAP to that AnnotationText, then run verify_dcp.py on it and log the result.

Usage:
    python3 rename_and_verify_dcp.py /path/to/root
    python3 rename_and_verify_dcp.py /path/to/root --dry-run
"""

import argparse
import html
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

DEFAULT_VERIFY_SCRIPT = "/Volumes/HOUSE2/House2/verify_dcp.py"
ASSETMAP_NAMES = {"assetmap", "assetmap.xml"}      # compared case-insensitively
INVALID_FS_CHARS = re.compile(r'[/\\:\x00-\x1f]')  # characters replaced with "_"
SUCCESS_RE = re.compile(r"\bSUCCESS\b")

log = logging.getLogger("dcp")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def setup_logging(log_path: Path) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def local_name(tag: str) -> str:
    """Strip an XML namespace: '{ns}AnnotationText' -> 'AnnotationText'."""
    return tag.rsplit("}", 1)[-1]


def read_annotation_text(assetmap: Path):
    """Return the AnnotationText string from an ASSETMAP file, or None."""
    try:
        root = ET.parse(assetmap).getroot()
        # Prefer the top-level AnnotationText, then fall back to any occurrence.
        for child in root:
            if local_name(child.tag) == "AnnotationText":
                return (child.text or "").strip() or None
        for el in root.iter():
            if local_name(el.tag) == "AnnotationText":
                return (el.text or "").strip() or None
    except ET.ParseError:
        # Malformed XML: fall back to a regex over the raw text.
        text = assetmap.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r"<(?:\w+:)?AnnotationText[^>]*>(.*?)</(?:\w+:)?AnnotationText>",
            text, re.S)
        if m:
            return html.unescape(m.group(1)).strip() or None
    return None


def sanitize_folder_name(name: str) -> str:
    return INVALID_FS_CHARS.sub("_", name).strip().rstrip(".")


def find_assetmaps(root: Path, max_depth: int):
    """
    Return a list of (depth, assetmap_path) for every folder up to max_depth
    levels below root that contains an ASSETMAP or ASSETMAP.xml.
    Only one assetmap is returned per folder.
    """
    results = []
    root_parts = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_parts
        if depth >= max_depth:
            dirnames[:] = []  # don't descend any further
        matches = sorted(f for f in filenames if f.lower() in ASSETMAP_NAMES)
        if matches:
            results.append((depth, Path(dirpath) / matches[0]))
    return results


def run_verify(verify_script: str, folder_name: str, cwd: Path, timeout: int):
    """
    Run verify_dcp.py, answering its folder-name prompt via stdin.
    Returns (is_success, return_code, combined_output).
    """
    try:
        proc = subprocess.run(
            ["python3", verify_script],
            input=folder_name + "\n",
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return bool(SUCCESS_RE.search(output)), proc.returncode, output
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return False, None, out + f"\n[TIMEOUT after {timeout}s]"
    except Exception as e:  # noqa: BLE001
        return False, None, f"[Could not run verify script: {e}]"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument("--max-depth", type=int, default=3,
                    help="Max folder depth below root to search (default: 3)")
    ap.add_argument("--verify-script", default=DEFAULT_VERIFY_SCRIPT,
                    help=f"Path to verify_dcp.py (default: {DEFAULT_VERIFY_SCRIPT})")
    ap.add_argument("--log", type=Path, default=None,
                    help="Log file path (default: dcp_verify_<timestamp>.log in current dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be renamed; don't rename or run verify")
    ap.add_argument("--pass-full-path", action="store_true",
                    help="Give verify_dcp.py the full path instead of just the folder name")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="Seconds to allow each verify run (default: 3600)")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 2

    log_path = args.log or Path(f"dcp_verify_{datetime.now():%Y%m%d_%H%M%S}.log")
    setup_logging(log_path)

    log.info("=" * 70)
    log.info("Root:          %s", root)
    log.info("Max depth:     %d", args.max_depth)
    log.info("Verify script: %s", args.verify_script)
    log.info("Dry run:       %s", args.dry_run)
    log.info("Log file:      %s", log_path.resolve())
    log.info("=" * 70)

    if not args.dry_run and not Path(args.verify_script).is_file():
        log.error("Verify script not found: %s (is the volume mounted?)", args.verify_script)
        return 2

    found = find_assetmaps(root, args.max_depth)
    if not found:
        log.info("No ASSETMAP / ASSETMAP.xml files found.")
        return 0

    # Process deepest folders first so renaming a parent never invalidates
    # the path of a not-yet-processed child.
    found.sort(key=lambda t: t[0], reverse=True)

    counts = {"success": 0, "failed": 0, "skipped": 0}

    for depth, assetmap in found:
        folder = assetmap.parent
        log.info("-" * 70)
        log.info("PROCESSING: %s", assetmap)

        if folder == root:
            log.warning("  SKIPPED: ASSETMAP is in the root folder itself; not renaming root.")
            counts["skipped"] += 1
            continue

        annotation = read_annotation_text(assetmap)
        if not annotation:
            log.error("  SKIPPED: no AnnotationText found in file.")
            counts["skipped"] += 1
            continue
        log.info("  AnnotationText: %s", annotation)

        new_name = sanitize_folder_name(annotation)
        if not new_name:
            log.error("  SKIPPED: AnnotationText is empty after sanitizing.")
            counts["skipped"] += 1
            continue
        if new_name != annotation:
            log.warning("  Invalid characters replaced: '%s' -> '%s'", annotation, new_name)

        new_folder = folder.with_name(new_name)

        # ---- rename ----
        if new_folder == folder:
            log.info("  Folder already named correctly: %s", folder.name)
        elif new_folder.exists():
            log.error("  SKIPPED: target already exists: %s", new_folder)
            counts["skipped"] += 1
            continue
        elif args.dry_run:
            log.info("  [DRY RUN] would rename: %s -> %s", folder.name, new_name)
            continue
        else:
            try:
                folder.rename(new_folder)
                log.info("  RENAMED: %s -> %s", folder.name, new_name)
            except OSError as e:
                log.error("  SKIPPED: rename failed: %s", e)
                counts["skipped"] += 1
                continue

        if args.dry_run:
            log.info("  [DRY RUN] would verify: %s", new_name)
            continue

        # ---- verify ----
        verify_input = str(new_folder) if args.pass_full_path else new_name
        log.info("  Running verify_dcp.py with input: %s", verify_input)
        ok, rc, output = run_verify(args.verify_script, verify_input,
                                    cwd=new_folder.parent, timeout=args.timeout)

        if ok:
            counts["success"] += 1
            log.info("  RESULT: SUCCESS  [%s]", new_name)
        else:
            counts["failed"] += 1
            log.error("  RESULT: NOT SUCCESS (exit code: %s)  [%s]", rc, new_name)
            log.error("  --- verify_dcp.py output ---")
            for line in output.rstrip().splitlines():
                log.error("  | %s", line)
            log.error("  --- end output ---")

    log.info("=" * 70)
    log.info("DONE. Processed: %d | SUCCESS: %d | Not success: %d | Skipped: %d",
             len(found), counts["success"], counts["failed"], counts["skipped"])
    log.info("=" * 70)
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
