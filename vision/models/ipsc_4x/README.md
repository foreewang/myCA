# 4x iPSC localization model package

Copy `model_manifest.example.json` to `model_manifest.json`, replace every
placeholder, and deploy the two exported ONNX graphs beside it. Model binaries
are intentionally ignored by Git.

The detector graph must already decode its predictions and perform NMS. Its
single output is `[N,6]` or `[1,N,6]` in input-pixel coordinates with columns
`x1,y1,x2,y2,score,class`; class `0` is `ipsc_clone`. The segmenter operates on
one padded detector ROI at a time and returns foreground logits. Every graph
uses batch size 1 and a fixed NCHW shape.

The manifest SHA-256 values, node names, dtypes, and shapes are verified before
the first image. A mismatch is a hard failure, not a zero-colony result. The 4x
pipeline performs localization only and never assigns good/bad quality labels.
