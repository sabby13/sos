"""Risk tiers.

This lives at the package root rather than inside ``tools`` or ``safety`` because both
of those need it, and a shared leaf module is what keeps them from importing each other.
"""

from __future__ import annotations

from enum import Enum


class RiskLevel(str, Enum):
    """How much damage a tool can do if the model gets it wrong.

    SAFE      - observes only; cannot change the machine (list, search, read, get info)
    CAUTION   - creates or changes something recoverable (mkdir, rename, launch an app)
    DANGEROUS - destroys, overwrites, or executes (delete, force-overwrite, run a command)
    """

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"

    @property
    def rank(self) -> int:
        return {"safe": 0, "caution": 1, "dangerous": 2}[self.value]
