"""Identifier normalization, sub-word token splitting, and search query sanitization."""

import re

# Regex to split CamelCase, PascalCase, snake_case, and kebab-case
_CAMEL_RE = re.compile(r"([A-Z]+(?=[A-Z][a-z0-9])|[A-Z][a-z0-9]+|[a-z0-9]+)")
_NON_WORD_RE = re.compile(r"[^a-zA-Z0-9_]+")


def split_identifier_subwords(identifier: str) -> list[str]:
    """Split a compound programming identifier into its constituent sub-words."""
    if not identifier:
        return []

    # Clean punctuation
    cleaned = _NON_WORD_RE.sub(" ", identifier).strip()
    words: list[str] = []

    for chunk in cleaned.split():
        # Split on snake_case
        subparts = chunk.split("_")
        for part in subparts:
            if not part:
                continue
            # Match camelCase/PascalCase boundaries
            matches = _CAMEL_RE.findall(part)
            if matches:
                words.extend(matches)
            else:
                words.append(part)

    # Return lowercase unique constituent words
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        wl = w.lower()
        if len(wl) >= 2 and wl not in seen:
            seen.add(wl)
            result.append(wl)
    return result


def extract_path_tokens(path: str) -> list[str]:
    """Extract path segments, filename, extension, and subwords from a file path."""
    parts = path.replace("\\", "/").split("/")
    tokens: list[str] = []
    seen: set[str] = set()

    for p in parts:
        if not p or p in (".", ".."):
            continue
        # Base name and extension
        dot_parts = p.split(".")
        for dp in dot_parts:
            if dp and dp.lower() not in seen:
                seen.add(dp.lower())
                tokens.append(dp.lower())
            subwords = split_identifier_subwords(dp)
            for sw in subwords:
                if sw not in seen:
                    seen.add(sw)
                    tokens.append(sw)
    return tokens


def build_search_terms(query: str) -> list[str]:
    """Sanitize and tokenize search query for safe tsquery generation."""
    cleaned = _NON_WORD_RE.sub(" ", query).strip()
    tokens = cleaned.split()
    expanded: list[str] = []
    seen: set[str] = set()

    for tok in tokens:
        tl = tok.lower()
        if tl not in seen:
            seen.add(tl)
            expanded.append(tl)
        for sw in split_identifier_subwords(tok):
            if sw not in seen:
                seen.add(sw)
                expanded.append(sw)
    return expanded
