"""CLI: ``python -m runbook_validator plan.json [--json]``.

Reads a plan document, validates it, and prints the report.  The tool only
validates the plan; it never executes any runbook step.
"""

import argparse
import json
import sys

from .model import Plan
from .validator import Validator


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="runbook_validator",
        description="Validate concurrent runbook plans (analysis only, "
                    "nothing is executed).")
    parser.add_argument("plan", help="path to a JSON plan document")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text")
    args = parser.parse_args(argv)

    with open(args.plan, "r", encoding="utf-8") as handle:
        plan = Plan.from_dict(json.load(handle))

    report = Validator(plan).validate()
    if args.json:
        print(report.to_json())
    else:
        print(report.render_text())
    return 0 if report.safe else 1


if __name__ == "__main__":
    sys.exit(main())
