#!/usr/bin/env python3
"""Merge-queue reservation, for the shell side of the merge path.

A pin-moving PR (one touching lake-manifest.json / lean-toolchain) invalidates the build cache, so
its merge-group build rebuilds everything and takes 83 to 95 minutes. main merges a PR every few
minutes, and the queue tests "current main plus this PR", so anything landing under the bump that the
new mathlib deprecates fails its build and evicts it. The bump therefore gets the queue to itself.

The reservation is DERIVED, never stored: "is a pin-moving PR in the queue?" is answered from the
queue each time, so nothing can be left set by a crashed job and release needs no code — the query
goes false when the bump merges or is evicted. Policy lives in sweep.py; this is only the CLI that
lets .github/workflows/merge-only.yml consult the same code instead of a second copy in jq.

  holder            print the reserving PR number (nothing if unreserved); exit 0
  clear --for N     dequeue every entry other than N, so N rebuilds alone; exit 0 if the queue is
                    clear afterwards

Exit 2 means the queue could not be READ, which is distinct from "no holder" on purpose: the caller
must fail closed rather than enqueue into a queue whose state it does not know.

Env: GH_TOKEN, REPO (owner/name), optional DRY_RUN=1, MAX_HOLD_HOURS, RESERVATION_ACTOR.
"""
import argparse
import datetime
import sys

import sweep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("holder", help="print the PR holding the merge queue, if any")
    clear = sub.add_parser("clear", help="dequeue everything except the given PR")
    clear.add_argument("--for", dest="holder", type=int, required=True)
    args = ap.parse_args(argv)

    if not sweep.REPO:
        print("queue-reservation: REPO env is required", file=sys.stderr)
        return 2
    try:
        entries = sweep.queue_entries()
    except RuntimeError as e:
        print(f"queue-reservation: cannot read the merge queue ({e})", file=sys.stderr)
        return 2

    if args.cmd == "holder":
        now = datetime.datetime.now(datetime.timezone.utc)
        holder = sweep.reservation_holder(entries, now)
        if holder is not None:
            print(holder)
        return 0

    # clear: the holder is enqueued only after the queue is empty, never before. An entry already
    # ahead of it can merge in the window between the two, and no later dequeue undoes a commit.
    failures = sweep.reconcile_reservation(entries, args.holder)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
