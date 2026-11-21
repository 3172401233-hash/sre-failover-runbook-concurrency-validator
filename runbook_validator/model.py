"""Data model for runbooks, steps and execution plans.

Everything is plain dataclasses so plans can be built either from Python or
from JSON/YAML documents via :meth:`Plan.from_dict`.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# Action kinds
READ = "read"            # read-only check, safe to run in parallel
WRITE = "write"          # mutates state but is reversible / non-destructive
DESTRUCTIVE = "destructive"  # irreversible unless a rollback action exists

ACTION_KINDS = (READ, WRITE, DESTRUCTIVE)


@dataclass
class Step:
    """A single runbook step.

    Attributes:
        id: unique step id inside its runbook.
        runbook: name of the owning runbook (filled in by Runbook).
        operator: the on-call operator/role expected to run the step.
        kind: one of read / write / destructive.
        reads: resources whose state the step inspects (preconditions too).
        writes: resources the step mutates.
        depends_on: ids of steps in the *same* runbook that must finish first.
        preconditions: predicates ``resource == expected`` checked at start.
        locks: resource locks the step claims to hold while running.
        fencing: fencing condition (e.g. ``"epoch == current_epoch"``) that
            must be re-validated at commit time.  Required for destructive
            steps so a stale/restarted operator cannot act on old leases.
        rollback: id of the step that undoes this one, if any.
        checkpoint: if True, completion of this step is recorded so an
            operator restart resumes *after* it instead of re-running it.
        idempotent: if True, re-executing the step is harmless.
        timeout: seconds the step may wait (for locks, drain, ...) before
            its preconditions are considered stale.
    """

    id: str
    runbook: str = ""
    operator: str = ""
    kind: str = WRITE
    reads: Set[str] = field(default_factory=set)
    writes: Set[str] = field(default_factory=set)
    depends_on: List[str] = field(default_factory=list)
    preconditions: Dict[str, str] = field(default_factory=dict)
    locks: Set[str] = field(default_factory=set)
    fencing: Optional[str] = None
    rollback: Optional[str] = None
    checkpoint: bool = False
    idempotent: bool = False
    timeout: Optional[float] = None

    @property
    def is_readonly(self):
        return self.kind == READ and not self.writes

    @property
    def is_destructive(self):
        return self.kind == DESTRUCTIVE

    @property
    def label(self):
        return "%s/%s" % (self.runbook, self.id)

    @staticmethod
    def from_dict(data):
        data = dict(data)
        for key in ("reads", "writes", "locks"):
            if key in data:
                data[key] = set(data[key])
        if "depends_on" in data:
            data["depends_on"] = list(data["depends_on"])
        if "preconditions" in data:
            data["preconditions"] = dict(data["preconditions"])
        step = Step(**data)
        if step.kind not in ACTION_KINDS:
            raise ValueError("step %s: unknown kind %r" % (step.id, step.kind))
        return step


@dataclass
class Runbook:
    """An ordered DAG of steps owned by one operator role."""

    name: str
    operator: str = ""
    steps: List[Step] = field(default_factory=list)

    def __post_init__(self):
        seen = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(
                    "runbook %s: duplicate step id %r" % (self.name, step.id))
            seen.add(step.id)
            step.runbook = self.name
            if not step.operator:
                step.operator = self.operator
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in seen:
                    raise ValueError(
                        "runbook %s: step %s depends on unknown step %r"
                        % (self.name, step.id, dep))
            if step.rollback and step.rollback not in seen:
                raise ValueError(
                    "runbook %s: step %s rolls back unknown step %r"
                    % (self.name, step.rollback, step.id))

    def step(self, step_id):
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    @staticmethod
    def from_dict(data):
        data = dict(data)
        data["steps"] = [Step.from_dict(s) for s in data.get("steps", [])]
        return Runbook(**data)


@dataclass
class Plan:
    """The full incident plan: several runbooks executed concurrently."""

    runbooks: List[Runbook] = field(default_factory=list)
    # Locks that exist in the environment.  A step "bypasses" the lock
    # regime when it writes a resource covered by a known lock without
    # claiming that lock.
    known_locks: Set[str] = field(default_factory=set)

    def all_steps(self):
        for runbook in self.runbooks:
            for step in runbook.steps:
                yield step

    def find_step(self, runbook, step_id):
        for rb in self.runbooks:
            if rb.name == runbook:
                return rb.step(step_id)
        raise KeyError("%s/%s" % (runbook, step_id))

    @staticmethod
    def from_dict(data):
        return Plan(
            runbooks=[Runbook.from_dict(r) for r in data.get("runbooks", [])],
            known_locks=set(data.get("known_locks", [])),
        )
