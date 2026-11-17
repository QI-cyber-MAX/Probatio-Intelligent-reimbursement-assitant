"""Reciprocal rank fusion over independently ordered search batches."""


def fuse_rankings(rankings: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for position, chunk_id in enumerate(ranking, start=1):
            if chunk_id not in seen:
                seen.add(chunk_id)
                scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + position)
    return sorted(scores, key=lambda key: (-scores[key], key))
