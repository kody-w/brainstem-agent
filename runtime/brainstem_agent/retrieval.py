"""Retrieval core of the learning cell: tokens, BM25, recency and usage, snippets.

One ranking function serves every knowledge kind (workspace facts, profile facts,
skills, instruction-file sections and, in the fallback engine, past turns), so the
context budget and the search tools agree on what "relevant" means.

- Lexical relevance is Okapi BM25 with the Lucene IDF ``ln(1 + (N - df + .5)/(df + .5))``,
  which stays positive in the tiny collections a young cell has (FTS5's classic IDF is
  clamped to ~0 for a term in half the documents, so every match ties).
- An item is a *match* when it contains at least ``min_coverage`` of the query's distinct
  terms and scores at least ``relative_threshold`` of the best match (a weak partial match
  on one common word of a long request is not offered). Coverage, unlike a raw BM25
  cut-off, does not depend on how many items the collection holds.
- Final score = normalised BM25 + ``recency_weight`` x recency + ``usage_weight`` x usage,
  where recency halves every ``half_life_days`` and usage saturates at 20 uses.
  Non-matching items rank after every match by recency and usage alone; callers decide
  how many of them may fill a leftover budget (``memory_fill``, ``skill_fill``).

The shipped ``DEFAULT_PARAMS`` were chosen offline on the labelled evaluation set in
``runtime/tests/retrieval_eval.py`` (tuning split), without live turns.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

__all__ = ["DEFAULT_PARAMS", "Item", "Params", "Scored", "bm25", "query_text", "rank", "snippet",
           "stem", "terms", "words"]

STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing done down during each else
ever few for from further had has have having he her here hers him his how i if in into is
it its itself just me more most my myself no nor not now of off on once only or other our
ours out over own please same she should so some such than that the their theirs them then
there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself earlier told tell remember
recall say said ask asked answer word one ok okay thanks thank hi hello let lets us
""".split())
_WORD = re.compile(r"[^\W_]+")
# The header schedules.run_occurrence puts before a schedule's own prompt.
_RUN_HEADER = re.compile(r"^\[Brainstem Agent (?:scheduled|manual) run of schedule [^\n]*?\]\n")
_USAGE_SATURATION = math.log1p(20)


def query_text(text: str) -> str:
    """The part of a turn's input that expresses the request (no scheduled-run header)."""
    return _RUN_HEADER.sub("", text or "", count=1)


def _undouble(word: str) -> str:
    if len(word) > 2 and word[-1] == word[-2] and word[-1] not in "aeiouylsz":
        return word[:-1]
    return word


def stem(word: str) -> str:
    """A small, deterministic suffix stripper, not a linguistic stemmer: plural -s/-es/-ies,
    -ing and -ed (undoubling a final consonant), then a final -e, so taxes/tax,
    tomatoes/tomato, staged/staging/stage and running/run meet."""
    if len(word) <= 3 or word.isdigit():
        return word
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ing"):
        word = _undouble(word[:-3])
    elif len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        word = _undouble(word[:-2])
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) >= 3 and word.endswith("e"):
        word = word[:-1]
    return word


def words(text: str) -> list[str]:
    """Lower-case words without stopwords or 1-letter words (digits stay), in order."""
    return [word for word in _WORD.findall((text or "").lower())
            if word not in STOPWORDS and (len(word) > 1 or word.isdigit())]


def terms(text: str) -> list[str]:
    return list(_cached_terms(text or ""))


@functools.lru_cache(maxsize=16384)
def _cached_terms(text: str) -> tuple[str, ...]:
    return tuple(stem(word) for word in words(text))


@dataclass(frozen=True)
class Params:
    k1: float = 1.2
    b: float = 0.75
    half_life_days: float = 30.0
    recency_weight: float = 0.15
    usage_weight: float = 0.1
    relative_threshold: float = 0.3
    min_coverage: float = 0.0
    memory_fill: int = 2
    skill_fill: int = 6

    def with_(self, **changes: Any) -> "Params":
        return replace(self, **changes)


