"""Offline deployment tests. No hardware, live project files, or services used."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from tools import code_update as update


def bundle(path, changes):
    entries = []
    with zipfile.ZipFile(path, "w") as archive:
        for name, before, after in changes:
            entries.append({"path": name,
                            "before": update.source_digest(before) if before is not None else None,
                            "after": update.digest(after) if after is not None else None,
                            "mode": 0o644 if after is not None else None})
            if after is not None:
                archive.writestr("files/" + name, after)
        archive.writestr("manifest.json", json.dumps({
            "schema": 1, "base_commit": "base", "target_commit": "target", "files": entries}))
    return path


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    for name in ("workflow", "devices", "config", "data"):
        (root / name).mkdir(parents=True)
    (root / "workflow/control.py").write_bytes(b"old\r\n")
    (root / "devices/removed.py").write_bytes(b"removed\n")
    (root / "config/site.json").write_bytes(b"local calibration")
    (root / "data/result.json").write_bytes(b"measurement")
    return root


@pytest.fixture
def release(tmp_path):
    return bundle(tmp_path / "release.zip", [
        ("workflow/control.py", b"old\n", b"new\n"),
        ("workflow/added.py", None, b"added\n"),
        ("devices/removed.py", b"removed\n", None),
    ])


def assert_original(site):
    assert (site / "workflow/control.py").read_bytes() == b"old\r\n"
    assert not (site / "workflow/added.py").exists()
    assert (site / "devices/removed.py").read_bytes() == b"removed\n"
    assert (site / "config/site.json").read_bytes() == b"local calibration"
    assert (site / "data/result.json").read_bytes() == b"measurement"


def test_apply_and_exact_rollback(site, release):
    result = update.apply_bundle(site, release)
    assert result["status"] == "applied"
    assert (site / "workflow/control.py").read_bytes() == b"new\n"
    assert (site / "workflow/added.py").read_bytes() == b"added\n"
    assert not (site / "devices/removed.py").exists()
    assert update.rollback(site)["status"] == "rolled_back"
    assert_original(site)
    with pytest.raises(ValueError, match="no active update"):
        update.rollback(site)


def test_baseline_conflict_does_not_change_files(site, release):
    (site / "workflow/control.py").write_bytes(b"site edit")
    with pytest.raises(ValueError, match="site baseline differs"):
        update.apply_bundle(site, release)
    assert (site / "workflow/control.py").read_bytes() == b"site edit"
    assert not (site / "workflow/added.py").exists()
    assert (site / "devices/removed.py").exists()
    assert update.history(site) == []


def test_rollback_preserves_later_manual_edits(site, release):
    update.apply_bundle(site, release)
    (site / "workflow/added.py").write_bytes(b"manual fix")
    with pytest.raises(ValueError, match="file changed after deployment"):
        update.rollback(site)
    assert (site / "workflow/control.py").read_bytes() == b"new\n"
    assert (site / "workflow/added.py").read_bytes() == b"manual fix"


def test_apply_failure_restores_partial_changes(site, release, monkeypatch):
    original_write = update.atomic_write
    failed = False

    def fail_once(path, data, *args):
        nonlocal failed
        if path == site / "workflow/added.py" and not failed:
            failed = True
            raise OSError("simulated disk write failure")
        return original_write(path, data, *args)

    monkeypatch.setattr(update, "atomic_write", fail_once)
    with pytest.raises(OSError, match="simulated"):
        update.apply_bundle(site, release)
    assert_original(site)
    assert update.read_record(site, update.history(site)[-1])[1]["status"] == "apply_failed_restored"


def test_interrupted_rollback_can_be_retried(site, release, monkeypatch):
    update.apply_bundle(site, release)
    original_write = update.atomic_write

    def fail_restore(path, data, *args):
        if path == site / "workflow/control.py":
            raise OSError("simulated interruption")
        return original_write(path, data, *args)

    monkeypatch.setattr(update, "atomic_write", fail_restore)
    with pytest.raises(OSError, match="simulated"):
        update.rollback(site)
    assert (site / "devices/removed.py").exists()  # first file already restored
    assert update.read_record(site, update.history(site)[-1])[1]["status"] == "rolling_back"
    monkeypatch.setattr(update, "atomic_write", original_write)
    assert update.rollback(site)["status"] == "rolled_back"
    assert_original(site)


def test_backups_are_verified_before_rollback(site, release):
    result = update.apply_bundle(site, release)
    backup = site / update.STATE_DIR / result["id"] / "before/workflow/control.py"
    backup.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="backup is corrupt"):
        update.rollback(site)
    assert (site / "workflow/control.py").read_bytes() == b"new\n"


def test_rollback_is_last_in_first_out(site, release, tmp_path):
    first = update.apply_bundle(site, release)
    second = bundle(tmp_path / "second.zip", [("workflow/control.py", b"new\n", b"newer\n")])
    second_result = update.apply_bundle(site, second)
    assert update.rollback(site)["id"] == second_result["id"]
    assert (site / "workflow/control.py").read_bytes() == b"new\n"
    assert update.rollback(site)["id"] == first["id"]
    assert_original(site)


@pytest.mark.parametrize("name", ["../workflow/a.py", "workflow/../a.py", "/workflow/a.py",
                                      "config/site.py", "workflow//a.py", "workflow/a.json"])
def test_unsafe_or_unmanaged_files_rejected(tmp_path, name):
    release = bundle(tmp_path / "unsafe.zip", [(name, None, b"data")])
    with pytest.raises(ValueError, match="unsafe source path"):
        update.read_bundle(release)


def test_bad_payload_rejected_before_changes(site, release, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(release) as source, zipfile.ZipFile(bad, "w") as target:
        for name in source.namelist():
            target.writestr(name, b"bad" if name == "files/workflow/added.py" else source.read(name))
    with pytest.raises(ValueError, match="corrupt package file"):
        update.apply_bundle(site, bad)
    assert_original(site)


@pytest.mark.parametrize("load,active,allowed", [
    ("loaded", "inactive", True), ("loaded", "failed", True),
    ("loaded", "active", False), ("loaded", "deactivating", False),
    ("not-found", "inactive", False),
])
def test_service_must_exist_and_be_stopped(monkeypatch, tmp_path, load, active, allowed):
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout=f"LoadState={load}\nActiveState={active}\nWorkingDirectory={tmp_path}\n"))
    if allowed:
        update.require_stopped("colony-system-api", tmp_path)
    else:
        with pytest.raises(ValueError, match="must exist and be stopped"):
            update.require_stopped("colony-system-api", tmp_path)


def test_wrong_service_directory_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout=f"LoadState=loaded\nActiveState=inactive\nWorkingDirectory={tmp_path / 'other'}\n"))
    with pytest.raises(ValueError, match="WorkingDirectory"):
        update.require_stopped("wrong-service", tmp_path)


def test_build_uses_committed_source_and_reports_exclusions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.invalid", *args]).decode().strip()

    git("init", "--quiet")
    (repo / "workflow").mkdir()
    (repo / "config").mkdir()
    (repo / "workflow/main.py").write_bytes(b"old\n")
    (repo / "config/site.json").write_bytes(b"old config")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "workflow/main.py").write_bytes(b"new\n")
    (repo / "config/site.json").write_bytes(b"new config")
    git("add", ".")
    git("commit", "--quiet", "-m", "target")
    target = git("rev-parse", "HEAD")
    (repo / "workflow/main.py").write_bytes(b"uncommitted edit")
    package = tmp_path / "built.zip"
    report = update.build_bundle(repo, base, target, package)
    assert report["excluded_changes"] == ["config/site.json"]
    assert report["target_commit"] == target
    _, payload = update.read_bundle(package)
    assert update.source_digest(payload["workflow/main.py"]) == update.source_digest(b"new\n")
    with pytest.raises(FileExistsError):
        update.build_bundle(repo, base, target, package)


def test_worktree_snapshot_selects_only_named_files_and_rolls_back(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Test',
                                        '-c', 'user.email=test@example.invalid', *args]).decode().strip()
    git('init', '--quiet')
    (repo / 'workflow').mkdir()
    (repo / 'devices').mkdir()
    (repo / 'workflow/main.py').write_bytes(b'old\n')
    (repo / 'devices/other.py').write_bytes(b'untouched\n')
    git('add', '.')
    git('commit', '--quiet', '-m', 'baseline')
    base = git('rev-parse', 'HEAD')
    (repo / 'workflow/main.py').write_bytes(b'new\r\n')
    (repo / 'workflow/added.py').write_bytes(b'added\r\n')
    (repo / 'devices/other.py').write_bytes(b'unrelated local change')
    package = tmp_path / 'snapshot.zip'
    selected = ['workflow/main.py', 'workflow/added.py']
    report = update.build_worktree_bundle(repo, base, selected, package)
    manifest, payload = update.read_bundle(package)
    assert manifest['target_commit'] is None
    assert manifest['target_snapshot'] == update.worktree_snapshot(manifest)
    assert set(payload) == set(selected)
    assert payload['workflow/main.py'] == b'new\n'
    assert git('rev-parse', 'HEAD') == base
    site = tmp_path / 'site'
    (site / 'workflow').mkdir(parents=True)
    (site / 'devices').mkdir()
    (site / 'workflow/main.py').write_bytes(b'old\r\n')
    update.check_baseline(site, manifest)
    record = update.apply_bundle(site, package)
    assert record['target_snapshot'] == report['target_snapshot']
    assert record['target_commit'] is None
    assert update.rollback(site)['status'] == 'rolled_back'
    assert (site / 'workflow/main.py').read_bytes() == b'old\r\n'
    assert not (site / 'workflow/added.py').exists()
    with pytest.raises(FileExistsError):
        update.build_worktree_bundle(repo, base, selected, package)
    with pytest.raises(ValueError, match='unsafe'):
        update.build_worktree_bundle(repo, base, ['../outside.py'], tmp_path / 'bad.zip')
    tampered = tmp_path / 'tampered.zip'
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name == 'manifest.json':
                changed = json.loads(data)
                changed['target_snapshot'] = 'worktree-sha256:' + '0' * 64
                data = json.dumps(changed).encode()
            target.writestr(name, data)
    with pytest.raises(ValueError, match='snapshot checksum'):
        update.read_bundle(tampered)
