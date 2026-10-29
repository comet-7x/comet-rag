from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from comet_rag._async import run_sync
from comet_rag.content import (
    ContentInput,
    ContentItem,
    ImagePart,
    TextPart,
    content_parts,
)

from ..types import RankedItem, RerankResult, _candidates


def _text(content: ContentInput) -> str:
    parts = content_parts(content)
    if any(isinstance(part, ImagePart) for part in parts):
        raise ValueError("local cross encoder does not support images")
    return "".join(part.text for part in parts if isinstance(part, TextPart))


class AsyncLocalReranker:
    """逐对单 logit cross encoder；不截断输入，注入的模型由创建者持有。"""

    def __init__(
        self,
        *,
        model: Any,
        tokenizer: Any,
        model_id: str,
        revision: str,
        device: str = "cpu",
        max_tokens: int | None = None,
        batch_size: int = 16,
    ) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (model_id, revision, device)
        ):
            raise ValueError("model_id, revision and device must be nonblank")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
            raise ValueError("max_tokens must be a positive integer")
        if getattr(model.config, "num_labels", None) != 1:
            raise ValueError("local cross encoder requires exactly one relevance logit")
        limits = [
            getattr(model.config, "max_position_embeddings", None),
            getattr(tokenizer, "model_max_length", None),
            max_tokens,
        ]
        limits = [value for value in limits if type(value) is int and value > 0]
        if not limits or min(limits) >= 1_000_000_000:
            raise ValueError("a finite model token budget is required")
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self.max_tokens = min(limits)
        self.batch_size = batch_size
        self.model = model.eval().to(device)
        self.tokenizer = tokenizer
        self._lock = threading.Lock()

    @classmethod
    async def load(
        cls,
        model_dir: Path,
        *,
        model_id: str,
        revision: str,
        device: str = "cpu",
        max_tokens: int | None = None,
        batch_size: int = 16,
    ) -> AsyncLocalReranker:
        directory = Path(model_dir).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("model_dir must be an existing local directory")

        def load_model() -> AsyncLocalReranker:
            transformers = import_module("transformers")
            model = transformers.AutoModelForSequenceClassification.from_pretrained(
                directory,
                local_files_only=True,
                use_safetensors=True,
                trust_remote_code=False,
            )
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                directory, local_files_only=True, trust_remote_code=False
            )
            return cls(
                model=model,
                tokenizer=tokenizer,
                model_id=model_id,
                revision=revision,
                device=device,
                max_tokens=max_tokens,
                batch_size=batch_size,
            )

        return await run_sync(load_model)

    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult:
        items = _candidates(candidates)
        query_text = _text(query)
        texts = tuple(_text(item.content) for item in items)
        if not items:
            return RerankResult((), self.model_id, "logit", revision=self.revision)
        encoded = await run_sync(self._encode, query_text, texts)
        scores: list[float] = []
        for start in range(0, len(items), self.batch_size):
            features = [
                {key: values[index] for key, values in encoded.items()}
                for index in range(start, min(start + self.batch_size, len(items)))
            ]
            scores.extend(await run_sync(self._score_batch, features))
        order = sorted(range(len(items)), key=lambda index: scores[index], reverse=True)
        result = RerankResult(
            tuple(
                RankedItem(items[index].id, rank, scores[index])
                for rank, index in enumerate(order, 1)
            ),
            self.model_id,
            "logit",
            revision=self.revision,
        )
        result.validate(items)
        return result

    def _encode(self, query: str, texts: tuple[str, ...]) -> dict[str, Any]:
        with self._lock:
            encoded = self.tokenizer(
                [query] * len(texts),
                list(texts),
                add_special_tokens=True,
                padding=False,
                truncation=False,
            )
            ids = encoded["input_ids"]
            if len(ids) != len(texts) or any(
                len(tokens) > self.max_tokens for tokens in ids
            ):
                raise ValueError(
                    "query/candidate pair exceeds cross encoder token budget or count"
                )
            if any(len(values) != len(texts) for values in encoded.values()):
                raise ValueError("tokenizer output must cover every candidate")
            return encoded

    def _score_batch(self, features: list[dict[str, Any]]) -> list[float]:
        torch = import_module("torch")
        with self._lock, torch.inference_mode():
            inputs = self.tokenizer.pad(features, padding=True, return_tensors="pt").to(
                self.device
            )
            logits = self.model(**inputs).logits
            if tuple(logits.shape) != (len(features), 1):
                raise ValueError("model must return one logit for every candidate")
            values = logits.detach().cpu().tolist()
            scores: list[float] = []
            for value in values:
                score = value[0]
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise ValueError("model logits must be finite numbers")
                scores.append(float(score))
            return scores
