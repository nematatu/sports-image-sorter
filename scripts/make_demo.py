"""Create procedural, non-personal fixtures for UI verification."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from sports_sorter.core import Database, Prediction, classify, scan


class DemoEngine:
    def __init__(self, alternate=False):
        self.alternate = alternate
        self.fingerprint = f"synthetic-demo-{int(alternate)}"

    def classify(self, pixels):
        label = "未分類" if self.alternate else "テニス"
        return Prediction(label, 0.5, "Synthetic UI fixture; no sports accuracy claim")


def make_demo(root, count=60):
    root = Path(root).absolute()
    source = root / "input"
    source.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        file = source / f"synthetic-{index:04}.png"
        if file.exists():
            continue
        image = Image.new("RGB", (400, 280), (224, 233, 221))
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 30, 370, 250), outline="white", width=3)
        draw.line((200, 30, 200, 250), fill="white", width=2)
        x = 60 + index % 12 * 20
        draw.ellipse((x, 90, x+50, 140), fill=(180+index % 70, 196, 61), outline="#365a38", width=2)
        draw.text((38, 38), f"SYNTHETIC {index+1}", fill="#253b2a")
        image.save(file)
    with Database(root / "state.sqlite") as db:
        scan(source, db)
        classify(db, DemoEngine())
        classify(db, DemoEngine(alternate=True))
    print("Synthetic demo prepared; labels are fixtures, not model inference.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--count", type=int, default=60)
    args = parser.parse_args()
    make_demo(args.directory, args.count)
