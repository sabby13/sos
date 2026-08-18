"""The tiered safety policy.

Given a tool and its parsed arguments, decide one of three things:

    allow          - run it now, say nothing
    confirm        - show the user a preview and wait for yes/no
    block          - refuse, and tell the model why so it can try something else

The default ``tiered`` mode is what makes voice mode usable: read-only tools never
interrupt you, and anything that deletes or executes always does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..config import SafetyConfig
from ..risk import RiskLevel

if TYPE_CHECKING:  # import only for types - avoids a safety <-> tools import cycle
    from ..tools.base import Tool, ToolContext


class Verdict(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.BLOCK


class SafetyPolicy:
    def __init__(self, config: SafetyConfig) -> None:
        self.config = config

    def evaluate(self, tool: Tool, args: object, ctx: ToolContext) -> Decision:
        name = tool.name

        # Explicit config always beats the risk tier, in both directions.
        if name in self.config.always_confirm:
            return Decision(Verdict.CONFIRM, "listed in safety.always_confirm")
        if name in self.config.auto_approve:
            return Decision(Verdict.ALLOW, "listed in safety.auto_approve")

        mode = self.config.mode
        if mode == "auto":
            return Decision(Verdict.ALLOW, "safety.mode is 'auto'")
        if mode == "strict":
            if tool.risk is RiskLevel.SAFE:
                return Decision(Verdict.ALLOW, "read-only")
            return Decision(Verdict.CONFIRM, "safety.mode is 'strict'")

        # tiered (the default)
        if tool.risk is RiskLevel.SAFE:
            return Decision(Verdict.ALLOW, "read-only")
        if tool.risk is RiskLevel.CAUTION:
            return Decision(Verdict.ALLOW, "reversible change")
        return Decision(Verdict.CONFIRM, "destructive or executes code")

    def describe(self) -> str:
        cfg = self.config
        bits = [f"mode={cfg.mode}"]
        if cfg.auto_approve:
            bits.append(f"auto-approve: {', '.join(cfg.auto_approve)}")
        if cfg.always_confirm:
            bits.append(f"always confirm: {', '.join(cfg.always_confirm)}")
        if cfg.allowed_roots:
            bits.append(f"writes limited to: {', '.join(cfg.allowed_roots)}")
        if not cfg.protect_system_paths:
            bits.append("system paths UNPROTECTED")
        return " | ".join(bits)
