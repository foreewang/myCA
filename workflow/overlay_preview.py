"""Write full-resolution JPEG previews without modifying algorithm artifacts."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile

from PIL import Image

logger = logging.getLogger(__name__)


def save_overlay_preview(overlay_path: str | Path) -> str | None:
    """Return a completed JPEG 90/4:4:4 path, or None on preview failure.

    A previous preview is never returned after a failed regeneration. Callers
    must use the original overlay when the current result has a null preview.
    """
    source = Path(overlay_path)
    destination = source.with_name(f"{source.stem}_preview.jpg")
    return save_jpeg_preview(source, destination)


def save_jpeg_preview(source_path: str | Path, destination_path: str | Path) -> str | None:
    """Atomically save a full-resolution JPEG copy; return None on failure.

    Only a successfully regenerated file is returned, never a stale preview.
    The source must not be overwritten, even with a custom capture directory.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    temporary = None
    try:
        if source.resolve() == destination.resolve():
            raise ValueError("JPEG preview destination must differ from source")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".part", delete=False
        ) as stream:
            temporary = Path(stream.name)
            with Image.open(source) as original:
                with original.convert("RGB") as rgb:
                    rgb.save(stream, format="JPEG", quality=90, subsampling=0, optimize=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return str(destination)
    except (OSError, ValueError):
        logger.warning("JPEG preview generation failed; use original image: %s", source, exc_info=True)
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove preview temporary file: %s", temporary, exc_info=True)
