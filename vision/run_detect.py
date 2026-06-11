import argparse
import json

from vision.vision.detect_pipeline import detect_from_path


def main():
    """命令行入口: 读取参数并执行菌落检测流程。"""
    parser = argparse.ArgumentParser(
        description='Coarse ROI + vectorized radial contour refinement for 5120 BMP colony images'
    )
    # 必填参数: 输入图像路径。
    parser.add_argument('image_path', help='input BMP image path')
    # 可选参数: 输出目录，默认写到项目下固定文件夹。
    parser.add_argument('--out_dir', default='outputs_5120_contour_refined_opt', help='output directory')
    # 粗检测阶段内部最大尺寸(越小越快，过小可能损失细节)。
    parser.add_argument('--coarse_work_max', type=int, default=1024, help='coarse detection internal max size')
    # 细化前对粗框额外扩边比例，防止轮廓贴边被截断。
    parser.add_argument('--refine_pad_ratio', type=float, default=0.20, help='extra pad around coarse box before refinement')
    # 仅保留前 K 个粗候选，0 表示全部保留。
    parser.add_argument('--max_keep', type=int, default=0, help='keep top-k coarse colonies; 0 means keep all')
    # 径向细化模式:
    # threshold=阈值穿越, gradient=梯度极值, hybrid=二者结合(默认)。
    parser.add_argument('--radial_mode', choices=['threshold', 'gradient', 'hybrid'], default='hybrid')
    parser.add_argument('--edge_refine_method', choices=['none', 'grabcut', 'hybrid'], default='hybrid')
    parser.add_argument('--edge_refine_iterations', type=int, default=2)
    # 首次径向扫描后，允许质心重定位并重复细化的次数。
    parser.add_argument('--recenter_iterations', type=int, default=1, help='number of center update iterations after first radial scan')
    parser.add_argument('--seed_quantile', type=float, default=0.12, help='strict dark-core quantile for coarse detection')
    parser.add_argument('--core_density_min', type=float, default=80, help='minimum density threshold for coarse dark-core candidates')
    parser.add_argument('--min_foreground_ratio', type=float, default=0.025, help='minimum dark-core pixels / bbox area')
    parser.add_argument('--max_bbox_area_ratio', type=float, default=0.30, help='maximum coarse bbox area / image area')
    parser.add_argument('--mm_per_pixel', type=float, default=None, help='millimeters per pixel for scale bar')
    parser.add_argument('--scale_bar_length_mm', type=float, default=None, help='fixed scale bar length in mm')
    parser.add_argument('--scale_bar_position', choices=['bottom_right', 'bottom_left'], default='bottom_right')
    args = parser.parse_args()

    scale_bar = None
    if args.mm_per_pixel is not None:
        scale_bar = {
            "enabled": True,
            "mm_per_pixel": args.mm_per_pixel,
            "position": args.scale_bar_position,
        }
        if args.scale_bar_length_mm is not None:
            scale_bar["length_mm"] = args.scale_bar_length_mm

    # 调用统一检测入口，返回结构化结果(JSON 可序列化字典)。
    result_json = detect_from_path(
        image_path=args.image_path,
        out_dir=args.out_dir,
        coarse_work_max=args.coarse_work_max,
        refine_pad_ratio=args.refine_pad_ratio,
        max_keep=(None if args.max_keep == 0 else args.max_keep),
        radial_mode=args.radial_mode,
        recenter_iterations=args.recenter_iterations,
        edge_refine_method=args.edge_refine_method,
        edge_refine_iterations=args.edge_refine_iterations,
        seed_quantile=args.seed_quantile,
        core_density_min=args.core_density_min,
        min_foreground_ratio=args.min_foreground_ratio,
        max_bbox_area_ratio=args.max_bbox_area_ratio,
        scale_bar=scale_bar,
        mm_per_pixel=args.mm_per_pixel
    )
    # 打印最终 JSON，便于命令行直接查看或重定向保存。
    print(json.dumps(result_json, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
