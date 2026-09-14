"""Scale-aware texture evidence; darkness is not a foreground requirement."""
import cv2
import numpy as np
from .gpu_ops import DEFAULT_TEXTURE_BACKEND, texture_moments

# Tuned in the working image grid, shared by coarse detection and ROI refinement.
BACKGROUND_SUPPORT_MULTIPLIER = 1.5
DARK_BODY_CONTRAST = 45.0
SUPPORT_CLOSE_WINDOW = 7


def resize_keep_ratio(gray, work_max=1024):
    """Return resized image and nominal scale; use actual axes for coordinates."""
    if not np.isfinite(work_max) or work_max < 1:
        raise ValueError('work_max must be positive')
    h, w = gray.shape
    scale = max(1.0, max(h, w) / float(work_max))
    size = (max(1, round(w / scale)), max(1, round(h / scale)))
    small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA) if size != (w, h) else gray.copy()
    return small, scale


def map_points(points, source_shape, target_shape):
    """Map pixel centers without intermediate rounding; ROI offsets are separate."""
    scale = np.array([target_shape[1] / source_shape[1], target_shape[0] / source_shape[0]])
    return (np.asarray(points, dtype=np.float64) + 0.5) * scale - 0.5


def map_bbox(bbox, source_shape, target_shape):
    """Map half-open bbox edges using actual x/y scales."""
    x, y, w, h = bbox
    sx, sy = target_shape[1] / source_shape[1], target_shape[0] / source_shape[0]
    x0, y0 = round(x * sx), round(y * sy)
    x1, y1 = round((x + w) * sx), round((y + h) * sy)
    return [x0, y0, x1 - x0, y1 - y0]


def texture_signal(gray, *, backend=DEFAULT_TEXTURE_BACKEND, window=7, density_window=21, noise_floor=1.5):
    """Absolute noise floor + adaptive contrast; return evidence, support and seed."""
    if not np.isfinite(noise_floor) or noise_floor <= 0:
        raise ValueError('texture_noise_floor must be positive')
    (std, density), meta = texture_moments(gray, window, density_window, backend)
    # Estimate background texture before thresholding. Absolute noise alone
    # cannot distinguish a textured substrate from a denser colony on top.
    interior_floor = max(float(noise_floor), float(np.percentile(std, 40)) * 1.5)
    upper = max(float(np.percentile(density, 99)), noise_floor * 2)
    heat = np.rint(np.clip(density / upper, 0, 1) * 255).astype(np.uint8)
    otsu, _ = cv2.threshold(heat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = max(noise_floor, float(otsu) * upper / 255)
    # Sparse cells must not percolate into a full-frame foreground component.
    # Use the measured background floor for support as well as for quality.
    support_floor = max(noise_floor, interior_floor * BACKGROUND_SUPPORT_MULTIPLIER, threshold * .65)
    seed = (density > max(support_floor, threshold)).astype(np.uint8) * 255
    support = (density > support_floor).astype(np.uint8) * 255
    # Dense, optically dark colonies can have low internal contrast. Include
    # their body, but still require real interior texture (a smooth disk fails).
    smooth = cv2.GaussianBlur(gray, (0, 0), 5)
    dark_body = smooth.astype(np.float32) < float(np.median(smooth)) - DARK_BODY_CONTRAST
    support[dark_body] = 255
    seed[dark_body & (std > noise_floor)] = 255
    # Normalize dark-body variance to the same quality threshold. This is
    # equivalent to testing std > noise_floor inside the dark body; elsewhere
    # std must exceed the adaptive background floor. Keep raw std unchanged.
    quality_std = np.where(dark_body, std * (interior_floor / noise_floor), std)
    support = cv2.morphologyEx(support, cv2.MORPH_CLOSE,
                             np.ones((SUPPORT_CLOSE_WINDOW, SUPPORT_CLOSE_WINDOW), np.uint8))
    support = cv2.morphologyEx(support, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return {'std': std, 'quality_std': quality_std, 'density_raw': density, 'density': heat,
            'seed': seed, 'support': support, 'threshold': threshold,
            'noise_floor': interior_floor, **meta}
