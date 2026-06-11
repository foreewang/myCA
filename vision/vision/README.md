# Structured vision package

This package contains the OpenCV-based colony detection pipeline used by the
workflow layer.

Goal:
- detect coarse colony ROI candidates from dark-core and texture-density cues
- refine local contours with radial search and optional GrabCut edge refinement
- annotate visual well-border distance and pickability fields
- keep a single pipeline entry for CLI or upper-level workflow calls

## Main Entry Points

Programmatic usage:

```python
from vision.detect_pipeline import detect_from_path, detect_from_gray
```

CLI:

```bash
python vision/run_detect.py /path/to/image.bmp --out_dir outputs
```

The output directory contains normalized gray image, coarse/refine debug images,
overlay, contour mask, and `07_result.json`.
