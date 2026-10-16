"""
Core Archive and Manifest Management for Upganam.
Handles packaging staged files into ZIP containers, manifest generation,
archive inspection, and selective item extraction.
"""
import os
import json
import zipfile
import datetime

MANIFEST_FILENAME = "manifest.json"

def create_manifest(entries: list[dict], serial: str = None, backup_format: str = "zip") -> dict:
    """
    Generate structured manifest dictionary storing metadata and file entries.
    """
    total_bytes = sum(e.get("size", 0) for e in entries if not e.get("is_dir", False))
    return {
        "version": "1.0",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_serial": serial or "unknown",
        "format": backup_format,
        "total_items": len(entries),
        "total_bytes": total_bytes,
        "entries": entries
    }

def build_zip_archive(staging_dir: str, output_zip_path: str, manifest_data: dict, progress_callback=None):
    """
    Pack all files inside staging_dir along with manifest.json into a ZIP archive.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_zip_path)), exist_ok=True)
    
    # Write manifest temporarily into staging directory
    manifest_path = os.path.join(staging_dir, MANIFEST_FILENAME)
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2)

    all_files = []
    for root_dir, _, files in os.walk(staging_dir):
        for f in files:
            all_files.append(os.path.join(root_dir, f))

    total_files = len(all_files)
    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, full_path in enumerate(all_files, 1):
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
    Returns: list[dict] with keys: 'name', 'original_path', 'size', 'is_dir'
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
                    "is_dir": entry.get("is_dir", False)
                })
        else:
            for zi in zf.infolist():
                if zi.filename == MANIFEST_FILENAME:
                    continue
                parsed_items.append({
                    "name": zi.filename,
                    "original_path": "/storage/emulated/0/" + zi.filename,
                    "size": zi.file_size,
                    "is_dir": zi.is_dir()
                })

    return parsed_items

def extract_file_from_archive(zip_path: str, member_name: str, target_dir: str) -> str:
    """
    Extract a single member from a ZIP archive into target_dir.
    Returns the absolute path to the extracted file.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        return zf.extract(member_name, path=target_dir)
