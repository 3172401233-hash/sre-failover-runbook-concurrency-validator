"""Static concurrency validation for incident runbooks.

The validator treats every runbook as a DAG of steps and assumes steps from
*different* runbooks may interleave arbitrarily (different operators acting
at the same time).  It then checks, for every possible interleaving:

* two steps writing the same resource without a shared lock;
* a rollback action restoring state that a later, unordered commit changed;
* a precondition that can be invalidated while the step is waiting;
* a non-idempotent step that an operator restart could execute twice
  (no checkpoint recorded before it);
* destructive steps without a fencing condition;
* writes that bypass a known resource lock.

Read-only steps never conflict with each other and may run in parallel.
"""

from .model import DESTRUCTIVE
from .report import (
    DUPLICATE_EXECUTION,
    ExclusionScope,
    Hazard,
    LOCK_BYPASS,
    MISSING_FENCING,
    PausePoint,
    ROLLBACK_OVERWRITE,
    STALE_PRECONDITION,
    ValidationReport,
    WRITE_CONFLICT,
)


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


class Validator:
    def __init__(self, plan):
        self.plan = plan
        self.report = ValidationReport()
        self._steps = list(plan.all_steps())
        self._by_label = {s.label: s for s in self._steps}
        # step label -> set of ancestor labels inside its own runbook
        self._ancestors = {}
        for rb in plan.runbooks:
            self._compute_ancestors(rb)

    # ------------------------------------------------------------------
    def _compute_ancestors(self, runbook):
        memo = {}

        def visit(step, trail):
            if step.id in memo:
                return memo[step.id]
            if step.id in trail:
                raise ValueError(
                    "runbook %s: dependency cycle at step %s"
                    % (runbook.name, step.id))
            result = set()
            for dep_id in step.depends_on:
                dep = runbook.step(dep_id)
                result.add(dep.label)
                result |= visit(dep, trail | {step.id})
            memo[step.id] = result
            return result

        for step in runbook.steps:
            self._ancestors[step.label] = visit(step, set())

    def _happens_before(self, first, second):
        """True if `first` must complete before `second` starts."""
        if first.runbook != second.runbook:
            return False
        return first.label in self._ancestors.get(second.label, set())

    def validate(self):
        self._check_write_conflicts()
        self._check_rollback_overwrites()
        self._check_stale_preconditions()
        self._check_duplicate_execution()
        self._check_fencing()
        self._check_lock_bypass()
        self._derive_pause_points()
        self._derive_exclusion_scopes()
        self._derive_serializable_order()
        return self.report

    # ------------------------------------------------------------------
    def _concurrent_pairs(self):
        """Yield step pairs that may run at the same time (different
        runbooks/operators), each pair once, sorted for determinism."""
        steps = sorted(self._steps, key=lambda s: s.label)
        for i, a in enumerate(steps):
            for b in steps[i + 1:]:
                if a.runbook != b.runbook:
                    yield a, b

    def _check_write_conflicts(self):
        for a, b in self._concurrent_pairs():
            if a.is_readonly or b.is_readonly:
                continue
            shared = a.writes & b.writes
            if not shared:
                continue
            if (a.locks & b.locks) & shared:
                continue  # both hold a common lock on the shared resource
            self.report.add_hazard(Hazard(
                kind=WRITE_CONFLICT,
                steps=[a.label, b.label],
                resources=sorted(shared),
                detail=(
                    "%s (%s) and %s (%s) both write %s without a shared "
                    "lock; either interleaving can silently undo the other"
                    % (a.label, a.operator, b.label, b.operator,
                       ", ".join(sorted(shared)))),
            ))

    def _check_rollback_overwrites(self):
        for rb in self.plan.runbooks:
            for step in rb.steps:
                if not step.rollback:
                    continue
                rollback_step = rb.step(step.rollback)
                restored = step.writes
                if not restored:
                    continue
                for other in self._steps:
                    if other.label in (step.label, rollback_step.label):
                        continue
                    overlap = other.writes & restored
                    if not overlap:
                        continue
                    # Safe only when ordering guarantees `other` commits
                    # before the rollback runs, or strictly after it.
                    if self._happens_before(other, rollback_step):
                        continue
                    if self._happens_before(rollback_step, other):
                        continue
                    self.report.add_hazard(Hazard(
                        kind=ROLLBACK_OVERWRITE,
                        steps=[rollback_step.label, other.label],
                        resources=sorted(overlap),
                        detail=(
                            "rollback %s restores %s to the state before "
                            "%s, which can overwrite the commit of %s "
                            "(no ordering guarantees it lands first)"
                            % (rollback_step.label,
                               ", ".join(sorted(overlap)),
                               step.label, other.label)),
                    ))

    def _check_stale_preconditions(self):
        for step in self._steps:
            if not step.preconditions or step.timeout is None:
                continue
            watched = set(step.preconditions)
            for other in self._steps:
                if other.runbook == step.runbook:
                    continue
                overlap = other.writes & watched
                if not overlap:
                    continue
                self.report.add_hazard(Hazard(
                    kind=STALE_PRECONDITION,
                    steps=[step.label, other.label],
                    resources=sorted(overlap),
                    detail=(
                        "%s checks precondition on %s but may wait up to "
                        "%ss; %s can invalidate it during the wait -- "
                        "re-check after waiting, before committing"
                        % (step.label, ", ".join(sorted(overlap)),
                           step.timeout, other.label)),
                ))

    def _check_duplicate_execution(self):
        for rb in self.plan.runbooks:
            for step in rb.steps:
                # Steps are listed in runbook order; a restart resumes at
                # the last checkpoint, so anything non-idempotent after the
                # most recent checkpoint may be re-executed.
                if step.checkpoint:
                    continue
                if step.is_readonly or step.idempotent:
                    continue
                self.report.add_hazard(Hazard(
                    kind=DUPLICATE_EXECUTION,
                    steps=[step.label],
                    resources=sorted(step.writes),
                    detail=(
                        "%s is not idempotent and is not protected by a "
                        "checkpoint; an operator restart re-executes it "
                        "(duplicate %s)"
                        % (step.label,
                           "rollback" if self._is_rollback_step(step)
                           else "write")),
                ))

    def _is_rollback_step(self, step):
        return any(
            other.runbook == step.runbook and other.rollback == step.id
            for other in self._steps)

    def _check_fencing(self):
        for step in self._steps:
            if step.kind == DESTRUCTIVE and not step.fencing:
                self.report.add_hazard(Hazard(
                    kind=MISSING_FENCING,
                    steps=[step.label],
                    resources=sorted(step.writes),
                    detail=(
                        "destructive step %s has no fencing condition; a "
                        "stale or restarted operator could apply it against "
                        "an expired lease" % step.label),
                ))

    def _check_lock_bypass(self):
        for step in self._steps:
            uncovered = (step.writes & self.plan.known_locks) - step.locks
            if uncovered:
                self.report.add_hazard(Hazard(
                    kind=LOCK_BYPASS,
                    steps=[step.label],
                    resources=sorted(uncovered),
                    detail=(
                        "%s writes %s, which %s protected by a known lock "
                        "the step never claims"
                        % (step.label, ", ".join(sorted(uncovered)),
                           "is" if len(uncovered) == 1 else "are")),
                ))

    # ------------------------------------------------------------------
    def _derive_pause_points(self):
        seen = set()

        def add(label, reason):
            if label not in seen:
                seen.add(label)
                self.report.pause_points.append(
                    PausePoint(before_step=label, reason=reason))

        for hazard in self.report.hazards:
            if hazard.kind == WRITE_CONFLICT:
                for label in hazard.steps:
                    step = self._by_label[label]
                    if step.is_destructive:
                        add(label, "destructive step involved in a write "
                                   "conflict; pause until the conflicting "
                                   "runbook releases the resource")
            elif hazard.kind == STALE_PRECONDITION:
                add(hazard.steps[0],
                    "re-validate preconditions here; they may have been "
                    "invalidated while waiting")
            elif hazard.kind == ROLLBACK_OVERWRITE:
                add(hazard.steps[0],
                    "confirm no concurrent commit touched the restored "
                    "resources before rolling back")

    def _derive_exclusion_scopes(self):
        uf = _UnionFind()
        members = {}

        def note(resource, label):
            uf.find(resource)
            members.setdefault(resource, set()).add(label)

        conflict_pairs = [h for h in self.report.hazards
                          if h.kind == WRITE_CONFLICT]
        for hazard in conflict_pairs:
            res = sorted(hazard.resources)
            for r in res:
                for label in hazard.steps:
                    note(r, label)
            for r in res[1:]:
                uf.union(res[0], r)
        # Also union resources co-written by a single conflicting step: they
        # must be locked together to make the critical section atomic.
        involved = {label for h in conflict_pairs for label in h.steps}
        for label in sorted(involved):
            writes = sorted(self._by_label[label].writes)
            for r in writes:
                note(r, label)
            for r in writes[1:]:
                uf.union(writes[0], r)

        groups = {}
        for resource, labels in members.items():
            root = uf.find(resource)
            groups.setdefault(root, {"resources": set(), "steps": set()})
            groups[root]["resources"].add(resource)
            groups[root]["steps"] |= labels
        for group in groups.values():
            self.report.exclusion_scopes.append(ExclusionScope(
                resources=sorted(group["resources"]),
                steps=sorted(group["steps"]),
            ))
        self.report.exclusion_scopes.sort(key=lambda s: s.resources)

    def _derive_serializable_order(self):
        # Nodes: every step.  Edges: runbook dependencies plus, for every
        # conflicting cross-runbook pair, a serialization edge in
        # deterministic (runbook, step) order.  Read-only steps stay
        # unordered so they can run in parallel.
        labels = sorted(self._by_label)
        edges = {label: set() for label in labels}
        for step in self._steps:
            for dep_id in step.depends_on:
                dep = self.plan.find_step(step.runbook, dep_id)
                edges[step.label].add(dep.label)
            if step.rollback:
                undo = self.plan.find_step(step.runbook, step.rollback)
                edges[undo.label].add(step.label)
        conflict_pairs = [h for h in self.report.hazards
                          if h.kind in (WRITE_CONFLICT, ROLLBACK_OVERWRITE)]
        for hazard in conflict_pairs:
            a, b = sorted(hazard.steps)
            edges[b].add(a)

        order = []
        remaining = dict(edges)
        while remaining:
            ready = sorted(l for l, deps in remaining.items() if not deps)
            if not ready:
                # Cycle: hazards made the plan non-serializable.
                self.report.serializable_order = []
                return
            for label in ready:
                order.append(label)
                del remaining[label]
                for deps in remaining.values():
                    deps.discard(label)
        self.report.serializable_order = order
