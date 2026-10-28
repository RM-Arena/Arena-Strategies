"""Build a deterministic strategy feed. Standard library only; also shipped in Arena-Strategies."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import zipfile

FORMAT = "arena-strategy-library-v1"
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SEASON = re.compile(r"rmuc-(\d{4})-")
SOURCE_LIMIT = 512 * 1024
# Folder path -> strategy ID the client knows. Existing IDs stay stable so built-in versions are still
# replaced and Elo records still match; new strategies get an ID derived from their folder.
IDS_FILE = "ids.json"


def strategies(source):
    """(client ID, folder) for strategies/<RMUC year>/<model provider>/<strategy>/, ordered by ID."""
    ids_path = source / IDS_FILE
    ids = json.loads(ids_path.read_bytes()) if ids_path.exists() else {}
    if not isinstance(ids, dict) or any(not isinstance(v, str) or not KEY.fullmatch(v) for v in ids.values()):
        raise ValueError(f"{IDS_FILE} must map folder paths to strategy IDs")
    found, used = [], set()
    for manifest_path in sorted(source.glob("**/manifest.json")):
        folder = manifest_path.parent
        relative = folder.relative_to(source).parts
        if len(relative) != 3 or not re.fullmatch(r"RMUC\d{4}", relative[0]) or not all(FOLDER.fullmatch(part) for part in relative):
            raise ValueError(f"Strategies live in strategies/RMUC<year>/<provider>/<strategy>/: {folder}")
        path = "/".join(relative)
        key = ids.get(path) or re.sub(r"[^A-Za-z0-9_-]", "", relative[2]) + "_" + relative[0][4:]
        if not KEY.fullmatch(key) or key.casefold() in used:
            raise ValueError(f"Invalid or duplicate strategy ID {key!r}: {folder}")
        used.add(key.casefold())
        found.append((key, folder, path))
    stale = set(ids) - {path for _, _, path in found}
    if stale:
        raise ValueError(f"{IDS_FILE} names missing folders: {', '.join(sorted(stale))}")
    return sorted(found)


def publish(source, output, min_client_version="0.6.0", tracked=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", min_client_version):
        raise ValueError("Minimum client version must be major.minor.patch with an optional prerelease suffix")
    tracked_paths = None
    if tracked:
        names = subprocess.check_output(["git", "ls-files", "-z", "--", str(source)], cwd=source).decode().split("\0")
        tracked_paths = {(source / name).resolve() for name in names if name}
    payload = io.BytesIO()
    count, unpacked, file_count = 0, 0, 0
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for key, folder, path in strategies(source):
            manifest_path = folder / "manifest.json"
            if tracked_paths is not None and manifest_path.resolve() not in tracked_paths:
                continue
            if any(p.is_symlink() for p in (folder, folder.parent, folder.parent.parent, manifest_path)):
                raise ValueError(f"Invalid strategy directory: {folder}")
            manifest = json.loads(manifest_path.read_bytes())
            if manifest.get("api_version") != 3 or manifest.get("mode") not in ("team", "squad", "distributed", "adaptive", "robot"):
                raise ValueError(f"Unsupported strategy: {folder}")
            season = SEASON.match(manifest.get("rules_profile") or "")
            if not season or path.split("/")[0] != "RMUC" + season.group(1):
                raise ValueError(f"Strategy is filed under the wrong season: {folder}")
            body = {k: v for k, v in manifest.items() if k != "version"}
            if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16] != manifest.get("version"):
                raise ValueError(f"Manifest version mismatch: {folder}")
            sources = set(manifest["entries"].values())
            if not sources or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.py", name) for name in sources):
                raise ValueError(f"Invalid entry paths: {folder}")
            names = {"manifest.json", *sources}
            names.update(name for name in ("profile.json", "showcase.json") if (folder / name).exists())
            for name in sorted(names):
                file = folder / name
                if file.is_symlink() or not file.is_file() or file.stat().st_size > SOURCE_LIMIT:
                    raise ValueError(f"Invalid strategy file: {file}")
                if tracked_paths is not None and file.resolve() not in tracked_paths:
                    raise ValueError(f"Strategy file is not tracked: {file}")
                data = file.read_bytes()
                unpacked += len(data)
                file_count += 1
                if unpacked > 128 * 1024 * 1024 or file_count > 16000:
                    raise ValueError("Strategy library exceeds archive limits")
                if name in sources:
                    if hashlib.sha256(data).hexdigest() != manifest["hashes"].get(name):
                        raise ValueError(f"Strategy checksum mismatch: {file}")
                    compile(data, name, "exec")
                else:
                    json.loads(data)
                # The archive stays flat (strategies/<ID>/<file>) for every released client.
                info = zipfile.ZipInfo(f"strategies/{key}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
            count += 1
    data = payload.getvalue()
    if not 0 < count <= 2000 or len(data) > 32 * 1024 * 1024:
        raise ValueError("Empty or oversized strategy library")
    digest = hashlib.sha256(data).hexdigest()
    relative = f"bundles/{digest}.zip"
    (output / "bundles").mkdir(parents=True, exist_ok=True)
    (output / relative).write_bytes(data)
    catalog = {"format": FORMAT, "min_client_version": min_client_version,
               "archive": relative, "sha256": digest, "bytes": len(data), "count": count}
    # Publish the pointer last. Bundles are content addressed and remain available across revisions.
    temporary = output / "catalog.json.tmp"
    temporary.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "catalog.json")
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("strategies"))
    parser.add_argument("--output", type=Path, default=Path("."))
    parser.add_argument("--min-client-version")
    parser.add_argument("--tracked", action="store_true", help="Only publish files tracked by git")
    args = parser.parse_args()
    settings = args.output / "library.json"
    minimum = args.min_client_version or (json.loads(settings.read_bytes())["min_client_version"] if settings.exists() else "0.6.0")
    print(json.dumps(publish(args.source, args.output, minimum, args.tracked), indent=2))


if __name__ == "__main__":
    main()
