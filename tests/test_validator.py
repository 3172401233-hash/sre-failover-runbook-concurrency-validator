import unittest

from runbook_validator.model import Plan, Runbook, Step
from runbook_validator.report import (
    DUPLICATE_EXECUTION,
    LOCK_BYPASS,
    MISSING_FENCING,
    ROLLBACK_OVERWRITE,
    STALE_PRECONDITION,
    WRITE_CONFLICT,
)
from runbook_validator.validator import Validator


def step(**kwargs):
    for key in ("reads", "writes", "locks"):
        if key in kwargs:
            kwargs[key] = set(kwargs[key])
    return Step(**kwargs)


def validate(runbooks, known_locks=()):
    plan = Plan(runbooks=list(runbooks), known_locks=set(known_locks))
    return Validator(plan).validate()


class ConcurrentFailoverTest(unittest.TestCase):
    """Two operators failing over the same primary at the same time."""

    def build_report(self):
        rb_a = Runbook(name="failover-a", operator="alice", steps=[
            step(id="check", kind="read", reads=["db-primary"]),
            step(id="promote", kind="destructive", writes=["db-primary"],
                 depends_on=["check"], fencing="epoch == current",
                 checkpoint=True),
        ])
        rb_b = Runbook(name="failover-b", operator="bob", steps=[
            step(id="check", kind="read", reads=["db-primary"]),
            step(id="promote", kind="destructive", writes=["db-primary"],
                 depends_on=["check"], fencing="epoch == current",
                 checkpoint=True),
        ])
        return validate([rb_a, rb_b])

    def test_write_conflict_detected(self):
        report = self.build_report()
        conflicts = report.hazards_of(WRITE_CONFLICT)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].resources, ["db-primary"])
        self.assertEqual(conflicts[0].steps,
                         ["failover-a/promote", "failover-b/promote"])
        self.assertFalse(report.safe)

    def test_readonly_checks_may_run_in_parallel(self):
        report = self.build_report()
        for hazard in report.hazards:
            self.assertNotIn("check", " ".join(hazard.steps))

    def test_pause_point_before_destructive_conflict(self):
        report = self.build_report()
        paused = {p.before_step for p in report.pause_points}
        self.assertIn("failover-a/promote", paused)
        self.assertIn("failover-b/promote", paused)

    def test_minimal_exclusion_scope_covers_shared_resource(self):
        report = self.build_report()
        scopes = [s.resources for s in report.exclusion_scopes]
        self.assertIn(["db-primary"], scopes)

    def test_serializable_order_separates_conflicting_writes(self):
        report = self.build_report()
        order = report.serializable_order
        self.assertEqual(len(order), 4)
        self.assertLess(order.index("failover-a/check"),
                        order.index("failover-a/promote"))
        self.assertLess(order.index("failover-b/check"),
                        order.index("failover-b/promote"))

    def test_shared_lock_makes_conflict_safe(self):
        rb_a = Runbook(name="a", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 locks=["db-primary"], fencing="f", checkpoint=True),
        ])
        rb_b = Runbook(name="b", operator="bob", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 locks=["db-primary"], fencing="f", checkpoint=True),
        ])
        report = validate([rb_a, rb_b])
        self.assertEqual(report.hazards_of(WRITE_CONFLICT), [])


class DuplicateRollbackTest(unittest.TestCase):
    """Rollback replay after restart and rollback overwriting commits."""

    def test_non_idempotent_rollback_without_checkpoint_is_flagged(self):
        rb = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 fencing="f", rollback="demote", checkpoint=True),
            step(id="demote", kind="write", writes=["db-primary"]),
        ])
        report = validate([rb])
        dups = report.hazards_of(DUPLICATE_EXECUTION)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].steps, ["failover/demote"])
        self.assertIn("rollback", dups[0].detail)

    def test_idempotent_checkpointed_rollback_is_safe(self):
        rb = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 fencing="f", rollback="demote", checkpoint=True),
            step(id="demote", kind="write", writes=["db-primary"],
                 idempotent=True, checkpoint=True),
        ])
        report = validate([rb])
        self.assertEqual(report.hazards_of(DUPLICATE_EXECUTION), [])

    def test_rollback_overwrites_concurrent_commit(self):
        rb_a = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 fencing="f", rollback="demote", checkpoint=True),
            step(id="demote", kind="write", writes=["db-primary"],
                 idempotent=True, checkpoint=True),
        ])
        rb_b = Runbook(name="traffic", operator="bob", steps=[
            step(id="repoint", kind="write", writes=["db-primary"],
                 checkpoint=True),
        ])
        report = validate([rb_a, rb_b])
        overwrites = report.hazards_of(ROLLBACK_OVERWRITE)
        self.assertEqual(len(overwrites), 1)
        self.assertEqual(overwrites[0].steps,
                         ["failover/demote", "traffic/repoint"])
        paused = {p.before_step for p in report.pause_points}
        self.assertIn("failover/demote", paused)

    def test_ordered_commit_is_not_overwritten(self):
        rb = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 fencing="f", rollback="demote", checkpoint=True),
            step(id="demote", kind="write", writes=["db-primary"],
                 idempotent=True, checkpoint=True),
            step(id="repoint", kind="write", writes=["db-primary"],
                 depends_on=["demote"], checkpoint=True),
        ])
        report = validate([rb])
        self.assertEqual(report.hazards_of(ROLLBACK_OVERWRITE), [])


