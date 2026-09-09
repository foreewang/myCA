"""Command-line entrypoint for model-based 4x localization or explicit legacy mode.

Direct script execution only places the ``vision`` directory on ``sys.path``.
Add the repository root so sibling packages such as ``workflow`` remain
importable. Module execution via ``python -m vision.run_detect`` already has
the correct import root.
"""

import argparse
import json
from pathlib import Path
import sys


if __package__ in (None, ""):
    script_directory = str(Path(__file__).resolve().parent)
    repository_root = str(Path(__file__).resolve().parents[1])
    # The current working directory may already contribute this path later in
    # the list. It still has to precede ``.../vision`` or ``vision`` resolves
    # to the inner package and ``vision.vision`` becomes unavailable.
    sys.path[:] = [
        entry for entry in sys.path if entry not in {repository_root, script_directory}
    ]
    sys.path.insert(0, repository_root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="4x iPSC colony instance localization (quality assessment requires 10x)"
    )
    parser.add_argument("image_path", help="input microscope image")
    parser.add_argument("--out-dir", default="outputs_4x_instances", help="output directory")
    parser.add_argument("--backend", choices=("model", "legacy"), default="model")
    parser.add_argument("--model-dir", help="directory containing model_manifest.json and ONNX weights")
    parser.add_argument("--provider", choices=("cuda", "cpu", "auto"), default="cuda")
    parser.add_argument(
        "--allow-cpu-fallback",
        action="store_true",
        help="explicitly allow rebuilding sessions on CPU when CUDA initialization fails",
    )
    parser.add_argument("--mm-per-pixel", type=float, default=None)
    parser.add_argument("--texture-backend", choices=("cpu", "cuda", "auto"), default=None,
                        help="legacy rule texture backend (default: cuda; errors if unavailable)")
    parser.add_argument("--safe-margin-px", type=float, default=None,
                        help="legacy minimum distance from texture boundary in original pixels")
    parser.add_argument(
        "--save-debug",
        action="store_true",
        help="legacy only: also save intermediate debug images 01-04",
    )
    args = parser.parse_args()
    if args.save_debug and args.backend != "legacy":
        parser.error("--save-debug is only supported when --backend=legacy")
    if args.backend != "legacy" and (args.texture_backend is not None or args.safe_margin_px is not None):
        parser.error("--texture-backend and --safe-margin-px require --backend=legacy")

    scale_bar = None
    if args.mm_per_pixel is not None:
        scale_bar = {"enabled": True, "mm_per_pixel": args.mm_per_pixel}

    if args.backend == "model":
        if not args.model_dir:
            parser.error("--model-dir is required when --backend=model")
        from vision.vision.instance_pipeline import detect_from_path

        result = detect_from_path(
            args.image_path,
            out_dir=args.out_dir,
            model_dir=args.model_dir,
            provider=args.provider,
            allow_cpu_fallback=args.allow_cpu_fallback,
            objective_name="4x",
            scale_bar=scale_bar,
        )
    else:
        from vision.vision.detect_pipeline import detect_from_path

        texture_options = {}
        if args.texture_backend is not None:
            texture_options["texture_backend"] = args.texture_backend
        if args.safe_margin_px is not None:
            texture_options["safe_margin_px"] = args.safe_margin_px
        result = detect_from_path(
            args.image_path,
            out_dir=args.out_dir,
            scale_bar=scale_bar,
            save_debug=args.save_debug,
            **texture_options,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
