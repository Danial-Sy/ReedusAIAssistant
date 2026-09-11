"""a local, sharded, self-building retrieval corpus."""
from .retrieve import Knowledge          # noqa: F401
from .store import KnowledgeStore, Passage   # noqa: F401
from . import domains                    # noqa: F401

__all__ = ["Knowledge", "KnowledgeStore", "Passage", "domains"]
