"""Embedding adapters package."""

from app.adapters.embedding.fake import FakeEmbeddingAdapter
from app.adapters.embedding.titan import AmazonTitanEmbeddingAdapter

__all__ = ["AmazonTitanEmbeddingAdapter", "FakeEmbeddingAdapter"]
