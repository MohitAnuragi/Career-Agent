"""Download the embedding model into ./models at build time (used as Vercel's build step).

Bundling the 65 MB ONNX model with the deployment means a cold start loads it from disk in about a second,
instead of downloading it from Hugging Face inside the user's request.

    python scripts/fetch_model.py
"""
from pathlib import Path

from huggingface_hub import snapshot_download

# fastembed's "BAAI/bge-small-en-v1.5" is this quantised ONNX export; the index is built with it.
REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
TARGET = Path(__file__).resolve().parent.parent / "models" / "bge-small-en-v1.5"
FILES = ["model_optimized.onnx", "tokenizer.json", "config.json", "special_tokens_map.json", "tokenizer_config.json"]


def main() -> None:
    snapshot_download(repo_id=REPO, local_dir=TARGET, allow_patterns=FILES)
    missing = [f for f in FILES if not (TARGET / f).exists()]
    if missing:
        raise SystemExit(f"model download incomplete, missing: {missing}")
    size = sum((TARGET / f).stat().st_size for f in FILES) / 1e6
    print(f"embedding model ready at {TARGET} ({size:.0f} MB)")


if __name__ == "__main__":
    main()
