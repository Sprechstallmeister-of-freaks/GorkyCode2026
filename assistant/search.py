"""Transparent no-LLM lexical search with small Russian inflection tolerance."""
import re
import unicodedata
from difflib import SequenceMatcher

STOP = frozenset({"задача", "задачу", "задаче", "найди", "найти", "покажи", "про", "по", "мне",
                  "старую", "старый", "когда", "была", "был", "встреча", "встречу", "встречи",
                  "у", "с", "со", "и", "на", "я", "ставил", "ставила", "то", "где", "какой", "еще"})


def words(text: str) -> list[str]:
    cleaned = unicodedata.normalize("NFKC", text.lower()).replace("ё", "е")
    return [word for word in re.findall(r"[а-яa-z0-9]+", cleaned) if word not in STOP]


def match(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) >= 5 and len(b) >= 5 and a[:4] == b[:4]:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def rank(query: str, title: str) -> float:
    qw = words(query)
    tw = words(title)
    if not qw or not tw:
        return 0.0
    per_token = [max(match(q, t) for t in tw) for q in qw]
    # Ignore unrelated records: require at least one clear anchor.
    if max(per_token) < 0.68:
        return 0.0
    coverage = sum(per_token) / len(per_token)
    matched = sum(s >= 0.72 for s in per_token) / len(per_token)
    return 0.7 * coverage + 0.3 * matched