class CheckpointRecoveryTest(unittest.TestCase):
    """Restart behaviour: checkpoints bound what may be re-executed."""

    def test_missing_checkpoint_flags_duplicate_execution(self):
        rb = Runbook(name="scale", operator="carol", steps=[
            step(id="add-replicas", kind="write", writes=["replica-pool"]),
        ])
        report = validate([rb])
        dups = report.hazards_of(DUPLICATE_EXECUTION)
        self.assertEqual([d.steps for d in dups], [["scale/add-replicas"]])

    def test_checkpoint_or_idempotency_suppresses_the_hazard(self):
        rb_ckpt = Runbook(name="scale", operator="carol", steps=[
            step(id="add-replicas", kind="write", writes=["replica-pool"],
                 checkpoint=True),
        ])
        rb_idem = Runbook(name="scale", operator="carol", steps=[
            step(id="add-replicas", kind="write", writes=["replica-pool"],
                 idempotent=True),
        ])
        for rb in (rb_ckpt, rb_idem):
            report = validate([rb])
            self.assertEqual(report.hazards_of(DUPLICATE_EXECUTION), [])

    def test_precondition_goes_stale_during_wait(self):
        rb_a = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 locks=["db-primary"], fencing="f", checkpoint=True,
                 preconditions={"db-primary": "reachable"}, timeout=30),
        ])
        rb_b = Runbook(name="traffic", operator="bob", steps=[
            step(id="repoint", kind="write", writes=["db-primary"],
                 locks=["db-primary"], checkpoint=True),
        ])
        report = validate([rb_a, rb_b])
        stale = report.hazards_of(STALE_PRECONDITION)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].steps[0], "failover/promote")
        paused = {p.before_step for p in report.pause_points}
        self.assertIn("failover/promote", paused)

    def test_no_wait_means_no_staleness(self):
        rb_a = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 locks=["db-primary"], fencing="f", checkpoint=True,
                 preconditions={"db-primary": "reachable"}),
        ])
        rb_b = Runbook(name="traffic", operator="bob", steps=[
            step(id="repoint", kind="write", writes=["db-primary"],
                 locks=["db-primary"], checkpoint=True),
        ])
        report = validate([rb_a, rb_b])
        self.assertEqual(report.hazards_of(STALE_PRECONDITION), [])


class LockBypassTest(unittest.TestCase):
    """Writes must claim the locks that protect their resources."""

    def test_bypass_detected(self):
        rb = Runbook(name="traffic", operator="bob", steps=[
            step(id="rewrite", kind="write", writes=["traffic-ingress"],
                 checkpoint=True),
        ])
        report = validate([rb], known_locks=["traffic-ingress"])
        bypass = report.hazards_of(LOCK_BYPASS)
        self.assertEqual(len(bypass), 1)
        self.assertEqual(bypass[0].resources, ["traffic-ingress"])

    def test_claimed_lock_is_not_a_bypass(self):
        rb = Runbook(name="traffic", operator="bob", steps=[
            step(id="rewrite", kind="write", writes=["traffic-ingress"],
                 locks=["traffic-ingress"], checkpoint=True),
        ])
        report = validate([rb], known_locks=["traffic-ingress"])
        self.assertEqual(report.hazards_of(LOCK_BYPASS), [])

    def test_unknown_locks_are_not_required(self):
        rb = Runbook(name="scale", operator="carol", steps=[
            step(id="add", kind="write", writes=["replica-pool"],
                 checkpoint=True),
        ])
        report = validate([rb], known_locks=["traffic-ingress"])
        self.assertEqual(report.hazards_of(LOCK_BYPASS), [])

    def test_destructive_step_requires_fencing(self):
        rb = Runbook(name="failover", operator="alice", steps=[
            step(id="promote", kind="destructive", writes=["db-primary"],
                 checkpoint=True),
        ])
        report = validate([rb])
        fencing = report.hazards_of(MISSING_FENCING)
        self.assertEqual(len(fencing), 1)
        self.assertEqual(fencing[0].steps, ["failover/promote"])


class PlanFromDictTest(unittest.TestCase):
    def test_json_round_trip_and_serializable_order(self):
        doc = {
            "known_locks": ["db-primary"],
            "runbooks": [
                {"name": "a", "operator": "alice", "steps": [
                    {"id": "s1", "kind": "read", "reads": ["db-primary"]},
                    {"id": "s2", "kind": "write", "writes": ["db-primary"],
                     "locks": ["db-primary"], "depends_on": ["s1"],
                     "checkpoint": True},
                ]},
                {"name": "b", "operator": "bob", "steps": [
                    {"id": "t1", "kind": "write", "writes": ["db-primary"],
                     "locks": ["db-primary"], "checkpoint": True},
                ]},
            ],
        }
        report = Validator(Plan.from_dict(doc)).validate()
        self.assertTrue(report.safe, report.render_text())
        order = report.serializable_order
        self.assertLess(order.index("a/s1"), order.index("a/s2"))
        import json
        json.loads(report.to_json())


if __name__ == "__main__":
    unittest.main()
