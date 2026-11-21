"""Validation report: hazards, pause points, exclusion scopes, order."""

import json
from dataclasses import dataclass, field
from typing import Dict, List


# Hazard kinds
WRITE_CONFLICT = "write_conflict"
ROLLBACK_OVERWRITE = "rollback_overwrite"
STALE_PRECONDITION = "stale_precondition"
DUPLICATE_EXECUTION = "duplicate_execution"
MISSING_FENCING = "missing_fencing"
LOCK_BYPASS = "lock_bypass"


@dataclass
class Hazard:
    kind: str
    steps: List[str]
    resources: List[str]
    detail: str

    def to_dict(self):
        return {
            "kind": self.kind,
            "steps": list(self.steps),
            "resources": sorted(self.resources),
            "detail": self.detail,
        }


@dataclass
class PausePoint:
    before_step: str
    reason: str

    def to_dict(self):
        return {"before_step": self.before_step, "reason": self.reason}


@dataclass
class ExclusionScope:
    """A minimal set of resources that must be mutated under one lock,
    plus the steps that force the grouping."""

    resources: List[str]
    steps: List[str]

    def to_dict(self):
        return {
            "resources": sorted(self.resources),
            "steps": sorted(self.steps),
        }


@dataclass
class ValidationReport:
    safe: bool = True
    hazards: List[Hazard] = field(default_factory=list)
    pause_points: List[PausePoint] = field(default_factory=list)
    exclusion_scopes: List[ExclusionScope] = field(default_factory=list)
    serializable_order: List[str] = field(default_factory=list)

    def add_hazard(self, hazard):
        self.hazards.append(hazard)
        self.safe = False

    def hazards_of(self, kind):
        return [h for h in self.hazards if h.kind == kind]

    def to_dict(self):
        return {
            "safe": self.safe,
            "hazards": [h.to_dict() for h in self.hazards],
            "pause_points": [p.to_dict() for p in self.pause_points],
            "exclusion_scopes": [s.to_dict() for s in self.exclusion_scopes],
            "serializable_order": list(self.serializable_order),
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def render_text(self):
        lines = []
        lines.append("safe: %s" % self.safe)
        if self.hazards:
            lines.append("hazards:")
            for h in self.hazards:
                lines.append("  - [%s] %s (resources: %s)" % (
                    h.kind, h.detail, ", ".join(sorted(h.resources))))
                for s in h.steps:
                    lines.append("      step: %s" % s)
        if self.pause_points:
            lines.append("pause points:")
            for p in self.pause_points:
                lines.append("  - before %s: %s" % (p.before_step, p.reason))
        if self.exclusion_scopes:
            lines.append("minimal exclusion scopes:")
            for s in self.exclusion_scopes:
                lines.append("  - resources {%s} locked by: %s" % (
                    ", ".join(sorted(s.resources)),
                    ", ".join(sorted(s.steps))))
        if self.serializable_order:
            lines.append("serializable order:")
            for i, label in enumerate(self.serializable_order, 1):
                lines.append("  %d. %s" % (i, label))
        return "\n".join(lines)
