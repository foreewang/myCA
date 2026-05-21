#评估残差：比较补偿前后人工点击坐标与图像中心的距离，计算误差减少的百分比。
import math

width = 5120
height = 5120
fov_mm = 3.0

#补偿前人工点击得到的坐标
before_cx = 3328
before_cy = 1794
# 改成补偿后人工点击得到的坐标
after_cx = 2470
after_cy = 2618

center_x = (width - 1) / 2.0
center_y = (height - 1) / 2.0
mm_per_px = fov_mm / width

before_dx = before_cx - center_x
before_dy = before_cy - center_y
before_norm_px = math.hypot(before_dx, before_dy)
before_norm_mm = before_norm_px * mm_per_px

after_dx = after_cx - center_x
after_dy = after_cy - center_y
after_norm_px = math.hypot(after_dx, after_dy)
after_norm_mm = after_norm_px * mm_per_px

improve = (before_norm_px - after_norm_px) / before_norm_px * 100 if before_norm_px else 0

print("image_center =", [center_x, center_y])
print("before_offset_px =", [before_dx, before_dy])
print("before_norm_px =", before_norm_px)
print("before_norm_mm =", before_norm_mm)
print("after_offset_px =", [after_dx, after_dy])
print("after_norm_px =", after_norm_px)
print("after_norm_mm =", after_norm_mm)
print("error_reduction_percent =", improve)