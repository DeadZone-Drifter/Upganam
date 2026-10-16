"""
Core Archive and Manifest Management for Upganam.
Handles packaging staged files into ZIP containers, manifest generation,
SHA-256 checksumming, archive inspection, and in-memory integrity verification.
"""
import os
import json
import zipfile
import datetime
import hashlib

MANIFEST_FILENAME = "manifest.json"
HASH_CHUNK_SIZE = 1024 * 1024  # 1 MB chunk for streaming hash calculation

def compute_file_sha256(file_path: str) -> str:
    """Compute the SHA-256 hex digest of a file on disk."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def compute_stream_sha256(stream) -> str:
    """Compute the SHA-256 hex digest of a readable byte stream."""
    hasher = hashlib.sha256()
    while True:
        chunk = stream.read(HASH_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()

def create_manifest(entries: list[dict], serial: str = None, backup_format: str = "zip", backup_type: str = "storage") -> dict:
    """
    Generate structured manifest dictionary storing metadata, entries, and format info.
    """
    total_bytes = sum(e.get("size", 0) for e in entries if not e.get("is_dir", False))
    return {
        "version": "1.1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_serial": serial or "unknown",
        "format": backup_format,
        "backup_type": backup_type,
        "total_items": len(entries),
        "total_bytes": total_bytes,
        "entries": entries,
        "checksums": {}
    }

def build_zip_archive(staging_dir: str, output_zip_path: str, manifest_data: dict, progress_callback=None):
    """
    Pack all files inside staging_dir along with SHA-256 checksums into a ZIP archive.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_zip_path)), exist_ok=True)

    all_files = []
    for root_dir, _, files in os.walk(staging_dir):
        for f in files:
            if f == MANIFEST_FILENAME:
                continue
            all_files.append(os.path.join(root_dir, f))

    # Calculate SHA-256 checksums for each staged file
    checksums = {}
    for full_path in all_files:
        arc_name = os.path.relpath(full_path, staging_dir).replace("\\", "/")
        checksums[arc_name] = compute_file_sha256(full_path)

    manifest_data["checksums"] = checksums

    # Write manifest temporarily into staging directory
    manifest_path = os.path.join(staging_dir, MANIFEST_FILENAME)
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2)

    all_files_to_pack = [manifest_path] + all_files
    total_files = len(all_files_to_pack)

    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, full_path in enumerate(all_files_to_pack, 1):
            arc_name = os.path.relpath(full_path, staging_dir).replace("\\", "/")
            zf.write(full_path, arc_name)
            if progress_callback and total_files > 0:
                progress_callback(idx, total_files, arc_name)

    # Clean up manifest file from staging
    if os.path.exists(manifest_path):
        try:
            os.remove(manifest_path)
        except OSError:
            pass

def inspect_archive(zip_path: str) -> list[dict]:
    """
    Inspect a ZIP archive and return a normalized list of item dictionaries.
    Returns: list[dict] with keys: 'name', 'original_path', 'size', 'is_dir', 'type', 'package_name'
    """
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"Archive file not found: '{zip_path}'")

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        manifest = None
        if MANIFEST_FILENAME in namelist:
            try:
                manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
            except Exception:
                manifest = None

        parsed_items = []
        if manifest and "entries" in manifest:
            info_map = {zi.filename: zi.file_size for zi in zf.infolist()}
            for entry in manifest["entries"]:
                rel_p = entry.get("rel_path", "")
                if rel_p == MANIFEST_FILENAME:
                    continue
                size = info_map.get(rel_p, entry.get("size", 0))
                parsed_items.append({
                    "name": rel_p,
                    "original_path": entry.get("remote_path", "/storage/emulated/0/" + rel_p),
                    "size": size,
                    "is_dir": entry.get("is_dir", False),
                    "type": entry.get("type", "file"),
                    "package_name": entry.get("package_name", "")
                })
        else:
            for zi in zf.infolist():
                if zi.filename == MANIFEST_FILENAME:
                    continue
                is_apk = zi.filename.lower().endswith(".apk")
                parsed_items.append({
                    "name": zi.filename,
                    "original_path": "/storage/emulated/0/" + zi.filename,
                    "size": zi.file_size,
                    "is_dir": zi.is_dir(),
                    "type": "apk" if is_apk else "file",
                    "package_name": ""
                })

    return parsed_items

def verify_archive_integrity(zip_path: str, progress_callback=None) -> dict:
    """
    Verify SHA-256 integrity of all files inside a ZIP archive against manifest.json.
    Computes checksums on the fly directly from the ZIP stream (zero disk extraction).
    Returns dict: {
        "success": bool,
        "total": int,
        "passed": int,
        "failed": list[tuple[str, str, str]],  # (file, expected, actual)
        "missing": list[str]
    }
    """
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"Archive file not found: '{zip_path}'")

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = set(zf.namelist())
        if MANIFEST_FILENAME not in namelist:
            return {
                "success": False,
                "total": 0,
                "passed": 0,
                "failed": [],
                "missing": [MANIFEST_FILENAME],
                "error": "No manifest.json found in backup archive."
            }

        try:
            manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
        except Exception as e:
            return {
                "success": False,
                "total": 0,
                "passed": 0,
                "failed": [],
                "missing": [],
                "error": f"Failed to parse manifest: {e}"
            }

        checksums = manifest.get("checksums", {})
        if not checksums:
            # Fallback: check if checksums were attached directly to entries
            for entry in manifest.get("entries", []):
                p = entry.get("rel_path")
                c = entry.get("sha256")
                if p and c:
                    checksums[p] = c

        if not checksums:
            return {
                "success": False,
                "total": 0,
                "passed": 0,
                "failed": [],
                "missing": [],
                "error": "No SHA-256 checksums recorded in this backup manifest."
            }

        total = len(checksums)
        passed = 0
        failed = []
        missing = []

        for idx, (arc_name, expected_sha) in enumerate(checksums.items(), 1):
            if arc_name not in namelist:
                missing.append(arc_name)
                continue

            with zf.open(arc_name, "r") as stream:
                actual_sha = compute_stream_sha256(stream)

            if actual_sha.lower() == expected_sha.lower():
                passed += 1
            else:
                failed.append((arc_name, expected_sha, actual_sha))

            if progress_callback:
                progress_callback(idx, total, arc_name)

        is_clean = (len(failed) == 0 and len(missing) == 0)
        return {
            "success": is_clean,
            "total": total,
            "passed": passed,
            "failed": failed,
            "missing": missing,
            "error": None
        }

def extract_file_from_archive(zip_path: str, member_name: str, target_dir: str) -> str:
    """
    Extract a single member from a ZIP archive into target_dir.
    Returns the absolute path to the extracted file.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        return zf.extract(member_name, path=target_dir)
