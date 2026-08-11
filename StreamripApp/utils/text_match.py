"""Small, dependency-free string-similarity helpers.

Extracted from the retired WordPiece semantic engine (see
deprecated_feature/README.md). The Levenshtein pass was the only part of that
module measurably earning its keep — it is what lets Settings search resolve
typos like "haptik" → Haptic Feedback — so it survived the removal while the
30,522-token vocabulary and its VSM did not.

Pure Python, no imports: safe on Android, runs in microseconds at the string
lengths this app deals with (search queries and setting keywords).
"""


def levenshtein_distance(s1: str, s2: str) -> int:
    """Edit distance between two strings, computed with a rolling row."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


__all__ = ["levenshtein_distance"]
