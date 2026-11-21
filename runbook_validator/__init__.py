"""Runbook concurrency validator.

Statically validates a set of runbooks that on-call operators may execute
concurrently during an incident.  The validator never executes anything; it
only analyses the plan and reports unsafe interleavings, recommended pause
points and minimal mutual-exclusion scopes.
"""

from .model import Plan, Runbook, Step
from .validator import Validator
from .report import ValidationReport

__all__ = ["Plan", "Runbook", "Step", "Validator", "ValidationReport"]
__version__ = "0.1.0"
