"""Shared deterministic ID generation for the eval dataset — derived from the caller's
seeded `random.Random`, never `uuid4()`, so a fixed seed reproduces identical IDs.
"""

import random


def random_id(rng: random.Random, prefix: str) -> str:
    return f"{prefix}_{rng.getrandbits(48):012x}"
