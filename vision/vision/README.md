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
