"""Download only public inference assets; never upload or replace Hiely weights."""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

from fast_langdetect import LangDetectConfig, LangDetector
from huggingface_hub import hf_hub_download, snapshot_download


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not (root / "api_v2.py").is_file():
        parser.error("root must contain the existing GPT-SoVITS runtime")
    language_cache = root / "GPT_SoVITS/pretrained_models/fast_langdetect"
    language_cache.mkdir(parents=True, exist_ok=True)
    repo = "lj1995/GPT-SoVITS"
    # These public revisions reproduce the existing deployment's asset hashes.
    revision = "336b2ec4e8d4ac74740798dd40af44e74659ecaf"
    snapshot_download(
        repo,
        revision=revision,
        local_dir=root / "GPT_SoVITS/pretrained_models",
        allow_patterns=[
            "chinese-roberta-wwm-ext-large/*",
            "chinese-hubert-base/*",
            "sv/pretrained_eres2netv2w24s4ep4.ckpt",
        ],
        max_workers=2,
    )
    g2pw_repo = "XXXXRT/GPT-SoVITS-Pretrained"
    g2pw_revision = "0c47645e02a7bc3688d7b263b0042c81e3cd82cd"
    archive = hf_hub_download(g2pw_repo, "G2PWModel.zip", revision=g2pw_revision)
    text_dir = root / "GPT_SoVITS/text"
    with zipfile.ZipFile(archive) as model_zip:
        for member in model_zip.infolist():
            target = (text_dir / member.filename).resolve()
            if not target.is_relative_to(text_dir / "G2PWModel"):
                raise ValueError(f"Unexpected archive member: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with model_zip.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    LangDetector(LangDetectConfig(cache_dir=language_cache)).detect("本机部署检查")
    print(json.dumps({"public_models": revision, "g2pw": g2pw_revision}))


if __name__ == "__main__":
    main()
