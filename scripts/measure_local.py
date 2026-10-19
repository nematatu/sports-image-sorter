"""Measure one explicit local inference pass; never reuse prediction-cache hits."""

import argparse
from collections import Counter
import json
import resource
import sys
import time

from sports_sorter.core import Database, effective_rows, load_pixels, safe_source_file, sha256_file
from sports_sorter.engines import load_engine


def measure(db_path, engine_name, revision=2):
    before = time.monotonic()
    engine = load_engine(engine_name, revision=revision)
    ready = time.monotonic()
    counts = Counter()
    with Database(db_path) as db:
        rows = effective_rows(db)
        for row in rows:
            try:
                path = safe_source_file(db, row)
                if sha256_file(path) != row["sha256"]:
                    raise ValueError("Source differs from inventory")
                result = engine.classify(load_pixels(path))
                if sha256_file(path) != row["sha256"]:
                    raise ValueError("Source changed during inference")
                counts[result.label] += 1
            except Exception:
                counts["errors"] += 1
    after = time.monotonic()
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    scale = 1 if sys.platform == "darwin" else 1024
    return {"engine_key": engine.fingerprint, "images": len(rows), "counts": dict(counts),
            "init_seconds": ready-before, "inference_seconds": after-ready, "wall_seconds": after-before,
            "python_max_rss_bytes": own.ru_maxrss*scale, "child_max_rss_bytes": child.ru_maxrss*scale,
            "python_cpu_seconds": own.ru_utime+own.ru_stime, "child_cpu_seconds": child.ru_utime+child.ru_stime,
            "runs": 1, "note": "One pass; no speed ranking. Child peak includes helper/compiler if run. RSS is not whole-Mac memory use."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--engine", choices=["vision", "clip"], required=True)
    parser.add_argument("--revision", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(measure(args.db, args.engine, args.revision), ensure_ascii=False, indent=2))
