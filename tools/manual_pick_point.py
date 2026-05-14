from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image_path")
    args = parser.parse_args()

    image_path = Path(args.image_path)
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"无法读取图像: {image_path}")

    display = img.copy()
    points = []

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.clear()
            points.append((x, y))
            vis = display.copy()
            cv2.drawMarker(
                vis,
                (x, y),
                (0, 0, 255),
                markerType=cv2.MARKER_CROSS,
                markerSize=40,
                thickness=2,
            )
            cv2.imshow("click target center", vis)
            print(f"clicked center_pixel = [{x}, {y}]")

    cv2.namedWindow("click target center", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("click target center", 1200, 900)
    cv2.setMouseCallback("click target center", on_mouse)

    print("左键点击目标中心；按 q 或 ESC 退出。")
    cv2.imshow("click target center", display)

    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (27, ord("q")):
            break

    cv2.destroyAllWindows()

    if points:
        x, y = points[0]
        print()
        print("最终选择:")
        print(f"center_pixel_x = {x}")
        print(f"center_pixel_y = {y}")
    else:
        print("未选择点。")


if __name__ == "__main__":
    main()