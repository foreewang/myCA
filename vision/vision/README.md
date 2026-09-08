# 4x iPSC colony localization

The production entrypoint is:

```text
vision.vision.instance_pipeline:process_image
```

It combines a full-image detector pass with overlapping tiles, fuses duplicate
boxes, and segments each retained ROI. Formal instances and uncertain review
candidates are separate. Output schema v2 enforces:

- `component_count == len(components)`
- `review_candidate_count == len(review_candidates)`
- stable IDs after spatial sorting (`C001`, `R001`)
- a 16-bit `05_instance_mask.png` whose label values match `instance_label`
- explicit model hashes, providers, fallback reasons, and timings
- `quality_assessment.status = not_assessed`; 4x is localization-only

The model directory must contain a strict manifest and two verified ONNX files.
See `../models/ipsc_4x/model_manifest.example.json`. Missing/incompatible models
are hard errors and never cause an empty result or a silent legacy fallback.

CLI example:

```bash
python -m vision.run_detect image.bmp --backend model \
  --model-dir /opt/colony_system/vision/models/ipsc_4x/production --provider cuda \
  --out-dir outputs/image_001
```

The previous OpenCV rule chain remains available only through explicit legacy
selection:

```text
vision.vision.detect_pipeline:process_image
python -m vision.run_detect image.bmp --backend legacy
```

Legacy output must not be used as evidence that the model meets recall or
precision acceptance criteria.

## Rule output controls

The rule algorithm and backend structure are unchanged. CLI still defaults to
`model`, and workflow still defaults to the 4x model entrypoint. Explicit rule
selection is required. Workflow supports
`vision.vision.detect_pipeline:process_image` and its
`vision.detect_pipeline:process_image` alias for the new output option.

Rule Python entrypoints accept `save_debug: bool = False`. With an output
directory, the default writes only:

```text
05_contour_mask.bmp
06_overlay.bmp
07_result.json
```

`save_debug=True` restores all outputs, including `01_gray.bmp`,
`02_coarse_flat.bmp`, `03_coarse_binary.bmp`, and `04_refine_density.bmp`.
Detection results and the pixels in 05/06 remain unchanged. For 5120×5120 inputs,
the BMP output volume is approximately 100 MiB instead of 200 MiB. Existing
01–04 files in a reused output directory are not automatically deleted; use a
new directory when checking the current run's outputs.

In task JSON, use `task.detect.save_debug` with a JSON boolean. Non-boolean
values are rejected during detection execution. Workflow forwards this option
only to the two rule `process_image` entrypoints above; model and third-party
calls retain their existing keyword arguments. No additional algorithm tuning
parameters are forwarded.

Existing output controls take precedence: workflow `save_overlay=false` does
not pass a vision output directory, and `overlay_source=workflow` only writes
the workflow overlay. Direct Python calls with `out_dir=None` return in-memory
results only, even with `save_debug=True`. Existing defaults are unchanged:
`process_image` writes nothing when `out_dir` is omitted, `detect_from_gray`
defaults to `out_dir=None`, and `detect_from_path` defaults to
`out_dir="outputs_5120_contour_refined_opt"`.

To save the complete legacy debug outputs:

```bash
python -m vision.run_detect image.bmp --backend legacy --save-debug \
  --out-dir outputs/image_001_debug
```

`--save-debug` is rejected for the model backend. Omitting it in legacy mode
writes the default 05–07 outputs when an output directory is provided.
