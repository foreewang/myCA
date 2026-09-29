"""Optional exact float32 ellipse closing for single-image rules.

CPU never imports CuPy. Auto remembers CUDA failures until reset/restart;
explicit CUDA always retries and fails loudly. GPU operations are serialized
and use a private stream; no global OpenCV monkeypatch or approximate kernels.
"""
import logging
import threading

import cv2
import numpy as np

_lock = threading.RLock()
_failure = None
_kernels = None
_logger = logging.getLogger(__name__)

_SOURCE = r'''
extern "C" __global__ void build(const float* prev, float* out, int w, int n, int step, int dilate) {
    int i=blockDim.x*blockIdx.x+threadIdx.x;
    if(i>=n)return;
    float a=prev[i], b=(i%w+step<w)?prev[i+step]:(dilate?-3.402823466e+38F:3.402823466e+38F);
    out[i]=dilate?fmaxf(a,b):fminf(a,b);
}
extern "C" __global__ void ellipse(const float* table, float* out, const int* radii,
                                  int h, int w, int kh, int dilate) {
    int i=blockDim.x*blockIdx.x+threadIdx.x;
    int n=h*w;
    if(i>=n)return;
    int y=i/w,x=i%w;
    float best=dilate?-3.402823466e+38F:3.402823466e+38F;
    for(int k=0;k<kh;k++) {
        int yy=y+k-kh/2;
        if(yy<0 || yy>=h)continue;
        int r=radii[k];
        int left=max(0,x-r),right=min(w-1,x+r);
        int level=31-__clz(right-left+1);
        int base=level*n+yy*w;
        float a=table[base+left], b=table[base+right-(1<<level)+1];
        float v=dilate?fmaxf(a,b):fminf(a,b);
        best=dilate?fmaxf(best,v):fminf(best,v);
    }
    out[i]=best;
}
'''


def reset_backend():
    """Allow auto to retry after a runtime/environment repair."""
    global _failure
    with _lock:
        _failure = None


def _cuda_close(image, kernel):
    global _kernels
    from .gpu_ops import _configure_cuda_cache
    _configure_cuda_cache()
    import cupy as cp

    if _kernels is None:
        _kernels = (cp.RawKernel(_SOURCE, 'build'), cp.RawKernel(_SOURCE, 'ellipse'))
    build, ellipse = _kernels
    radii = []
    for row in kernel:
        xs = np.flatnonzero(row)
        if not len(xs) or xs[0] + xs[-1] != kernel.shape[1] - 1 or not np.all(row[xs[0]:xs[-1]+1]):
            raise ValueError('CUDA closing requires centered contiguous kernel rows')
        radii.append(int(xs[-1] - kernel.shape[1] // 2))
    h, w = image.shape
    n = h * w
    levels = min(w, kernel.shape[1]).bit_length()
    if n * levels > np.iinfo(np.int32).max:
        raise ValueError('CUDA closing working grid is too large')
    stream = cp.cuda.Stream(non_blocking=True)
    with stream:
        try:
            values = cp.asarray(image)
            rows = cp.asarray(radii, dtype=cp.int32)
            table = cp.empty((levels, h, w), cp.float32)
            grid, block = ((n + 255) // 256,), (256,)
            for dilate in (True, False):
                table[0] = values
                for level in range(1, levels):
                    build(grid, block, (table[level-1], table[level], np.int32(w), np.int32(n),
                                        np.int32(1 << (level-1)), np.int32(dilate)))
                values = cp.empty_like(values)
                ellipse(grid, block, (table, values, rows, np.int32(h), np.int32(w),
                                      np.int32(kernel.shape[0]), np.int32(dilate)))
            return cp.asnumpy(values)
        finally:
            stream.synchronize()


def background_close(image, kernel, backend='auto'):
    """Return the closed image and per-call backend diagnostics."""
    global _failure
    if backend not in {'cpu', 'cuda', 'auto'}:
        raise ValueError('texture_backend must be cpu, cuda or auto')
    if image.ndim != 2 or image.dtype != np.float32 or not image.size or not np.isfinite(image).all():
        raise ValueError('background image must be nonempty finite float32 grayscale')
    if kernel.ndim != 2 or not kernel.size or any(s % 2 == 0 for s in kernel.shape):
        raise ValueError('background kernel must have odd dimensions')
    reason = None
    if backend != 'cpu':
        with _lock:
            if backend == 'cuda' or _failure is None:
                try:
                    result = _cuda_close(image, kernel)
                    return result, {'backend': 'cuda', 'fallback_reason': None}
                except Exception as exc:
                    if backend == 'cuda':
                        raise RuntimeError(f'V4 CUDA background closing failed: {exc}') from exc
                    _failure = f'{type(exc).__name__}: {exc}'
                    _logger.warning('V4 GPU unavailable; auto uses CPU until reset/restart: %s', _failure)
            reason = _failure
    return cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel), {'backend': 'cpu', 'fallback_reason': reason}
