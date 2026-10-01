"""Resume language check (CV-14, DA-15)."""

from langdetect import DetectorFactory, detect  # type: ignore[import-untyped]
from langdetect.lang_detect_exception import (  # type: ignore[import-untyped]
    LangDetectException,
)

# Fixed seed makes langdetect deterministic across runs (DA-15).
DetectorFactory.seed = 0

ENGLISH_BLOCK_RATIO = 0.6
BLOCK_MIN_LETTERS = 120
MIN_BLOCK_LETTERS = 20


def is_predominantly_english(text: str) -> bool:
    """Return True when at least 60% of the detectable text blocks are English."""
    languages = [lang for lang in (_detect(block) for block in _blocks(text)) if lang]
    if not languages:
        return False
    english = sum(1 for lang in languages if lang == "en")
    return english / len(languages) >= ENGLISH_BLOCK_RATIO


def _blocks(text: str) -> list[str]:
    """Group consecutive lines into blocks with enough letters to be classified."""
    blocks: list[str] = []
    current: list[str] = []
    letters = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        current.append(stripped)
        letters += _letter_count(stripped)
        if letters >= BLOCK_MIN_LETTERS:
            blocks.append(" ".join(current))
            current, letters = [], 0
    if current:
        tail = " ".join(current)
        if blocks and letters < BLOCK_MIN_LETTERS // 2:
            blocks[-1] = f"{blocks[-1]} {tail}"
        else:
            blocks.append(tail)
    return [block for block in blocks if _letter_count(block) >= MIN_BLOCK_LETTERS]


def _detect(block: str) -> str | None:
    try:
        return str(detect(block))
    except LangDetectException:
        return None


def _letter_count(text: str) -> int:
    return sum(1 for char in text if char.isalpha())
