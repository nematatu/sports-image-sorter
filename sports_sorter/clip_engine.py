"""Optional, offline CLIP comparison with a fixed taxonomy and abstention."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from .core import Prediction, protected_path, sha256_file

MODEL = "openai/clip-vit-base-patch32"
REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
PROMPTS = {
    "バドミントン": "an athlete playing badminton, hitting a shuttlecock with a racket on a badminton court",
    "テニス": "an athlete playing tennis, hitting a tennis ball with a racket on a tennis court",
    "バレーボール": "athletes playing volleyball, hitting a volleyball over the net on a volleyball court",
    "バスケットボール": "athletes playing basketball with a basketball and a hoop on a basketball court",
    "陸上競技": "an athlete competing in track and field athletics, running a race, jumping or throwing in a stadium",
    "水泳": "an athlete swimming a race in a swimming pool with racing lane ropes",
    "卓球": "an athlete playing table tennis with a paddle and a small ball across a ping pong table",
    "サッカー": "athletes playing soccer kicking a football on a soccer pitch",
    "体操": "an athlete performing artistic gymnastics on gymnastic apparatus in a gymnasium",
    "新体操": "an athlete performing rhythmic gymnastics with a ribbon, hoop, clubs or a ball on the floor",
    "ゴルフ": "an athlete playing golf swinging a golf club on a golf course",
    "野球": "athletes playing baseball with a baseball bat and a ball on a baseball diamond",
    "ソフトボール": "athletes playing softball with a softball bat and a ball on a softball field",
    "ラグビー": "athletes playing rugby carrying an oval rugby ball on a rugby pitch",
    "ハンドボール": "athletes playing team handball throwing a small ball at a goal on a handball court",
    "ホッケー": "athletes playing field hockey hitting a ball with hockey sticks on a hockey pitch",
    "スケート": "an athlete skating or figure skating on ice in an ice rink",
    "スキー": "an athlete skiing downhill on snow with skis",
    "自転車競技": "athletes racing bicycles in a cycling race",
    "格闘技": "athletes competing in judo wrestling boxing or martial arts in a ring or on a mat",
    "その他の競技": "an athlete actively competing in a different sport with visible sporting equipment and a playing field",
}
NEGATIVES = [
    "a close up portrait of a person's face, without visible sport equipment or action",
    "a person posing for a portrait in casual clothes, not playing a sport",
    "athletes standing at an awards ceremony holding medals trophies or flowers on a podium",
    "an athlete giving an interview or attending a press conference",
    "a person posing in a sports uniform without visible sporting action or equipment",
    "a person in a swimsuit posing on a beach or beside a swimming pool, not swimming a race",
    "an audience, spectators or cheerleaders watching a sporting event",
    "a photograph of an object, food, landscape, document or a screenshot",
]


def decide(scores, threshold=0.26, margin=0.025):
    if len(scores) != len(PROMPTS) + len(NEGATIVES):
        raise ValueError("Unexpected number of CLIP scores")
    labels = [*PROMPTS, *(["未分類"] * len(NEGATIVES))]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    best = order[0]
    score = float(scores[best])
    competing = max(float(scores[i]) for i in order[1:] if labels[i] != labels[best])
    label = labels[best]
    if score < threshold or score - competing < margin:
        label = "未分類"
    candidates = [{"label": labels[i], "cosine": float(scores[i]), "prompt": ([*PROMPTS.values(), *NEGATIVES])[i]} for i in order[:5]]
    return Prediction(label, max(0.0, min(1.0, score)), "CLIP text/image similarity; uncalibrated candidate, visual review required", {"candidates": candidates, "threshold": threshold, "margin": margin, "calibrated": False})


def model_directory():
    return protected_path(os.environ.get("SPORTS_CLIP_MODEL", str(Path.home() / ".cache/sports-image-sorter/clip-vit-base-patch32")))


class ClipEngine:
    def __init__(self):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        try:
            import torch
            import transformers
            from transformers import CLIPModel, CLIPProcessor
        except ImportError as error:
            raise RuntimeError("Install the optional clip extra and prepare model weights first") from error
        directory = model_directory()
        if not (directory / "pytorch_model.bin").is_file():
            raise RuntimeError("Model weights are absent. Run python -m sports_sorter.clip_engine download first")
        torch.set_num_threads(1)
        self.torch = torch
        self.processor = CLIPProcessor.from_pretrained(directory, local_files_only=True, use_fast=False)
        self.model = CLIPModel.from_pretrained(directory, local_files_only=True, use_safetensors=False).eval().to("cpu")
        prompts = [f"a photograph of {text}" for text in PROMPTS.values()] + [f"a photograph of {text}" for text in NEGATIVES]
        inputs = self.processor(text=prompts, return_tensors="pt", padding=True)
        with torch.inference_mode():
            vectors = self.model.get_text_features(**inputs)
            self.text_vectors = vectors / vectors.norm(dim=-1, keepdim=True)
        self.config = {"engine": "clip", "model": MODEL, "revision": REVISION, "weights_sha256": sha256_file(directory / "pytorch_model.bin"), "prompts": prompts, "threshold": 0.26, "margin": 0.025, "torch": torch.__version__, "transformers": transformers.__version__, "preprocess": "rgb-orient-1024-center224-v1"}
        self.fingerprint = "clip-" + hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()[:20]

    def classify(self, pixels):
        inputs = self.processor(images=pixels, return_tensors="pt")
        with self.torch.inference_mode():
            vector = self.model.get_image_features(**inputs)
            vector = vector / vector.norm(dim=-1, keepdim=True)
            scores = (vector @ self.text_vectors.T)[0].tolist()
        return decide(scores)


def download():
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import snapshot_download
    directory = model_directory()
    snapshot_download(MODEL, revision=REVISION, local_dir=directory, token=False, allow_patterns=["config.json", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.json", "merges.txt", "pytorch_model.bin"], max_workers=1)
    print("Model files prepared. Inference uses local_files_only=True and one CPU thread.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare optional CLIP weights; no input images are sent")
    parser.add_argument("action", choices=["download"])
    parser.parse_args()
    download()
