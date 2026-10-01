"""Detection of "I don't know" answers (CT-45, INTV-07, EVAL-09).

An answer that only states the absence of knowledge is accepted as answered and scored 0
without calling the LLM. Detection is an exact match against a fixed list of phrases after
normalization (Unicode compatibility form, case folding, punctuation and control characters
removed, whitespace collapsed), so an answer that says more than that is always evaluated.
"""

import re
import unicodedata

__all__ = ["DONT_KNOW_PHRASES", "is_dont_know"]

# Phrases in normalized form: lowercase, no punctuation (apostrophes removed), single spaces.
DONT_KNOW_PHRASES = frozenset(
    {
        "i dont know",
        "i do not know",
        "dont know",
        "do not know",
        "i dont know sorry",
        "sorry i dont know",
        "i dont know the answer",
        "i do not know the answer",
        "idk",
        "no idea",
        "i have no idea",
        "i got no idea",
        "no clue",
        "i have no clue",
        "dunno",
        "i dunno",
        "not sure",
        "im not sure",
        "i am not sure",
        "i dont remember",
        "i do not remember",
        "i cant answer",
        "i cannot answer",
        "no answer",
    }
)

_APOSTROPHES = str.maketrans({"’": "", "‘": "", "ʼ": "", "'": "", "`": ""})
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_APOSTROPHES).casefold()
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc" and char not in "\t\n\r":
            # NUL and other control characters are dropped, never treated as separators.
            continue
        if category[0] in {"L", "N"}:
            kept.append(char)
        else:
            kept.append(" ")
    return _WHITESPACE_RE.sub(" ", "".join(kept)).strip()


def is_dont_know(text: str) -> bool:
    """Return whether ``text`` only states that the candidate does not know the answer."""
    return _normalize(text) in DONT_KNOW_PHRASES
