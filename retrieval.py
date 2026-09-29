from __future__ import annotations

from collections import Counter
import math
import re

from .models import DocumentRecord, RetrievedContext


TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def tokenize(text: str) -> list[str]:
    return [normalize_token(token) for token in TOKEN.findall(text.lower()) if token not in STOPWORDS]


def normalize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


class Retriever:
    def __init__(self, documents: list[DocumentRecord]) -> None:
        self.documents = documents
        self.chunk_index: list[tuple[DocumentRecord, str, Counter[str]]] = []
        self.document_frequency: Counter[str] = Counter()
        for document in documents:
            doc_terms = set()
            for chunk in document.chunks:
                counts = Counter(tokenize(chunk))
                if not counts:
                    continue
                self.chunk_index.append((document, chunk, counts))
                doc_terms.update(counts.keys())
            self.document_frequency.update(doc_terms)

    def search(self, query: str, top_k: int = 3) -> list[RetrievedContext]:
        query_counts = Counter(tokenize(query))
        if not query_counts or not self.chunk_index:
            return []
        scored: list[RetrievedContext] = []
        total_docs = max(1, len(self.documents))
        for document, chunk, counts in self.chunk_index:
            score = 0.0
            for token, query_count in query_counts.items():
                if token not in counts:
                    continue
                idf = math.log((1 + total_docs) / (1 + self.document_frequency[token])) + 1.0
                score += min(counts[token], query_count) * idf
            if score > 0:
                normalized_score = score / max(1.0, len(query_counts))
                scored.append(RetrievedContext(path=document.path, chunk=chunk, score=round(normalized_score, 4),
                                               metadata=document.metadata, content_hash=document.content_hash))
        scored.sort(key=lambda item: (-item.score, item.path))
        return scored[:top_k]


def confidence_from_contexts(contexts: list[RetrievedContext], query: str) -> float:
    terms = set(tokenize(query))
    if not terms or not contexts:
        return 0.0
    haystack = " ".join(context.chunk.lower() for context in contexts)
    covered = sum(1 for term in terms if term in haystack)
    coverage = covered / len(terms)
    score = min(1.0, sum(context.score for context in contexts) / max(1.0, len(terms)))
    return round((coverage * 0.7) + (score * 0.3), 4)
