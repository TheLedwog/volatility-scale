"""One-shot: build the dataset and train the model.

    python -m app.ml.build
"""
from __future__ import annotations

from .dataset import build_dataset
from .train import train


def run() -> dict:
    df = build_dataset()
    print(f"[build] dataset rows: {len(df)}")
    result = train(df=df)
    if result.get("error"):
        print("[build] " + result["error"])
    else:
        print(f"[build] trained on {result['n_samples']} sessions; "
              f"Spearman {result['spearman']}, "
              f"lift {result['lift_bottom_third_er']} -> {result['lift_top_third_er']}")
    return result


if __name__ == "__main__":
    run()
