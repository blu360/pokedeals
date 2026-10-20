"""Text/ID normalisation shared by the sync (DB side) and the parser (title side).

Both sides MUST use the same functions, otherwise lookups silently miss.
"""
from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LOCAL_ID = re.compile(r"^([A-Za-z]*)0*(\d+)$")


def name_key(text: str) -> str:
    """'Pokémon  Mr. Mime' -> 'pokemon mr mime' (accents stripped, punctuation -> space)."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", ascii_text.lower()).strip()


def local_key(local_id: str) -> str:
    """Card number normalised for comparison: '004' -> '4', 'TG01' -> 'TG1', 'swsh001' -> 'SWSH1'."""
    m = _LOCAL_ID.match(local_id.strip())
    if not m:
        return local_id.strip().upper()
    prefix, digits = m.groups()
    return f"{prefix.upper()}{int(digits)}"
