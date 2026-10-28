from __future__ import annotations

import io
import threading
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from comet_rag._async import run_sync
from comet_rag.content import ContentItem, ImagePart, content_parts, text_view
from comet_rag.identity import file_hash

from ..types import EmbeddingResult, EmbeddingSpace, vector_values


class AsyncCLIPEmbedder:
    """本地 CLIP 的独立文本/图片共享空间；联合图文输入明确拒绝。"""

    def __init__(
        self,
        *,
        model: Any,
        processor: Any,
        space: EmbeddingSpace,
        device: str = "cpu",
        max_media_bytes: int = 20 * 1024 * 1024,
        max_pixels: int = 40_000_000,
    ) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (max_media_bytes, max_pixels)
        ):
            raise ValueError("media budgets must be positive integers")
        self.model = model.eval().to(device)
        self.processor = processor
        self.space = space
        self.device = device
        self.max_media_bytes = max_media_bytes
        self.max_pixels = max_pixels
        self._lock = threading.Lock()

    @classmethod
    async def load(
        cls, model_dir: Path, *, model_id: str, revision: str, device: str = "cpu"
    ) -> AsyncCLIPEmbedder:
        def load_model() -> AsyncCLIPEmbedder:
            transformers = import_module("transformers")
            model = transformers.CLIPModel.from_pretrained(
                model_dir, local_files_only=True, use_safetensors=True
            )
            processor = transformers.CLIPProcessor.from_pretrained(
                model_dir, local_files_only=True, use_fast=False
            )
            files = {
                path.name: file_hash(path)
                for path in sorted(Path(model_dir).iterdir())
                if path.is_file()
                and path.name
                in {
                    "model.safetensors",
                    "config.json",
                    "preprocessor_config.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "special_tokens_map.json",
                    "vocab.json",
                    "merges.txt",
                }
            }
            space = EmbeddingSpace(
                model_id,
                model.config.projection_dim,
                "local-transformers",
                version=revision,
                normalized=True,
                preprocessing={"adapter": "clip_v1", "files": files, "use_fast": False},
            )
            return cls(model=model, processor=processor, space=space, device=device)

        return await run_sync(load_model)

    async def embed(
        self, items: Sequence[ContentItem], *, role: Literal["query", "document"]
    ) -> tuple[EmbeddingResult, ...]:
        if role not in ("query", "document") or len({item.id for item in items}) != len(
            items
        ):
            raise ValueError("invalid embedding role or duplicate IDs")
        return await run_sync(self._embed, tuple(items))

    def _embed(self, items: tuple[ContentItem, ...]) -> tuple[EmbeddingResult, ...]:
        torch = import_module("torch")
        Image = import_module("PIL.Image")

        result = []
        with self._lock, torch.inference_mode():
            for item in items:
                parts = content_parts(item.content)
                images = [
                    part.resource for part in parts if isinstance(part, ImagePart)
                ]
                text = text_view(item.content)
                if images:
                    if len(images) != 1 or text:
                        raise ValueError(
                            "CLIP supports one image or text per item; joint input requires another model"
                        )
                    resource = images[0]
                    if resource.path is not None:
                        with Path(resource.path).open("rb") as stream:
                            data = stream.read(self.max_media_bytes + 1)
                    elif resource.data is not None:
                        data = resource.data
                    else:
                        raise ValueError("CLIP requires local or inline image bytes")
                    if len(data) > self.max_media_bytes:
                        raise ValueError("image exceeds byte budget")
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > self.max_pixels:
                            raise ValueError("image exceeds pixel budget")
                        image.load()
                        inputs = self.processor(
                            images=image.convert("RGB"), return_tensors="pt"
                        )
                        vector = self.model.get_image_features(**inputs.to(self.device))
                else:
                    inputs = self.processor(
                        text=[text], padding=True, truncation=False, return_tensors="pt"
                    )
                    if (
                        inputs["input_ids"].shape[-1]
                        > self.model.config.text_config.max_position_embeddings
                    ):
                        raise ValueError("text exceeds CLIP token budget")
                    vector = self.model.get_text_features(**inputs.to(self.device))
                vector = vector / vector.norm(dim=-1, keepdim=True)
                result.append(
                    EmbeddingResult(
                        item.id,
                        vector_values(vector[0].cpu().tolist(), self.space.dimension),
                        self.space.space_id,
                    )
                )
        return tuple(result)
