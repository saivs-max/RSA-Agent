#!/usr/bin/env python3
"""
rsa_normalize.py — enforce the standing RSA Tracker rules on a schedule:
  • Overall Status → 'Passed'  where RSA Status == 'Completed RSA'
  • DRI            → 'HW Ops'  where RSA Status != 'Completed RSA'
  • DRI            → 'CSM'     where RSA Status == 'Completed RSA'
  • RSA Status     → the LATEST Truno call per cart (via truno_failures): open failures
    become 'Failed RSA', carts that later passed become 'Completed/Passed', ambiguous
    Truno rows are highlighted for review. Update-only — never adds a duplicate row.

Runs sheets.normalize_tracker() periodically (started by rsa_agent). Only rows that
actually change are written, so once the tracker converges each pass is a silent no-op.
Named individuals in the DRI column are never overwritten.

Slack notifications are capped to ONE per day — the first pass that finds changed rows
posts a summary; subsequent passes that day (including those triggered by restarts) are
silent. The date is saved to RSA_NORMALIZE_STATE_FILE (default: rsa_normalize_state.json).

Also runnable standalone:

    python3 rsa_normalize.py            # one pass now, print the counts
"""
import os
import json
import time
import logging
import threading
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import rsa_sheets as sheets

log = logging.getLogger("rsa-agent.normalize")
POLL_SECONDS = int(os.environ.get("RSA_NORMALIZE_POLL_SECONDS", "900"))    # every 15 min
STATE_FILE   = os.environ.get("RSA_NORMALIZE_STATE_FILE", "rsa_normalize_state.json")


def _last_notified_date():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("lastNotified")
    except Exception:
        return None


def _mark_notified(date_str):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"lastNotified": date_str}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("could not save %s: %s", STATE_FILE, e)


def run_once(notify=None):
    """One normalize pass. Fixes rows and logs the summary — never posts to Slack
    (normalize is internal housekeeping; the log is the right place for it)."""
    res = sheets.normalize_tracker()
    if res.get("error"):
        log.warning("normalize_tracker error: %s", res["error"])
        return res
    if res.get("changedRows"):
        parts = [f"RSA→Completed: {res.get('rsaFixed', 0)}",
                 f"Overall→Passed: {res['overallPassed']}",
                 f"DRI→CSM: {res['driCsm']}",
                 f"DRI→HW Ops: {res.get('driHwops', 0) + res.get('driReverted', 0)}"]
        log.info("Tracker normalized — %s (%s row(s)).",
                 ", ".join(p for p in parts if not p.endswith(": 0")),
                 res["changedRows"])

    # Normalize RSA Status to the LATEST Truno call (open failures → Failed RSA, carts that
    # later passed → Completed/Passed), and highlight ambiguous Truno rows. Update-only, so
    # it never adds a duplicate row. Part of the standing normalization step.
    try:
        import truno_failures
        tf = truno_failures.run(commit=True, highlight=True, notify=None)
        if isinstance(tf, dict) and not tf.get("error"):
            nf, npass, amb = len(tf.get("fail", [])), len(tf.get("pass", [])), len(tf.get("ambiguous", []))
            res["trunoFailed"], res["trunoPassed"], res["trunoFlagged"] = nf, npass, amb
            if nf or npass or amb:
                log.info("Truno→RSA normalize — Failed RSA: %d, Passed: %d, flagged for review: %d", nf, npass, amb)
        elif isinstance(tf, dict) and tf.get("error"):
            log.warning("Truno→RSA normalize skipped: %s", tf["error"])
    except Exception as e:
        log.warning("Truno→RSA normalize error: %s", e)
    return res


def start_poller(notify=None, interval=POLL_SECONDS):
    """Enforce the tracker rules every `interval` seconds in a daemon thread.
    Results are logged only — no Slack posts, so restarts don't clutter the channel."""
    def loop():
        log.info("Tracker normalizer running every %ss (DRI: HW Ops→CSM→HW Ops routing)", interval)
        while True:
            try:
                run_once()          # notify intentionally not passed — log only
            except Exception as e:
                log.error("normalize loop error: %s", e)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="rsa-normalize", daemon=True)
    t.start()
    return t


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("rsa_normalize:", run_once())


if __name__ == "__main__":
    main()
