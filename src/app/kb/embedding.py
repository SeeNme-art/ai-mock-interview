"""Pinned Chinese text embeddings. Only explicit model preparation uses the network."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from typing import Iterable, Sequence
from urllib.request import urlopen

from fastembed import TextEmbedding
import numpy as np

MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
ASSETS = {
    "config.json": "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    "model_optimized.onnx": "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38",
}
DOWNLOAD_URLS = {
    "huggingface": f"https://huggingface.co/Qdrant/bge-small-zh-v1.5/resolve/{REVISION}/",
    "modelscope": "https://modelscope.cn/api/v1/models/Qdrant/bge-small-zh-v1.5/repo?Revision=master&FilePath=",
}


def _valid_asset(path: Path, expected: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == expected


def prepare_model(directory: Path, source: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in ASSETS.items():
        path = directory / name
        if _valid_asset(path, expected):
            continue
        local = NamedTemporaryFile(dir=directory, suffix=".download", delete=False)
        temporary = Path(local.name)
        try:
            with local, urlopen(DOWNLOAD_URLS[source] + name, timeout=30) as remote:
                shutil.copyfileobj(remote, local)
            if not _valid_asset(temporary, expected):
                raise ValueError(f"下载文件 {name} 与固定模型快照的校验值不匹配")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class LocalChineseEmbedder:
    dimension = 512

    def __init__(self, directory: Path) -> None:
        for name, expected in ASSETS.items():
            if not _valid_asset(directory / name, expected):
                raise ValueError(f"本地模型文件 {name} 缺失或校验失败，请先运行 prepare-model")
        self.model_id = (
            f"{MODEL}@{REVISION}/fastembed-{version('fastembed')}"
            f"/onnxruntime-{version('onnxruntime')}/tokenizers-{version('tokenizers')}"
        )
        self._model = TextEmbedding(model_name=MODEL, specific_model_path=str(directory.resolve()),
                                    cache_dir=str(directory.resolve()), local_files_only=True,
                                    providers=["CPUExecutionProvider"], threads=2)

    def embed(self, texts: Sequence[str]) -> Iterable[np.ndarray]:
        return self._model.embed(texts, batch_size=16)
