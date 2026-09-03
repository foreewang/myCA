"""Locate Hikrobot MVS Python bindings and native libraries on Linux.

The Windows deployment copies ``MvImport`` next to the project.  The Linux
industrial-PC layout uses the vendor installer under ``/opt/MVS`` and needs
``libMvCameraControl.so`` on the dynamic-linker search path before the
ctypes wrapper is imported.

Official ``MvCameraControl_class.py`` loads the native library as
``getenv('MVCAM_COMMON_RUNENV') + '/64/libMvCameraControl.so'``.  That variable
must therefore point at ``/opt/MVS/lib``, not the install root ``/opt/MVS``.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MVS_ROOT = Path("/opt/MVS")
DEFAULT_MVS_LIB_ROOT = DEFAULT_MVS_ROOT / "lib"
DEFAULT_MVS_PYTHON_DIR = "/opt/MVS/Samples/64/Python/MvImport"
MVS_REQUIRED_PYTHON_FILES = (
    "MvCameraControl_class.py",
    "CameraParams_header.py",
    "CameraParams_const.py",
)
_MVS_PYTHON_CANDIDATES = (
    DEFAULT_MVS_PYTHON_DIR,
    "/opt/MVS/Samples/Python/MvImport",
    "/opt/MVS/Samples/aarch64/Python/MvImport",
)
_MVS_LIB_CANDIDATES = (
    "/opt/MVS/lib/64",
    "/opt/MVS/lib/aarch64",
    "/opt/MVS/lib",
)
_MVS_ARCH_DIRS = ("64", "aarch64", "32", "armhf", "arm-none")
_MVS_NATIVE_GLOB = "libMvCameraControl.so*"

_native_libraries_ready = False


def _mvs_install_root() -> Path:
    """Return the MVS installer root that contains Samples/ and lib/."""

    raw = str(os.getenv("MVCAM_COMMON_RUNENV") or "").strip()
    path = Path(raw) if raw else DEFAULT_MVS_ROOT
    if path.name == "lib":
        return path.parent
    return path


def _native_libraries_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in sorted(directory.glob(_MVS_NATIVE_GLOB)):
        if not (candidate.is_file() or candidate.is_symlink()):
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found


def _is_wrapper_runenv(path: Path) -> bool:
    return any(_native_libraries_in(path / arch) for arch in _MVS_ARCH_DIRS)


def resolve_mvcam_common_runenv() -> Path:
    """Return the directory official MvImport prepends before ``/64/*.so``."""

    raw = str(os.getenv("MVCAM_COMMON_RUNENV") or "").strip()
    candidates: list[Path] = []
    if raw:
        path = Path(raw)
        candidates.extend((path, path / "lib"))
        if path.name in _MVS_ARCH_DIRS:
            candidates.append(path.parent)
    candidates.extend((DEFAULT_MVS_LIB_ROOT, DEFAULT_MVS_ROOT))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if _is_wrapper_runenv(candidate):
            return candidate
    return Path(raw) if raw else DEFAULT_MVS_LIB_ROOT


def mvs_python_dir_candidates(configured: str | None = None) -> list[Path]:
    """Return SDK import directories in search order."""

    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        resolved = str(path)
        if not resolved or resolved in seen:
            return
        seen.add(resolved)
        candidates.append(path)

    if configured and str(configured).strip():
        add(Path(str(configured).strip()))
    env_dir = str(os.getenv("MVS_PYTHON_DIR") or "").strip()
    if env_dir:
        add(Path(env_dir))
    root = _mvs_install_root()
    add(root / "Samples" / "64" / "Python" / "MvImport")
    add(root / "Samples" / "Python" / "MvImport")
    add(root / "Samples" / "aarch64" / "Python" / "MvImport")
    for fallback in _MVS_PYTHON_CANDIDATES:
        add(Path(fallback))
    add(PROJECT_ROOT / "MvImport")
    return candidates


def resolve_existing_mvs_python_dir(configured: str | None = None) -> Path | None:
    """Return the first candidate that contains the required Python wrapper files."""

    for path in mvs_python_dir_candidates(configured):
        if all((path / name).is_file() for name in MVS_REQUIRED_PYTHON_FILES):
            return path
    return None


def _native_library_dirs() -> list[Path]:
    install_root = _mvs_install_root()
    runenv = resolve_mvcam_common_runenv()
    dirs: list[Path] = []
    seen: set[str] = set()
    for path in (
        *(runenv / arch for arch in _MVS_ARCH_DIRS),
        install_root / "lib" / "64",
        install_root / "lib" / "aarch64",
        install_root / "lib",
        *(Path(item) for item in _MVS_LIB_CANDIDATES),
    ):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            dirs.append(path)
    return dirs


def ensure_mvs_native_libraries() -> None:
    """Make MVS shared objects visible to ctypes before importing MvImport.

    ``LD_LIBRARY_PATH`` is normally read at process start.  Preloading the
    vendor ``.so`` with an absolute path still lets a later ``CDLL(soname)``
    succeed in the same process when ldconfig has not been run.

    Official MvImport concatenates ``MVCAM_COMMON_RUNENV`` with
    ``/64/libMvCameraControl.so``.  Rewrite a bare ``/opt/MVS`` value to
    ``/opt/MVS/lib`` before that import runs.
    """

    global _native_libraries_ready
    if os.name == "nt" or _native_libraries_ready:
        return

    runenv = resolve_mvcam_common_runenv()
    os.environ["MVCAM_COMMON_RUNENV"] = str(runenv)

    lib_dirs = _native_library_dirs()
    if lib_dirs:
        current = str(os.environ.get("LD_LIBRARY_PATH") or "")
        extra = os.pathsep.join(str(path) for path in lib_dirs)
        if current:
            parts = current.split(os.pathsep)
            prefixed = [item for item in extra.split(os.pathsep) if item and item not in parts]
            if prefixed:
                os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(prefixed + parts)
        else:
            os.environ["LD_LIBRARY_PATH"] = extra

    last_error: OSError | None = None
    for directory in lib_dirs:
        for candidate in _native_libraries_in(directory):
            try:
                ctypes.CDLL(str(candidate), mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                logger.debug("preloaded MVS native library %s", candidate)
                _native_libraries_ready = True
                return
            except OSError as exc:
                last_error = exc
                logger.debug("unable to preload %s: %s", candidate, exc)

    if last_error is not None:
        logger.warning("MVS native library preload failed: %s", last_error)
    _native_libraries_ready = True
