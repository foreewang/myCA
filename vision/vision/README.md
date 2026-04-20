# Structured vision package

This package is a direct structural refactor of `detect_colony_5120_contour_optimized_v2.py`.

Goal:
- keep algorithm logic unchanged
- split responsibilities into the existing `vision/` project modules
- keep a single pipeline entry for CLI or upper-level system calls

## Suggested replacement
Copy the files under `vision/` into your project path:

```bash
~/colony_system/vision/
```

## Entry points

Programmatic:

```python
from vision.detect_pipeline import detect_from_path, detect_from_gray
```

CLI:

```bash
python3 run_detect.py /path/to/image.bmp --out_dir outputs
```
