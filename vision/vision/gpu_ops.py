"""Optional CuPy texture operators, with identical CPU border/quantization rules.

No CUDA import at module import time. Explicit cuda fails loudly; auto remembers
device failures and uses CPU until reset_backend() or process restart.
"""
from __future__ import annotations

import os
import tempfile
import threading

import cv2
import numpy as np

DEFAULT_TEXTURE_BACKEND = 'cuda'
_lock = threading.RLock()
_cupy = None
_failure = None
_fallback_count = 0
_last_backend = 'cpu'


def reset_backend():
    global _cupy, _failure, _fallback_count, _last_backend
    with _lock:
        _cupy, _failure, _fallback_count, _last_backend = None, None, 0, 'cpu'


def backend_status():
    with _lock:
        result = {'last_backend': _last_backend, 'fallback_reason': _failure,
                  'fallback_count': _fallback_count}
        if _cupy is not None:
            try:
                result['memory_pool_total_bytes'] = int(_cupy.get_default_memory_pool().total_bytes())
                result['device_name'] = _cupy.cuda.runtime.getDeviceProperties(0)['name'].decode()
            except Exception:
                pass
        return result


def _cpu_moments(gray, window, density_window):
    # Centering reduces catastrophic cancellation for bright low-contrast images.
    values = gray.astype(np.float32) - 128.0
    mean = cv2.boxFilter(values, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    mean2 = cv2.boxFilter(values * values, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    std = np.sqrt(np.maximum(mean2 - mean * mean, 0))
    std = np.rint(std * 256) / 256
    density = cv2.boxFilter(std, -1, (density_window, density_window), borderType=cv2.BORDER_REFLECT)
    return std, np.rint(density * 256) / 256


def _cuda_moments(gray, window, density_window):
    global _cupy
    _configure_cuda_cache()
    import cupy as cp
    from cupyx.scipy.ndimage import uniform_filter
    values = cp.asarray(gray, dtype=cp.float32) - 128.0
    mean = uniform_filter(values, window, mode='reflect')
    mean2 = uniform_filter(values * values, window, mode='reflect')
    std = cp.rint(cp.sqrt(cp.maximum(mean2 - mean * mean, 0)) * 256) / 256
    density = cp.rint(uniform_filter(std, density_window, mode='reflect') * 256) / 256
    result = cp.asnumpy(cp.stack((std, density)))  # One synchronous device-to-host transfer.
    _cupy = cp
    return result[0], result[1]


def _configure_cuda_cache():
    """Avoid NVRTC source paths it cannot open on affected Windows setups.

    In-memory compilation avoids changing process-wide TEMP/TMP. Respect an
    explicit CuPy cache policy; the fallback is only needed for non-ASCII temp.
    """
    if os.name == 'nt' and not tempfile.gettempdir().isascii():
        os.environ.setdefault('CUPY_CACHE_IN_MEMORY', '1')


def texture_moments(gray, window=7, density_window=21, backend=DEFAULT_TEXTURE_BACKEND):
    """Return local std and its spatial density in original uint8 intensity units."""
    global _failure, _fallback_count, _last_backend
    if backend not in {'cpu', 'cuda', 'auto'}:
        raise ValueError('texture_backend must be cpu, cuda or auto')
    if gray.ndim != 2 or gray.dtype != np.uint8 or not gray.size:
        raise ValueError('texture_moments requires nonempty uint8 grayscale')
    for value in (window, density_window):
        if not isinstance(value, int) or value < 1 or value % 2 == 0:
            raise ValueError('texture windows must be positive odd integers')
    with _lock:
        if backend != 'cpu' and (backend == 'cuda' or _failure is None):
            try:
                result = _cuda_moments(gray, window, density_window)
                _last_backend = 'cuda'
                return result, {'backend': 'cuda', 'fallback_reason': None}
            except Exception as exc:
                if backend == 'cuda':
                    raise RuntimeError(f'CUDA texture computation failed: {exc}') from exc
                _failure = f'{type(exc).__name__}: {exc}'
        if backend == 'auto' and _failure:
            _fallback_count += 1
        _last_backend = 'cpu'
        return _cpu_moments(gray, window, density_window), {
            'backend': 'cpu', 'fallback_reason': _failure if backend == 'auto' else None}
