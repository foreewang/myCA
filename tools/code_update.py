"""Build Git-based code bundles locally; check/apply/rollback without Git remotely.

Python standard library only. Stop the hardware service before apply/rollback.
Only Python and Markdown source files in named source directories are managed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

SOURCE_DIRS = {"devices", "workflow", "tools", "tests", "docs", "third_party"}
STATE_DIR = ".code-updates"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_digest(data):
    # Git commonly stores LF while Windows working copies contain CRLF.
    return digest(data.replace(b"\r\n", b"\n"))


def worktree_snapshot(manifest):
    """Identify selected source content, not a Git commit or ZIP timestamp."""
    identity = {key: manifest[key] for key in
                ("schema", "source_kind", "base_commit", "selected_paths", "files")}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    return "worktree-sha256:" + digest(canonical)


def validate_name(name):
    path = PurePosixPath(name)
    if (not name or str(path) != name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"..", "."} for part in name.split("/"))
            or path.parts[0] not in SOURCE_DIRS or path.suffix not in {".py", ".md"}):
        raise ValueError(f"unmanaged or unsafe source path: {name}")
    return name


def destination(root, name):
    validate_name(name)
    path = root / name
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"symlink in managed path: {name}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"path escapes project: {name}")
    if path.exists() and not path.is_file():
        raise ValueError(f"not a regular source file: {name}")
    return path


def atomic_write(path, data, mode=None, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".code-update-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if owner is not None and hasattr(os, "chown"):
            os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path, data):
    atomic_write(path, (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("utf-8"), 0o600)


def build_bundle(repo, base, target, output):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    base = git("rev-parse", "--verify", base + "^{commit}").decode().strip()
    target = git("rev-parse", "--verify", target + "^{commit}").decode().strip()
    changed = git("diff", "--name-only", "--no-renames", "-z", base, target).decode("utf-8").split("\0")
    files, payload, excluded = [], {}, []
    for name in filter(None, changed):
        try:
            validate_name(name)
        except ValueError:
            excluded.append(name)
            continue
        versions = []
        modes = []
        for revision in (base, target):
            tree = git("ls-tree", revision, "--", name)
            if not tree:
                versions.append(None)
                modes.append(None)
            else:
                entry = tree.split(b"\t", 1)[0].split()
                if entry[0] not in (b"100644", b"100755"):
                    raise ValueError(f"unsupported Git file type: {name}")
                versions.append(git("show", revision + ":" + name))
                modes.append(int(entry[0], 8) & 0o777)
        before, after = versions
        files.append({"path": name, "before": None if before is None else source_digest(before),
                      "after": None if after is None else digest(after), "mode": modes[1]})
        if after is not None:
            payload[name] = after
    if not files:
        raise ValueError("no managed code changes between these commits")
    manifest = {"schema": 1, "base_commit": base, "target_commit": target,
                "scope": "changed Python/Markdown source only; not a full checkout",
                "excluded_changes": excluded, "files": files}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never silently replace an existing release archive.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, data in payload.items():
            archive.writestr("files/" + name, data)
    return {"package": str(output), "sha256": digest(output.read_bytes()), **manifest}


def read_bundle(package):
    with zipfile.ZipFile(package) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("duplicate ZIP members")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or not manifest.get("files"):
            raise ValueError("unsupported or empty manifest")
        payload, seen = {}, set()
        allowed = {"manifest.json"}
        for entry in manifest["files"]:
            name = validate_name(entry["path"])
            if name in seen:
                raise ValueError(f"duplicate source path: {name}")
            seen.add(name)
            for key in ("before", "after"):
                value = entry.get(key)
                if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
                    raise ValueError(f"invalid hash: {name}")
            if entry.get("before") is None and entry.get("after") is None:
                raise ValueError(f"empty change: {name}")
            if entry.get("after") is not None:
                if entry.get("mode") not in (0o644, 0o755):
                    raise ValueError(f"invalid source mode: {name}")
                member = "files/" + name
                allowed.add(member)
                data = archive.read(member)
                if digest(data) != entry["after"]:
                    raise ValueError(f"corrupt package file: {name}")
                payload[name] = data
        if set(archive.namelist()) != allowed:
            raise ValueError("unexpected ZIP members")
        if manifest.get("source_kind") == "worktree" or "target_snapshot" in manifest:
            selected = manifest.get("selected_paths")
            if (manifest.get("source_kind") != "worktree" or manifest.get("target_commit") is not None
                    or not isinstance(selected, list) or not selected
                    or any(not isinstance(name, str) for name in selected)):
                raise ValueError("invalid worktree snapshot metadata")
            for name in selected:
                validate_name(name)
            if selected != sorted(set(selected)) or not seen.issubset(selected):
                raise ValueError("invalid worktree selected paths")
            if manifest.get("target_snapshot") != worktree_snapshot(manifest):
                raise ValueError("worktree snapshot checksum mismatch")
    return manifest, payload


def build_worktree_bundle(repo, base, paths, output):
    """Package only explicitly selected working files against a Git baseline."""
    repo = repo.resolve(strict=True)
    if not paths:
        raise ValueError("select at least one source file with --path")
    selected = sorted({validate_name(name) for name in paths})

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    base = git("rev-parse", "--verify", base + "^{commit}").decode().strip()
    files, payload = [], {}
    for name in selected:
        path = destination(repo, name)
        tree = git("ls-tree", base, "--", name)
        before, mode = None, 0o644
        if tree:
            entry = tree.split(b"\t", 1)[0].split()
            if entry[0] not in (b"100644", b"100755"):
                raise ValueError(f"unsupported Git file type: {name}")
            before = git("show", base + ":" + name)
            mode = int(entry[0], 8) & 0o777
        after = path.read_bytes().replace(b"\r\n", b"\n") if path.exists() else None
        if before is None and after is None:
            raise ValueError(f"selected file is absent from both baseline and worktree: {name}")
        before_hash = source_digest(before) if before is not None else None
        after_hash = digest(after) if after is not None else None
        if before_hash == after_hash:
            continue
        files.append({"path": name, "before": before_hash, "after": after_hash,
                      "mode": mode if after is not None else None})
        if after is not None:
            payload[name] = after
    if not files:
        raise ValueError("no managed code changes in selected working files")
    manifest = {"schema": 1, "source_kind": "worktree", "base_commit": base,
                "target_commit": None, "selected_paths": selected,
                "scope": "selected working Python/Markdown changes only; not a Git commit or full checkout",
                "excluded_changes": [], "files": files}
    manifest["target_snapshot"] = worktree_snapshot(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, data in payload.items():
            archive.writestr("files/" + name, data)
    return {"package": str(output), "sha256": digest(output.read_bytes()), **manifest}


def check_baseline(root, manifest):
    conflicts = []
    for entry in manifest["files"]:
        path = destination(root, entry["path"])
        actual = source_digest(path.read_bytes()) if path.exists() else None
        if actual != entry["before"]:
            conflicts.append(entry["path"])
    if conflicts:
        raise ValueError("site baseline differs; no files changed: " + ", ".join(conflicts))


def state_paths(root, create=False):
    state = root / STATE_DIR
    if state.is_symlink():
        raise ValueError("update state must not be a symlink")
    if create:
        state.mkdir(mode=0o700, exist_ok=True)
    return state, state / "history.json"


def history(root):
    _, path = state_paths(root)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def record_path(root, update_id):
    if not update_id or any(c not in "0123456789abcdef-" for c in update_id):
        raise ValueError("invalid update id")
    state, _ = state_paths(root)
    path = state / update_id
    if path.is_symlink():
        raise ValueError("update record must not be a symlink")
    return path


@contextmanager
def update_lock(root):
    state, _ = state_paths(root, create=True)
    lock = state / "lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError("another update or interrupted update lock exists; inspect .code-updates before proceeding") from None
    try:
        yield
    finally:
        lock.rmdir()


def read_record(root, update_id):
    folder = record_path(root, update_id)
    record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
    if record.get("root") != str(root) or record.get("id") != update_id:
        raise ValueError("backup belongs to a different project/update")
    for item in record["files"]:
        destination(root, item["path"])
        if item["before_raw"] is not None:
            backup = folder / "before" / item["path"]
            if (any(part.is_symlink() for part in (backup, *backup.parents) if part.is_relative_to(folder))
                    or digest(backup.read_bytes()) != item["before_raw"]):
                raise ValueError(f"backup is corrupt: {item['path']}")
    return folder, record


def check_restore(root, record, *, interrupted=False):
    # Validate every path/hash before modifying anything. Do not overwrite later
    # manual edits during rollback. Interrupted apply may have old or new bytes.
    for item in record["files"]:
        path = destination(root, item["path"])
        actual = digest(path.read_bytes()) if path.exists() else None
        accepted = {item["after"]}
        if interrupted:
            accepted.add(item["before_raw"])
        if actual not in accepted:
            raise ValueError(f"file changed after deployment; rollback stopped: {item['path']}")


def restore(root, folder, record, *, interrupted=False):
    check_restore(root, record, interrupted=interrupted)
    for item in reversed(record["files"]):
        path = destination(root, item["path"])
        if item["before_raw"] is None:
            if path.exists():
                path.unlink()  # only a named, hash-checked file added by this update
        else:
            data = (folder / "before" / item["path"]).read_bytes()
            atomic_write(path, data, item["before_mode"], item["before_owner"])


def apply_bundle(root, package):
    manifest, payload = read_bundle(package)
    with update_lock(root):
        past = history(root)
        if past:
            _, latest = read_record(root, past[-1])
            if latest["status"] not in {"applied", "rolled_back", "apply_failed_restored"}:
                raise ValueError("previous update is incomplete; recover it before applying another")
        check_baseline(root, manifest)
        update_id = uuid.uuid4().hex
        folder = record_path(root, update_id)
        folder.mkdir(mode=0o700)
        record = {"id": update_id, "root": str(root), "status": "preparing",
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "package_sha256": digest(package.read_bytes()),
                  "base_commit": manifest["base_commit"], "target_commit": manifest["target_commit"],
                  "files": []}
        for key in ("source_kind", "target_snapshot", "selected_paths"):
            if key in manifest:
                record[key] = manifest[key]
        for entry in manifest["files"]:
            path = destination(root, entry["path"])
            item = dict(entry, before_raw=None, before_mode=None, before_owner=None)
            if path.exists():
                data, attributes = path.read_bytes(), path.stat()
                backup = folder / "before" / entry["path"]
                atomic_write(backup, data, 0o600)
                item.update(before_raw=digest(data), before_mode=stat.S_IMODE(attributes.st_mode),
                            before_owner=[attributes.st_uid, attributes.st_gid] if hasattr(os, "chown") else None)
            record["files"].append(item)
        # Detect edits made while the backups were being prepared.
        check_baseline(root, manifest)
        for item in record["files"]:
            if item["before_raw"] is not None:
                backup = folder / "before" / item["path"]
                if source_digest(backup.read_bytes()) != item["before"]:
                    raise ValueError("source changed while preparing backup")
        write_json(folder / "record.json", record)
        _, history_file = state_paths(root)
        write_json(history_file, past + [update_id])
        # Validate durable backups before the first mutation.
        read_record(root, update_id)
        record["status"] = "applying"
        write_json(folder / "record.json", record)
        try:
            for item in record["files"]:
                path = destination(root, item["path"])
                if item["after"] is None:
                    path.unlink()
                else:
                    atomic_write(path, payload[item["path"]], item["before_mode"] or item["mode"], item["before_owner"])
            check_restore(root, record)
            record["status"] = "applied"
            write_json(folder / "record.json", record)
        except BaseException:
            try:
                restore(root, folder, record, interrupted=True)
                record["status"] = "apply_failed_restored"
            except Exception as recovery_error:
                record["status"] = "recovery_required"
                record["recovery_error"] = str(recovery_error)
            write_json(folder / "record.json", record)
            raise
    return record


def rollback(root):
    with update_lock(root):
        past = history(root)
        chosen = None
        for update_id in reversed(past):
            folder, record = read_record(root, update_id)
            if record["status"] not in {"rolled_back", "apply_failed_restored"}:
                chosen = (folder, record)
                break
        if chosen is None:
            raise ValueError("no active update to roll back")
        folder, record = chosen
        check_restore(root, record, interrupted=record["status"] != "applied")
        # Persist intent first so a retry can accept already-restored files.
        record["status"] = "rolling_back"
        write_json(folder / "record.json", record)
        restore(root, folder, record, interrupted=True)
        record["status"] = "rolled_back"
        record["rolled_back_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(folder / "record.json", record)
        return record


def require_stopped(service, root):
    if not service or service.startswith("-"):
        raise ValueError("specify the existing systemd service name")
    state = subprocess.run(["systemctl", "show", service, "--property=LoadState,ActiveState,WorkingDirectory", "--no-pager"],
                           check=True, capture_output=True, text=True).stdout
    fields = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
    if fields.get("LoadState") != "loaded" or fields.get("ActiveState") not in {"inactive", "failed"}:
        raise ValueError("service must exist and be stopped before modifying code")
    if not fields.get("WorkingDirectory") or Path(fields["WorkingDirectory"]).resolve() != root:
        raise ValueError("service WorkingDirectory must match the project root")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--repo", type=Path, default=Path.cwd())
    build.add_argument("--base", required=True)
    build.add_argument("--target", required=True)
    build.add_argument("--output", type=Path, required=True)
    worktree = commands.add_parser("build-worktree")
    worktree.add_argument("--repo", type=Path, default=Path.cwd())
    worktree.add_argument("--base", required=True)
    worktree.add_argument("--path", action="append", required=True,
                          help="explicit repository-relative Python/Markdown path; repeat to select more")
    worktree.add_argument("--output", type=Path, required=True)
    for name in ("check", "apply", "rollback", "status"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name in ("check", "apply"):
            command.add_argument("--package", type=Path, required=True)
        if name in ("apply", "rollback"):
            command.add_argument("--service", required=True)
    args = parser.parse_args()
    if args.command == "build":
        report = build_bundle(args.repo, args.base, args.target, args.output)
    elif args.command == "build-worktree":
        report = build_worktree_bundle(args.repo, args.base, args.path, args.output)
    else:
        root = args.root.resolve(strict=True)
        if not root.is_dir() or not (root / "workflow").is_dir() or not (root / "devices").is_dir():
            raise ValueError("root must be the existing colony system project directory")
        if args.command == "check":
            report, _ = read_bundle(args.package)
            check_baseline(root, report)
            report = {"check": "passed", **report}
        elif args.command == "status":
            report = []
            for update_id in history(root):
                _, record = read_record(root, update_id)
                report.append({k: record.get(k) for k in ("id", "status", "base_commit", "target_commit",
                                                         "target_snapshot", "source_kind", "created_utc")})
        else:
            require_stopped(args.service, root)
            report = apply_bundle(root, args.package) if args.command == "apply" else rollback(root)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
