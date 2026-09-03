from __future__ import annotations

from pathlib import Path

from PIL import Image

from vision.tools.inventory_dataset import build_inventory


def _write_rgb(path: Path, size: tuple[int, int] = (10, 8)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (128, 128, 128)).save(path)


def test_inventory_supports_unicode_paths_without_inferencing_quality(tmp_path: Path) -> None:
    dataset_root = tmp_path / "数据集"
    image_path = dataset_root / "较高质量" / "克隆一.bmp"
    _write_rgb(image_path, (17, 13))

    inventory = build_inventory(dataset_root, objective="4x")

    assert inventory["summary"] == {
        "image_count": 1,
        "readable_image_count": 1,
        "unreadable_image_count": 0,
        "quality_hint_folders_found": ["较高质量"],
    }
    record = inventory["images"][0]
    assert record["relative_path"] == "较高质量/克隆一.bmp"
    assert (record["width"], record["height"], record["channels"]) == (17, 13, 3)
    assert record["annotation_status"] == "pending"
    assert record["objective"] == "4x"
    assert record["source_folder_hint"] == "较高质量"
    assert record["source_folder_hint_is_label"] is False
    assert inventory["objective"]["quality_assessment"] == "not_assessed_at_4x"
    assert not (dataset_root / "dataset_inventory.json").exists()


def test_inventory_scans_supported_extensions_in_stable_order(tmp_path: Path) -> None:
    dataset_root = tmp_path / "source"
    _write_rgb(dataset_root / "b.PNG")
    _write_rgb(dataset_root / "A.jpg")
    _write_rgb(dataset_root / "nested" / "c.JPEG")
    (dataset_root / "ignored.txt").write_text("not an image", encoding="utf-8")

    inventory = build_inventory(dataset_root, objective="4x")

    assert [item["relative_path"] for item in inventory["images"]] == [
        "A.jpg",
        "b.PNG",
        "nested/c.JPEG",
    ]
    assert [item["id"] for item in inventory["images"]] == [1, 2, 3]


def test_inventory_marks_corrupt_supported_files_unreadable(tmp_path: Path) -> None:
    dataset_root = tmp_path / "source"
    dataset_root.mkdir()
    (dataset_root / "broken.bmp").write_bytes(b"not a bitmap")

    inventory = build_inventory(dataset_root, objective="4x")

    assert inventory["summary"]["image_count"] == 1
    assert inventory["summary"]["unreadable_image_count"] == 1
    assert inventory["images"][0]["annotation_status"] == "pending"
    assert "read_error" in inventory["images"][0]
