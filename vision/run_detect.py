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
    # 首次径向扫描后，允许质心重定位并重复细化的次数。
    parser.add_argument('--recenter_iterations', type=int, default=1, help='number of center update iterations after first radial scan')
    args = parser.parse_args()

    # 调用统一检测入口，返回结构化结果(JSON 可序列化字典)。
    result_json = detect_from_path(
        image_path=args.image_path,
        out_dir=args.out_dir,
        coarse_work_max=args.coarse_work_max,
        refine_pad_ratio=args.refine_pad_ratio,
        max_keep=(None if args.max_keep == 0 else args.max_keep),
        radial_mode=args.radial_mode,
        recenter_iterations=args.recenter_iterations,
    )
    # 打印最终 JSON，便于命令行直接查看或重定向保存。
    print(json.dumps(result_json, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
