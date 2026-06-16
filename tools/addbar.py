import cv2
from vision.vision.postprocess import draw_scale_bar

overlay_image_path = r"C:\colony_system\fig\fig4\06_overlay.bmp"
mm_per_pixel = 0.00062890625

# OpenCV uses BGR color order, not RGB.
scale_bar_style = {
    "enabled": True,
    "mm_per_pixel": mm_per_pixel,
    "length_mm": 1.0,
    "position": "bottom_right",
    "color_bgr": (0, 255, 0),      # main bar/text color: white
    "outline_bgr": (0, 0, 0),          # outline color: black
    "thickness": 10,                   # bar/tick thickness in pixels
    "font_scale": 3,                 # label size
    "font_thickness": 3,               # label stroke thickness
    "margin_px": 120,                  # distance from image border
}

img = cv2.imread(overlay_image_path, cv2.IMREAD_COLOR)
if img is None:
    raise FileNotFoundError(f"Failed to read overlay image: {overlay_image_path}")

draw_scale_bar(img, scale_bar_style)

cv2.imwrite(overlay_image_path.replace("06_overlay.bmp", "06_overlay_scaled.bmp"), img)
