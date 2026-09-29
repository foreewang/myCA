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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=source.parent, prefix=f".{destination.name}.", suffix=".part", delete=False
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
        logger.warning("Overlay preview generation failed; use original overlay: %s", source, exc_info=True)
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove preview temporary file: %s", temporary, exc_info=True)
