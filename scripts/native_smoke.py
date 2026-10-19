"""Exercise native Vision against generated pixels on macOS."""

from PIL import Image
from sports_sorter.engines import VisionEngine


engine = VisionEngine()
result = engine.classify(Image.new("RGB", (320, 320), "gray"))
assert result.label == "未分類", result
assert result.raw["observations"]
print("Native Vision classified generated pixels and abstained.")
