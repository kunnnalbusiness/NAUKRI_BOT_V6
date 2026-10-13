r"""
Creates a production-ready zip archive of the project outside the project folder:
Destination: c:\Users\Sakshi\Desktop\Naukri\v14\naukri-job.zip
Excludes regenerable clutter: node_modules, __pycache__, .pytest_cache, .git
"""
import os
import sys
import zipfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEST_ZIP = PROJECT_ROOT.parent.parent / "naukri-job.zip"

EXCLUDE_DIRS = {
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    ".idea",
    ".vscode",
    ".agents",
    ".mimosa",
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
}

def create_zip():
    print(f"Creating zip archive of:\n  Source: {PROJECT_ROOT}\n  Destination: {DEST_ZIP}")
    t0 = time.time()

    file_count = 0
    total_bytes = 0

    # Ensure parent dir exists
    DEST_ZIP.parent.mkdir(parents=True, exist_ok=True)

    # Use ZIP_DEFLATED with compression level 6
    with zipfile.ZipFile(DEST_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(PROJECT_ROOT):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            rel_root = Path(root).relative_to(PROJECT_ROOT)

            for file in files:
                file_path = Path(root) / file
                ext = file_path.suffix.lower()

                if ext in EXCLUDE_EXTS:
                    continue

                # Relative path inside zip, prefixed with 'naukri-job/'
                arcname = Path("naukri-job") / rel_root / file

                try:
                    zf.write(file_path, arcname=str(arcname))
                    file_count += 1
                    total_bytes += file_path.stat().st_size
                except Exception as e:
                    print(f"Warning: could not add {file_path}: {e}")

    elapsed = time.time() - t0
    zip_size_mb = DEST_ZIP.stat().st_size / (1024 * 1024)
    orig_size_mb = total_bytes / (1024 * 1024)

    print("=" * 70)
    print("ZIP CREATION SUCCESSFUL!")
    print(f"  Archive Path : {DEST_ZIP}")
    print(f"  Files Added  : {file_count:,}")
    print(f"  Original Size: {orig_size_mb:.2f} MB")
    print(f"  Zip Size     : {zip_size_mb:.2f} MB")
    print(f"  Time Taken   : {elapsed:.2f} seconds")
    print("=" * 70)

if __name__ == "__main__":
    create_zip()