# Chosen offline by ``runtime/tests/retrieval_eval.py --tune`` (5,832 configurations on the
# tune split; objective recall-weighted F2 over memory and skills, then fewer characters;
# ties broken toward the prior k1 1.2, b 0.75, recency 0.1, usage 0.05, threshold 0.35,
# coverage 0.15, fills 2/6). The data moved k1, b, the threshold and both fills; recency,
# usage and coverage did not change a single outcome on the set and stay at the prior.
# Held-out test split: retrieved recall 0.96, precision 0.60 (untuned 1.00 / 0.07).
DEFAULT_PARAMS = Params(k1=0.9, b=0.4, half_life_days=30.0, recency_weight=0.1,
                        usage_weight=0.05, relative_threshold=0.5, min_coverage=0.15,
                        memory_fill=0, skill_fill=0)


@dataclass(frozen=True)
class Item:
    """One retrievable piece of knowledge; ``data`` carries whatever the caller renders."""

    key: str
    text: str
    updated_at: float = 0.0
    uses: int = 0
    boost: float = 0.0
    data: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class Scored:
    item: Item
    score: float
    lexical: float
    raw: float
    coverage: float
    matched: bool


def bm25(query: Sequence[str], documents: Sequence[Sequence[str]], *, k1: float = 1.2,
         b: float = 0.75) -> list[float]:
    """Okapi BM25 of each tokenised document for the distinct query terms (Lucene IDF)."""
    wanted = list(dict.fromkeys(query))
    count = len(documents)
    if not wanted or not count:
        return [0.0] * count
    lengths = [len(document) for document in documents]
    average = (sum(lengths) / count) or 1.0
    frequencies = []
    document_frequency = dict.fromkeys(wanted, 0)
    for document in documents:
        seen: dict[str, int] = {}
        for term in document:
            if term in document_frequency:
                seen[term] = seen.get(term, 0) + 1
        for term in seen:
            document_frequency[term] += 1
        frequencies.append(seen)
    idf = {term: math.log(1 + (count - df + 0.5) / (df + 0.5))
           for term, df in document_frequency.items()}
    scores = []
    for seen, length in zip(frequencies, lengths):
        norm = k1 * (1 - b + b * length / average)
        scores.append(sum(idf[term] * tf * (k1 + 1) / (tf + norm) for term, tf in seen.items()))
    return scores


def _recency(updated_at: float, now: float, half_life_days: float) -> float:
    if not updated_at or half_life_days <= 0:
        return 0.0
    age_days = max(0.0, now - updated_at) / 86400.0
    return 0.5 ** (age_days / half_life_days)


def _usage(uses: int) -> float:
    return min(1.0, math.log1p(max(0, uses)) / _USAGE_SATURATION)


def rank(query: str, items: Iterable[Item], *, params: Params = DEFAULT_PARAMS,
         now: float) -> list[Scored]:
    """Every item, best first: matches (see module doc), then the rest by recency and usage."""
    items = list(items)
    tokens = [terms(item.text) for item in items]
    wanted = terms(query)
    distinct = set(wanted)
    raw = bm25(wanted, tokens, k1=params.k1, b=params.b)
    best = max(raw, default=0.0)
    scored = []
    for item, document, value in zip(items, tokens, raw):
        lexical = value / best if best > 0 else 0.0
        coverage = len(distinct.intersection(document)) / len(distinct) if distinct else 0.0
        matched = (value > 0 and coverage >= params.min_coverage
                   and lexical >= params.relative_threshold)
        signal = (params.recency_weight * _recency(item.updated_at, now, params.half_life_days)
                  + params.usage_weight * _usage(item.uses) + item.boost)
        scored.append(Scored(item, (lexical if matched else 0.0) + signal, lexical, value,
                             coverage, matched))
    # Matches first; ties broken by recency, then key, so the order is deterministic.
    scored.sort(key=lambda entry: (not entry.matched, -entry.score, -entry.item.updated_at,
                                   entry.item.key))
    return scored


def snippet(text: str, query: str, width: int = 180) -> str:
    """The ``width``-character window of ``text`` with the most query-term hits."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    wanted = set(terms(query))
    positions = [match.start() for match in _WORD.finditer(flat.lower())
                 if stem(match.group()) in wanted]
    if not positions:
        return flat[: width - 3].rstrip() + "..."
    best_start, best_hits = positions[0], 0
    for start in positions:
        hits = sum(1 for position in positions if start <= position < start + width)
        if hits > best_hits:
            best_start, best_hits = start, hits
    start = max(0, min(best_start - width // 4, len(flat) - width))
    piece = flat[start:start + width].strip()
    return ("..." if start > 0 else "") + piece + ("..." if start + width < len(flat) else "")
