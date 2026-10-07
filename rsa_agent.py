#!/usr/bin/env python3
"""
RSA Operations Agent — Slack bot (natural language, via Instacart AI Gateway).

Ops managers send a plain-English Slack message; the agent:
  1. Parses intent with an LLM through the Instacart AI Gateway (OpenAI-compatible)
  2. Reads/writes the RSA Tracker, TRUNO tracker, and Compliance Guide directly via
     the Google Sheets API (service account — see rsa_sheets.py)
  3. Posts a confirmation (onboarding also surfaces the W&M/RSA compliance directive)

Handles: new-cart onboarding, status updates, queries, filtered lists, summaries,
and compliance lookups. Socket Mode — DM the bot or @mention it.
"""

import os
import re
import json
import logging
import time

import requests
from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError   # APITimeoutError subclasses APIConnectionError
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import rsa_sheets as sheets
import store_map
import test_forms
import tech_forms
import backfill_dates

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rsa-agent")

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN      = os.environ["SLACK_APP_TOKEN"]
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
NICOLE_SLACK_ID      = os.environ.get("NICOLE_SLACK_ID", "")
CSM_MENTION          = os.environ.get("CSM_MENTION", "")   # e.g. <@U123> / <!subteam^S123> — tagged when a cart passes RSA

# AI Gateway (OpenAI-compatible). Auth is by network/identity, so the API key is a
# placeholder unless your gateway needs a token (then set AIGATEWAY_API_KEY).
AIGATEWAY_BASE_URL = os.environ.get(
    "AIGATEWAY_BASE_URL", "https://aigateway.instacart.tools/proxy/rovi_agent/openai/v1")
AIGATEWAY_API_KEY  = os.environ.get("AIGATEWAY_API_KEY", "gateway-no-token-needed")
AGENT_MODEL        = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
# Fail fast and retry transient gateway/network blips (the SDK retries connection
# errors, 408/409/429 and 5xx with exponential backoff). Defaults: 30s/3 tries — far
# better than the SDK's 600s no-timeout default, which can hang a Slack reply.
AIGATEWAY_TIMEOUT     = float(os.environ.get("AIGATEWAY_TIMEOUT", "30"))
AIGATEWAY_MAX_RETRIES = int(os.environ.get("AIGATEWAY_MAX_RETRIES", "3"))

llm = OpenAI(base_url=AIGATEWAY_BASE_URL, api_key=AIGATEWAY_API_KEY,
             timeout=AIGATEWAY_TIMEOUT, max_retries=AIGATEWAY_MAX_RETRIES)

slack = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)


# ═══════════════════════════════════════════════════════════════════════════════
# INTENT PARSER (LLM via the gateway)
# ═══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are the RSA Operations assistant for Instacart Hardware Ops.
You help ops managers interact with the RSA cart tracker and automate workflows.

ACTIONS available:
  • onboardCart    — Full workflow: add to RSA Tracker + schedule TRUNO + (auto) email Nicole.
                     Use when an ops manager submits NEW cart(s) that need inspection scheduled.
                     Signals: "new cart", "add cart", "onboard", "launch", "schedule inspection".
                     IMPORTANT: users give cart NUMBER(S) only — e.g. 1, 7, 16, 3A — never full
                     serial IDs. Put every number in "cartNumbers" (array of strings). They often
                     onboard several carts at one store at once.
                     MULTIPLE STORES: if the message lists more than one store (e.g.
                     "Sprouts 15 - carts 3,4,5 / Sprouts 20 - carts 5,6,9"), return ONE entry per
                     store in the "stores" array — each with its own "store", "state", "cartNumbers"
                     (and "address" if given) — and leave the top-level "store"/"cartNumbers" null.
                     For a single store, just use the top-level "store" and "cartNumbers" (leave
                     "stores" null). The service trigger and RSA vendor apply to ALL stores in the
                     request. Capture any street address in "address". The service "trigger" is REQUIRED — only set it if
                     the user actually states it; otherwise leave trigger:null and the bot will ask.
                     VENDOR: only set "rsaVendor" if the user explicitly names one (TRUNO / Winter
                     Scales / DUMAC). For NJ stores the bot must NOT assume TRUNO — leave
                     rsaVendor:null when unstated and the bot will ask TRUNO vs Winter Scales.
                     Normalize phrasing: "winter scales"/"winterscale"/"winterscales" → "Winter Scales".
  • updateCart     — Update specific fields on an existing cart (status changes, notes, DRI).
  • queryCart      — Look up a specific cart's current status.
  • getSummary     — Overall dashboard counts and breakdown by state.
  • listCarts      — Filtered list (by state, status, DRI, store, stale carts).
  • complianceCheck— Look up the RSA/W&M requirements for a store/state from the compliance guide.
                     Signals: "what's required", "compliance", "do we need RSA or W&M", "rules for".
  • upcomingVisits — Scheduled RSA/Truno visit dates in the next N days, filterable by store
                     name and/or cart number. Designed for CSMs checking when a specific store
                     or cart is getting serviced.
                     Signals: "when is cart 3 at Sprouts being visited", "upcoming visits for
                     ShopRite 116", "what's scheduled for cart 7", "visit dates for Sprouts NJ",
                     "when is my store getting inspected", "next visit for cart 12",
                     "schedule for Whole Foods 15", "what visits are coming up".
                     Set "store" if a store name is mentioned, "cartNumbers" (first element) if
                     a specific cart number is mentioned, "daysAhead" (default 60) if a time
                     window is specified.
  • scheduledNoTruno — List of carts with RSA Status = "Scheduled RSA" that have no matching
                     row in the Truno tracker. Used to identify carts that appear scheduled but
                     were never actually sent to Truno.
                     Signals: "carts not on Truno", "scheduled with no Truno row", "missing from
                     Truno", "which scheduled carts aren't in Truno", "Truno gaps".
  • missingResults — Carts that don't yet have a tech (calibration) and/or W&M test result.
                     Signals: "missing results", "incomplete carts", "which carts need tech results",
                     "carts without calibration", "what's missing tech/test". Set "resultsKind" to
                     "tech", "test", "both" (missing BOTH), or "any" (missing either; the default).
  • monthlySummary — Visits completed and scheduled for a given month, drawn from the RSA Tracker
                     (completion dates + scheduled RSA dates) and the TRUNO tracker (visit dates).
                     Signals: "how many visits did we do in June", "monthly summary", "visits this month",
                     "what's scheduled for July", "completed in May", "monthly report".
                     Set "month" (1-12) and "year" (4-digit) — both default to current month/year.
  • backfillDates  — Scan the RSA Tracker for completed/failed carts that are missing the RSA Scheduled
                     Date or RSA Completion Date, look up the TRUNO visit date, and write the missing
                     values back. Safe to run multiple times (idempotent; skips rows that already have dates).
                     Signals: "backfill dates", "fill in missing dates", "carts missing scheduled date",
                     "carts missing completion date", "populate missing RSA dates", "fix missing dates".
                     Set "dryRun": true to preview without writing.
  • fixDri         — Force-correct every DRI value in the tracker right now:
                       • RSA Status == "Completed RSA"  →  DRI = CSM
                       • RSA Status != "Completed RSA"  →  DRI = HW Ops
                     (The normalizer does this every 5 min automatically; this runs it immediately.)
                     Signals: "fix DRI", "normalize DRI", "update DRI", "fix tracker DRI",
                     "set HW Ops for pending carts", "repair DRI", "DRI is wrong", "DRI out of sync",
                     "update all DRI to HW Ops", "correct the DRI column", "review RSA status DRI".
                     Set "dryRun": true to preview counts without writing.
  • unknown        — Request needs clarification.

CSM pending queries: "pending with CSM", "how many CSM visits", "CSM handoffs this month" →
  use listCarts with dri="CSM" and sinceDays=30. The sinceDays filter keeps only carts whose
  RSA Completion Date falls within the last N days (carts with no completion date are excluded).

RSA Tracker field reference:
  cartId, store, state (2-letter), trigger (Launches/Cart replacement/Load cell recal/W&M Failed/
  W&M Inspection/Scale Drift/Bent Frame/Top unit replacement/Jetson Replacement/HW Ops Inspection)
  rsaStatus (Pending Truno Scheduled/Scheduled RSA/Completed RSA/Failed RSA/Pending RSA)
  rsaVendor (Winter Scales/TRUNO/DUMAC/TBD)
  rsaSchedDate (MM/DD/YYYY)
  wmStatus (Pending W&M/Scheduled W&M/Passed W&M/Failed W&M/Completed W&M)
  wmDate (MM/DD/YYYY)
  overallStatus (Pending RSA/Pending W&M/Passed/Failed/Redispatch Needed/Needs Verification)
  dri (Sai VS/Vishnu Kariyattu/Maitland Kelly/Andrew Cha/Reshmi Chowdhury/HW Ops/Ops Manager/CSM/
  Winter Scales/TRUNO/DUMAC/TBD)
  notes, visitDate (for TRUNO scheduling), submittedBy (name of the submitter)

Return ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "action": "onboardCart"|"updateCart"|"queryCart"|"getSummary"|"listCarts"|"upcomingVisits"|"scheduledNoTruno"|"complianceCheck"|"missingResults"|"monthlySummary"|"backfillDates"|"fixDri"|"unknown",
  "cartId": string|null, "cartNumbers": string[]|null, "store": string|null, "storeHint": string|null,
  "stores": {"store": string, "state": string|null, "cartNumbers": string[], "address": string|null}[]|null,
  "state": string|null, "address": string|null,
  "trigger": string|null, "rsaStatus": string|null, "rsaVendor": string|null, "rsaSchedDate": string|null,
  "wmStatus": string|null, "wmDate": string|null, "overallStatus": string|null, "dri": string|null,
  "notes": string|null, "appendNotes": boolean, "visitDate": string|null, "submittedBy": string|null,
  "staleOnly": boolean, "storeFragment": string|null, "limit": number|null,
  "resultsKind": "tech"|"test"|"both"|"any"|null,
  "month": number|null, "year": number|null,
  "sinceDays": number|null,
  "daysAhead": number|null,
  "dryRun": boolean|null,
  "clarification": string|null
}"""


def _extract_json(text: str):
    """Best-effort parse of an LLM reply into JSON. Returns a dict/list, or None if
    nothing parseable is found. Tolerates code fences, leading/trailing prose, and a
    JSON object/array embedded in surrounding text — so a chatty or partly-malformed
    model reply never crashes the handler."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```json\s*", "", t, flags=re.I)
    t = re.sub(r"^```\s*", "", t)
    t = re.sub(r"```$", "", t).strip()
    try:                                            # fast path: the whole thing is JSON
        return json.loads(t)
    except Exception:
        pass
    # Fallback: scan for the first balanced {...} or [...] span and parse that.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = t.find(open_ch)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break
    return None


def parse_intent(user_message: str, user_name: str) -> dict:
    """Parse an ops-manager message into a structured intent. NEVER raises: any
    backend error, empty reply, or non-JSON output degrades to an 'unknown' intent
    with a clarification so the Slack handler can ask the user rather than crash."""
    try:
        resp = llm.chat.completions.create(
            model=AGENT_MODEL,
            max_tokens=1000,                       # headroom for multi-store payloads
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Submitted by: {user_name or 'ops manager'}\nMessage: {user_message}"},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
    except APIConnectionError as e:
        # Network/gateway blip (incl. timeouts) — already retried by the SDK. Log concisely
        # (no stack-trace spam) and tell the user plainly so they just retry.
        log.warning("LLM gateway unreachable (%s) after retries", type(e).__name__)
        return {"action": "unknown",
                "clarification": ("I couldn't reach the assistant backend just now "
                                  "(network/gateway). Please try again in a moment.")}
    except Exception as e:
        log.exception("LLM intent call failed")
        return {"action": "unknown",
                "clarification": f"the assistant backend hit an error ({type(e).__name__}). Please try again."}

    parsed = _extract_json(text)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        # Model returned a bare list — treat as multi-store onboarding if it looks like
        # store groups; otherwise fall through to a clarification.
        groups = [g for g in parsed if isinstance(g, dict)
                  and (g.get("cartNumbers") or g.get("carts") or g.get("store"))]
        if groups:
            return {"action": "onboardCart", "stores": groups}

    snippet = " ".join((text or "").split())
    if len(snippet) > 200:
        snippet = snippet[:200] + "…"
    return {"action": "unknown",
            "clarification": snippet or ("I couldn't read that. Try e.g. "
                                         "`onboard carts 3,4,5 at Sprouts 15 NJ, trigger Launches`.")}


# ═══════════════════════════════════════════════════════════════════════════════
# RESPONSE FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════
def _btn(text, url, style=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "url": url}
    if style:
        b["style"] = style
    return b


def _actions(*buttons):
    els = [b for b in buttons if b and b.get("url")]
    return [{"type": "actions", "elements": els}] if els else []


# Service triggers (the dashboard dropdown) — onboarding asks for one if not given.
TRIGGER_OPTIONS = ["Launches", "Cart replacement", "Load cell recal", "W&M Failed",
                   "W&M Inspection", "Scale Drift", "Bent Frame", "Top unit replacement",
                   "Jetson Replacement", "HW Ops Inspection"]

# RSA vendors offered for NJ stores. NJ does NOT default to TRUNO — the agent asks.
VENDOR_OPTIONS = ["TRUNO", "Winter Scales"]


def format_trigger_prompt(store, state, carts, address=None, notes=None):
    """Ask for the mandatory service trigger, presenting the dashboard options as
    buttons. Each button carries the full onboard context in its value, so the click
    can complete the add with no server-side state. Typing the trigger also works."""
    cart_str = ", ".join(str(c) for c in (carts or []))
    ctx = {"store": store, "state": state, "carts": [str(c) for c in (carts or [])],
           "address": address, "notes": notes}
    btns = [{"type": "button", "text": {"type": "plain_text", "text": t},
             "action_id": f"rsa_set_trigger_{i}",
             "value": json.dumps({**ctx, "trigger": t})}
            for i, t in enumerate(TRIGGER_OPTIONS)]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*Before I add cart(s) {cart_str} at _{store}_ ({state})* — what's the *service trigger*? "
        f"_(required for the tracker)_\nPick one below, or just type it, e.g. "
        f"`onboard carts {cart_str} at {store} {state}, trigger Cart replacement`.")}}]
    for i in range(0, len(btns), 5):                      # ≤5 buttons per actions block
        blocks.append({"type": "actions", "elements": btns[i:i + 5]})
    return {"text": f"What's the service trigger for cart(s) {cart_str} at {store}?", "blocks": blocks}


def format_vendor_prompt(store, state, carts, trigger, address=None, notes=None, cartId=None):
    """NJ rule: never silently assign TRUNO. Ask the ops manager to pick the RSA
    vendor. Each button carries the full onboard context in its value, so the click
    completes the add with no server-side state. Typing the vendor also works."""
    cart_str = ", ".join(str(c) for c in (carts or [])) or (cartId or "")
    ctx = {"store": store, "state": state, "carts": [str(c) for c in (carts or [])],
           "cartId": cartId, "trigger": trigger, "address": address, "notes": notes}
    btns = []
    for i, v in enumerate(VENDOR_OPTIONS):
        b = {"type": "button", "text": {"type": "plain_text", "text": v},
             "action_id": f"rsa_set_vendor_{i}", "value": json.dumps({**ctx, "vendor": v})}
        if v == "TRUNO":
            b["style"] = "primary"
        btns.append(b)
    ws_emails = ", ".join(sheets.WINTERSCALES_EMAILS) or "the Winter Scales team"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*{store} is in NJ* — who should run the RSA for cart(s) {cart_str}?\n"
        f"• *TRUNO* — I add the cart(s) and schedule TRUNO (Nicole emailed automatically), the usual flow.\n"
        f"• *Winter Scales* — I add the cart(s) as *Pending RSA* and email {ws_emails} to request scheduling; "
        f"you'll then need to follow up with Winter Scales directly.\n"
        f"_Pick one below, or just type it — e.g._ `... vendor Winter Scales`.")}}]
    blocks.append({"type": "actions", "elements": btns})
    return {"text": f"NJ store — TRUNO or Winter Scales for cart(s) {cart_str} at {store}?", "blocks": blocks}


def _groups_summary(groups):
    """One-line recap like 'Sprouts 15 (VA): 3, 4, 5  ·  Sprouts 20 (MD): 5, 6'."""
    parts = []
    for g in groups:
        nums = ", ".join(str(c) for c in (g.get("carts") or []))
        st = f" ({g.get('state')})" if g.get("state") else ""
        parts.append(f"_{g.get('store') or '?'}_{st}: {nums}")
    return "  ·  ".join(parts)


def _clean_groups(groups):
    """Minimal, JSON-safe store groups for embedding in a button value (drops any
    private _keys so the payload stays small and under Slack's value limit).
    'storeKey' (the unique Deployed-Stores id, when a store has been resolved) rides
    along so the validated selection survives a button round-trip."""
    return [{"store": g.get("store"), "storeKey": g.get("storeKey"), "state": g.get("state"),
             "carts": g.get("carts") or [], "address": g.get("address"),
             "notes": g.get("notes")} for g in (groups or [])]


def _cand_label(c):
    """Button/label text for a store candidate: friendly name + city/state so two
    same-named stores (e.g. the two 'Roosevelt's) are distinguishable. ≤75 chars."""
    name = c.get("display") or c.get("store") or "?"
    sub = ", ".join(p for p in (c.get("city"), c.get("state")) if p)
    txt = f"{name} ({sub})" if sub else name
    return txt[:75]


def _store_prompt_text(typed, m):
    """The natural-language ask for an unresolved store, branching on match status:
      ambiguous → a real name that hits several stores (a retailer, or a dup name)
      suggest   → no exact match, but close names exist
      none      → nothing close — list how to identify a store."""
    status, count = m.get("status"), m.get("count", 0)
    if status == "ambiguous":
        head = f'*"{typed}" matches {count} stores* — which one?'
        if count > len(m.get("candidates") or []):
            head += (f"\n_Showing {len(m['candidates'])} of {count}. If it's not here, type the "
                     f"*nickname* or *external store ID* to narrow it down._")
        return head
    if status == "suggest":
        return (f'*I don\'t have a store called "{typed}".* Did you mean one of these?\n'
                f"_Or type the exact *store name*, *nickname*, *external store ID*, or *retailer*._")
    return (f'*"{typed}" isn\'t in the deployed stores list.*\n'
            f"Pick a store by its *store name*, *nickname*, *external store ID*, or *retailer* — "
            f"e.g. `Price Chopper 200`, `200`, or `prod-mckeevers-5`.")


def format_store_prompt(groups, idx, trigger, vendor):
    """Constrain onboarding to a REAL deployed store. When a group's store doesn't
    resolve to exactly one directory entry, ask the ops manager to pick from the
    matching stores. Each button carries the full onboard context + the chosen store's
    unique id, so a click resumes the flow with the selection locked in. Typing a
    better name also works (it just re-enters this gate)."""
    g = groups[idx]
    typed = g.get("store")
    try:
        m = store_map.match_store(typed)
    except Exception:
        log.exception("match_store failed for %s", typed)
        m = {"status": "none", "candidates": [], "count": 0}
    nums = ", ".join(str(c) for c in (g.get("carts") or []))
    ctx = {"groups": _clean_groups(groups), "resolveIdx": idx, "trigger": trigger, "vendor": vendor}
    cands = m.get("candidates") or []
    btns = [{"type": "button", "text": {"type": "plain_text", "text": _cand_label(c)},
             "action_id": f"rsa_set_store_{i}",
             "value": json.dumps({**ctx, "store": c["store"]})}
            for i, c in enumerate(cands)]
    head = _store_prompt_text(typed, m)
    if nums:
        head += f"  _(cart(s) {nums})_"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for i in range(0, len(btns), 5):                      # ≤5 buttons per actions row
        blocks.append({"type": "actions", "elements": btns[i:i + 5]})
    # Offer to add as a new launch whenever the store isn't an exact match —
    # both "none" (nothing found) and "suggest" (similar stores exist but this
    # specific location isn't in the directory yet, e.g. a new Weis store).
    if m.get("status") in ("none", "suggest"):
        new_launch_ctx = {**ctx, "typedStore": typed}
        label = "Add as New Launch" if m.get("status") == "none" else "Not listed - Add as New Launch"
        blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": label, "emoji": False},
             "style": "primary", "action_id": "rsa_new_launch_open",
             "value": json.dumps(new_launch_ctx)}]})
    return {"text": f'Which store did you mean for "{typed}"?', "blocks": blocks}


def store_text_prompt(typed, m):
    """Text-only version of the store ask (no resume buttons) for the single-cart
    (by-serial) path, which isn't wired through store groups. Lists the candidate
    stores so the ops manager can re-send with a valid name/nickname/ID."""
    head = _store_prompt_text(typed, m)
    cands = m.get("candidates") or []
    if cands:
        head += "\n" + "\n".join(f"• *{_cand_label(c)}*  — `{c.get('store')}`" for c in cands)
        head += "\n_Re-send using one of these (store name, nickname, or external store ID)._"
    return {"text": head}


# Quick-pick states for the "what state is this?" prompt (candidates come first).
COMMON_STATES = ["NJ", "NY", "PA", "MD", "VA", "DE", "NV", "AZ", "GA", "CA", "TX", "OH", "FL"]


def format_state_prompt(groups, idx, trigger, vendor):
    """Ask the ops manager for a store's state when the Caper deployment address can't
    pin it down (missing/ambiguous). We never guess. Buttons carry the full onboard
    context so a click resumes the flow; the answer is remembered as an override."""
    g = groups[idx]
    store = g.get("store")
    cands = list(g.get("_stateCandidates") or [])
    opts, seen = [], set()
    for s in cands + COMMON_STATES:
        if s not in seen:
            opts.append(s); seen.add(s)
    opts = opts[:15]
    ctx = {"groups": _clean_groups(groups), "resolveIdx": idx, "trigger": trigger, "vendor": vendor}
    btns = [{"type": "button", "text": {"type": "plain_text", "text": s},
             "action_id": f"rsa_set_state_{i}", "value": json.dumps({**ctx, "state": s})}
            for i, s in enumerate(opts)]
    why = (f"its Caper address is ambiguous (could be {', '.join(cands)})" if cands
           else "I couldn't find this store's address in the Caper deployment sheet")
    nums = ", ".join(str(c) for c in (g.get("carts") or []))
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*What state is _{store}_ in?*  (cart(s) {nums})\n"
        f"I don't want to guess — {why}. Pick below, or just type it "
        f"(e.g. `{store} is in VA`). _I'll remember it for next time._")}}]
    for i in range(0, len(btns), 5):
        blocks.append({"type": "actions", "elements": btns[i:i + 5]})
    return {"text": f"What state is {store} in?", "blocks": blocks}


def format_trigger_prompt_multi(groups):
    """Ask for the ONE service trigger that applies to every store in the request.
    Each button carries ALL store groups in its value, so a single click completes the
    whole (possibly multi-store) onboard with no server-side state. Typing also works."""
    summary = _groups_summary(groups)
    if len(groups) == 1:
        g = groups[0]
        cart_str = ", ".join(str(c) for c in (g.get("carts") or []))
        head = (f"*Before I add cart(s) {cart_str} at _{g.get('store')}_ ({g.get('state')})* — "
                f"what's the *service trigger*? _(required for the tracker)_\nPick one below, or just type it.")
        alt = f"What's the service trigger for cart(s) {cart_str} at {g.get('store')}?"
    else:
        head = (f"*Before I onboard these carts across {len(groups)} stores* — what's the "
                f"*service trigger*? _(required; applies to all stores)_\n{summary}\n"
                f"Pick one below, or just type it.")
        alt = f"What's the service trigger for {len(groups)} stores?"
    ctx = {"groups": _clean_groups(groups)}
    btns = [{"type": "button", "text": {"type": "plain_text", "text": t},
             "action_id": f"rsa_set_trigger_{i}",
             "value": json.dumps({**ctx, "trigger": t})}
            for i, t in enumerate(TRIGGER_OPTIONS)]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for i in range(0, len(btns), 5):                      # ≤5 buttons per actions block
        blocks.append({"type": "actions", "elements": btns[i:i + 5]})
    return {"text": alt, "blocks": blocks}


def format_vendor_prompt_multi(groups, trigger):
    """NJ rule across one or more stores: ask the RSA vendor ONCE; the choice applies
    to every store in the request. Each button carries all groups + the trigger."""
    summary = _groups_summary(groups)
    nj = [g.get("store") for g in groups if sheets.is_nj(g.get("state") or "NJ")]
    ctx = {"groups": _clean_groups(groups), "trigger": trigger}
    btns = []
    for i, v in enumerate(VENDOR_OPTIONS):
        b = {"type": "button", "text": {"type": "plain_text", "text": v},
             "action_id": f"rsa_set_vendor_{i}", "value": json.dumps({**ctx, "vendor": v})}
        if v == "TRUNO":
            b["style"] = "primary"
        btns.append(b)
    ws_emails = ", ".join(sheets.WINTERSCALES_EMAILS) or "the Winter Scales team"
    nj_str = ", ".join(nj) or "these NJ stores"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*NJ store(s) in this request* — who should run the RSA for {nj_str}? _(applies to all)_\n"
        f"{summary}\n"
        f"• *TRUNO* — I add the cart(s) and schedule TRUNO (Nicole emailed automatically), the usual flow.\n"
        f"• *Winter Scales* — I add the cart(s) as *Pending RSA* and email {ws_emails} to request scheduling; "
        f"you'll then need to follow up with Winter Scales directly.\n"
        f"_Pick one below, or just type it — e.g._ `... vendor Winter Scales`.")}}]
    blocks.append({"type": "actions", "elements": btns})
    return {"text": f"NJ — TRUNO or Winter Scales for {len(groups)} store(s)?", "blocks": blocks}


def format_onboard(result):
    if not result:
        return {"text": "❌ No response from server."}
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    steps = result.get("steps") or {}
    vendor = result.get("vendor") or "TRUNO"
    s1 = steps.get("step1_rsaTracker") or {}
    s1_icon = "❌" if s1.get("error") else ("➕" if s1.get("action") == "added" else "✏️")
    s1_label = (f"Failed — {s1['error']}" if s1.get("error")
                else ("Cart added to RSA Tracker" if s1.get("action") == "added" else "Cart updated in RSA Tracker"))
    lines = [f"*🛒 Cart Onboarded: `{result.get('cartId')}`*  at _{result.get('store')}_  ·  vendor: *{vendor}*", "",
             f"{s1_icon} *Step 1 — RSA Tracker:* {s1_label}"]
    truno_url = None
    if vendor == sheets.WINTERSCALES_VENDOR:
        ws = steps.get("step2_winterScales") or {}
        ws_to = ", ".join(ws.get("emailTo") or sheets.WINTERSCALES_EMAILS)
        if ws.get("error"):
            lines.append(f"❌ *Step 2 — Winter Scales:* {ws['error']}")
        elif ws.get("emailSent"):
            lines.append(f"⚖️ *Step 2 — Winter Scales:* added as *Pending RSA*; emailed {ws_to} to request scheduling.")
            lines.append("📌 Winter Scales isn't auto-scheduled — please follow up with them directly to lock a date.")
        else:
            lines.append(f"⚠️ *Step 2 — Winter Scales:* couldn't auto-email ({ws.get('reason') or 'send failed'}).")
            lines.append(f"📌 Please contact Winter Scales directly: {ws_to}.")
    else:
        s2 = steps.get("step2_trunoSchedule") or {}
        truno_url = s2.get("trunoUrl")
        s2_icon = "❌" if s2.get("error") else "📋"
        s2_label = (f"Truno request failed — {s2['error']}" if s2.get("error")
                    else "Submitted to TRUNO — *Pending Truno Scheduled*; Nicole will set the date"
                         + (f" (cc: <@{NICOLE_SLACK_ID}>)" if NICOLE_SLACK_ID else ""))
        lines.append(f"{s2_icon} *Step 2 — TRUNO Schedule:* {s2_label}")
        if s2.get("emailSkipped"):
            lines.append(f"📧 Nicole (`{s2.get('emailTo')}`) emailed automatically by the scheduled trigger (test carts excluded).")
        elif s2.get("emailSent"):
            lines.append(f"📧 Email sent to Nicole at `{s2.get('emailTo')}`")
        elif not s2.get("error"):
            lines.append("📧 Email to Nicole queued")
    text = "\n".join([ln for ln in lines if ln])
    comp = result.get("compliance") or {}
    if isinstance(comp, dict) and comp.get("slackText"):
        text += "\n\n" + comp["slackText"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    btns = [_btn("📊 RSA Tracker", result.get("trackerUrl"), "primary")]
    if truno_url:
        btns.append(_btn("📋 TRUNO Tracker", truno_url))
    blocks += _actions(*btns)
    return {"text": f"Cart {result.get('cartId')} onboarded at {result.get('store')}", "blocks": blocks}


def format_onboard_multi(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    carts = result.get("carts", [])
    trig = result.get("trigger")
    vendor = result.get("vendor") or "TRUNO"
    head = f"*🛒 Onboarding at _{result.get('store')}_ ({result.get('state')})*"
    if trig:
        head += f"  ·  trigger: *{trig}*"
    head += f"  ·  vendor: *{vendor}*"
    lines = [head]
    ok_nums = []
    for c in carts:
        if c.get("error"):
            lines.append(f"❌ Cart {c.get('cartNum')}: {c['error']}")
        else:
            icon = "➕" if c.get("action") == "added" else "✏️"
            lines.append(f"{icon} Cart {c['cartNum']} → `{c['cartId']}` ({c.get('action')})")
            ok_nums.append(str(c.get("cartNum")))
    truno_url = None
    if vendor == sheets.WINTERSCALES_VENDOR:
        ws = result.get("winterScales", {}) or {}
        ws_to = ", ".join(ws.get("emailTo") or sheets.WINTERSCALES_EMAILS)
        if ws.get("error"):
            lines.append(f"❌ Winter Scales email: {ws['error']}")
        elif ws.get("emailSent"):
            lines.append(f"⚖️ Added as *Pending RSA*. Emailed Winter Scales ({ws_to}) to request scheduling for cart(s) {', '.join(ok_nums) or '—'}.")
            lines.append("📌 Winter Scales isn't auto-scheduled — please follow up with them directly to lock a date.")
        else:
            lines.append(f"⚠️ Couldn't auto-email Winter Scales — {ws.get('reason') or 'send failed'}.")
            lines.append(f"📌 Please contact Winter Scales directly: {ws_to}.")
    else:
        tr = result.get("truno", {}) or {}
        truno_url = tr.get("trunoUrl")
        if tr.get("error"):
            lines.append(f"❌ TRUNO: {tr['error']}")
        else:
            lines.append(f"📋 Submitted to TRUNO for cart(s) {', '.join(ok_nums) or '—'} — RSA status *Pending Truno Scheduled* (no date yet).")
            lines.append("📧 Nicole emailed automatically to schedule (test carts excluded). Once she confirms, the tracker flips to *Scheduled RSA* with the date.")
    text = "\n".join(lines)
    comp = result.get("compliance") or {}
    if isinstance(comp, dict) and comp.get("slackText"):
        text += "\n\n" + comp["slackText"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    btns = [_btn("📊 RSA Tracker", sheets.webapp_url(), "primary")]
    if truno_url:
        btns.append(_btn("📋 TRUNO Tracker", truno_url))
    blocks += _actions(*btns)
    return {"text": f"Onboarded {len(carts)} cart(s) at {result.get('store')}", "blocks": blocks}


def _store_result_lines(result):
    """Per-store body for the multi-store renderer. Returns (lines, ok_count, truno_url)."""
    carts = result.get("carts", [])
    vendor = result.get("vendor") or "TRUNO"
    head = (f"*🛒 {result.get('store')}* ({result.get('state')})  ·  "
            f"trigger: *{result.get('trigger')}*  ·  vendor: *{vendor}*")
    lines, ok_nums, truno_url = [head], [], None
    for c in carts:
        if c.get("error"):
            lines.append(f"   ❌ Cart {c.get('cartNum')}: {c['error']}")
        else:
            icon = "➕" if c.get("action") == "added" else "✏️"
            lines.append(f"   {icon} Cart {c['cartNum']} → `{c['cartId']}` ({c.get('action')})")
            ok_nums.append(str(c.get("cartNum")))
    if vendor == sheets.WINTERSCALES_VENDOR:
        ws = result.get("winterScales", {}) or {}
        ws_to = ", ".join(ws.get("emailTo") or sheets.WINTERSCALES_EMAILS)
        if ws.get("error"):
            lines.append(f"   ❌ Winter Scales email: {ws['error']}")
        elif ws.get("emailSent"):
            lines.append(f"   ⚖️ Added *Pending RSA*; emailed Winter Scales ({ws_to}) for cart(s) "
                         f"{', '.join(ok_nums) or '—'}. Follow up directly to lock a date.")
        else:
            lines.append(f"   ⚠️ Couldn't auto-email Winter Scales — {ws.get('reason') or 'send failed'}. "
                         f"Contact: {ws_to}.")
    else:
        tr = result.get("truno", {}) or {}
        truno_url = tr.get("trunoUrl")
        if tr.get("error"):
            lines.append(f"   ❌ TRUNO: {tr['error']}")
        else:
            lines.append(f"   📋 Submitted to TRUNO for cart(s) {', '.join(ok_nums) or '—'} — "
                         f"*Pending Truno Scheduled*. Nicole emailed to schedule.")
    comp = result.get("compliance") or {}
    if isinstance(comp, dict) and comp.get("slackText"):
        lines.append(comp["slackText"])
    return lines, len(ok_nums), truno_url


def format_onboard_groups(store_results):
    """Render onboarding across SEVERAL stores: a summary header, one section block per
    store (each stays under Slack's section-text limit), and a single tracker button
    (plus a TRUNO button if any store was handed to TRUNO)."""
    total, truno_url, blocks = 0, None, []
    for result in store_results:
        if result.get("error"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"❌ *{result.get('store') or 'store'}*: {result['error']}"}})
            continue
        lines, ok, t_url = _store_result_lines(result)
        truno_url = truno_url or t_url
        total += ok
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    blocks.insert(0, {"type": "section", "text": {"type": "mrkdwn",
                   "text": f"*🛒 Onboarded {total} cart(s) across {len(store_results)} store(s)*"}})
    btns = [_btn("📊 RSA Tracker", sheets.webapp_url(), "primary")]
    if truno_url:
        btns.append(_btn("📋 TRUNO Tracker", truno_url))
    blocks += _actions(*btns)
    return {"text": f"Onboarded {total} cart(s) across {len(store_results)} store(s)", "blocks": blocks}


def format_update(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    fields = ", ".join(result.get("updated", []) or []) or "nothing"
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
               "text": f"✅ *Updated `{result.get('cartId')}`* at _{result.get('store')}_\nFields: {fields}"}}]
    blocks += _actions(_btn("📊 View Tracker", result.get("trackerUrl"), "primary"))
    return {"text": f"Updated {result.get('cartId')} — {fields}", "blocks": blocks}


def _icon(s):
    if not s:
        return "—"
    low = s.lower()
    if "pass" in low or "complet" in low:
        return "✅"
    if "fail" in low or "redispatch" in low:
        return "❌"
    if "sched" in low:
        return "📅"
    return "⏳"


def format_query(result):
    if result.get("error") or not result.get("found"):
        return {"text": result.get("error") or result.get("message") or "Cart not found."}
    days = f"{result['days']}d" if result.get("days") is not None else "—"
    rsa_status = result.get('rsaStatus') or '—'
    sched_date = result.get('rsaSchedDate') or '—'
    compl_date = result.get('rsaComplDate') or '—'
    # Build the date line: show scheduled date always; swap for completion date if RSA is done
    if result.get('rsaComplDate'):
        date_line = f"📅 Scheduled: {sched_date}  ·  ✅ Completed: {compl_date}"
    else:
        date_line = f"📅 RSA Scheduled Date: *{sched_date}*"
    text = "\n".join([
        f"*🛒 `{result['cartId']}`*  |  _{result.get('store')}_  |  {result.get('state') or '—'}",
        f"{_icon(rsa_status)} RSA: *{rsa_status}*  ·  Vendor: {result.get('rsaVendor') or '—'}",
        date_line,
        f"{_icon(result.get('overallStatus'))} Overall: *{result.get('overallStatus') or '—'}*  ·  Days: {days}",
        f"DRI: {result.get('dri') or '—'}  ·  Updated: {result.get('lastUpdated') or '—'}",
    ] + ([f"📝 {result['notes']}"] if result.get("notes") else []))
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    blocks += _actions(_btn("📊 View Tracker", result.get("trackerUrl"), "primary"))
    return {"text": f"Cart {result['cartId']}: {rsa_status} · Scheduled: {sched_date}", "blocks": blocks}


def format_summary(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    c = result.get("counts", {})
    by_state = result.get("byState", {})
    top = "  ·  ".join(f"{s}: {n}" for s, n in sorted(by_state.items(), key=lambda kv: kv[1], reverse=True)[:6])
    csm = c.get('csmPendingThisMonth', 0)
    text = "\n".join([
        "*📊 RSA Tracker Summary*",
        f"Total carts: *{c.get('total', 0)}*  ·  🔴 Needs Action: *{c.get('needsAction', 0)}*  ·  ⏰ Stale 10+ days: *{c.get('stale10plus', 0)}*",
        "",
        f"*RSA*: ⏳ Pending {c.get('pendingRSA', 0)}  📅 Scheduled {c.get('scheduledRSA', 0)}  ✅ Completed {c.get('completedRSA', 0)}  ❌ Failed {c.get('failedRSA', 0)}",
        f"*Overall Passed*: {c.get('overallPassed', 0)}  ·  👤 *Pending with CSM (last 30 days)*: {csm}",
        "",
        f"*Top States*: {top or '—'}",
    ])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    blocks += _actions(_btn("📊 Open Tracker", result.get("trackerUrl"), "primary"))
    return {"text": f"RSA Tracker: {c.get('total', 0)} carts, {c.get('needsAction', 0)} need action", "blocks": blocks}


def _section(text):
    """Slack section block — text must be ≤3000 chars."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _pack_sections(line_groups, limit=2900):
    """Pack groups of lines into section blocks, each staying under `limit` chars.
    Each group is a list of strings that must stay together (header + its rows).
    Never splits a group across two blocks; starts a new block when needed."""
    blocks, buf = [], []
    for group in line_groups:
        chunk = "\n".join(group)
        candidate = "\n".join(buf + group) if buf else chunk
        if buf and len(candidate) > limit:
            blocks.append(_section("\n".join(buf)))
            buf = group[:]
        else:
            buf.extend(group)
    if buf:
        blocks.append(_section("\n".join(buf)))
    return blocks


def format_monthly_summary(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    month = result.get("month", "This month")
    n_comp = result.get("completedCount", 0)
    n_rsa  = result.get("rsaScheduledCount", 0)
    n_trun = result.get("trunoScheduledCount", 0)
    text = (f"*📅 {month} — RSA Visit Summary*\n"
            f"✅ Completed: *{n_comp}*  ·  📋 RSA Scheduled: *{n_rsa}*  ·  🔧 TRUNO Scheduled: *{n_trun}*")
    blocks = [_section(text)]
    btns = [_btn("📊 RSA Tracker", result.get("trackerUrl"), "primary"),
            _btn("🔧 TRUNO Tracker", result.get("trunoUrl"))]
    blocks += _actions(*btns)
    return {"text": f"{month}: {n_comp} completed, {n_rsa + n_trun} scheduled", "blocks": blocks}


def format_backfill(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    n_filled  = result.get("filled", 0)
    n_skipped = result.get("skipped", 0)
    dry       = result.get("dryRun", False)
    prefix    = "🔍 *Dry-run preview* — nothing written.\n" if dry else ""
    if n_filled == 0 and n_skipped == 0:
        return {"text": f"{prefix}✅ Nothing to backfill — all completed carts already have their dates."}
    lines = [f"{prefix}*📅 Backfill {'preview' if dry else 'complete'}*"]
    lines.append(f"✅ {'Would fill' if dry else 'Filled'}: *{n_filled}* cart{'s' if n_filled != 1 else ''}  ·  "
                 f"⚠️ No TRUNO match: *{n_skipped}*")
    for d in result.get("details", [])[:20]:
        cols = ", ".join(d.get("wrote", []))
        lines.append(f"  • `{d['cartId']}` ({d['store']}) — {d['visit']} → {cols}")
    if len(result.get("details", [])) > 20:
        lines.append(f"  _…and {len(result['details']) - 20} more_")
    if result.get("noMatch"):
        lines.append(f"\n*No TRUNO match found for:*")
        for nm in result["noMatch"][:10]:
            lines.append(f"  • `{nm['cartId']}` ({nm['store']}) — {nm['status']}")
        if len(result["noMatch"]) > 10:
            lines.append(f"  _…and {len(result['noMatch']) - 10} more_")
    text = "\n".join(lines)
    blocks = [_section(text)]
    if result.get("trackerUrl"):
        blocks += _actions(_btn("📊 RSA Tracker", result["trackerUrl"], "primary"))
    return {"text": f"Backfill: {n_filled} filled, {n_skipped} unmatched", "blocks": blocks}


def format_list(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    if result.get("total", 0) == 0:
        return {"text": "🔍 No carts match the given filters."}
    header = (f"*{result['total']} carts* (showing first {result['shown']}):"
              if result.get("total", 0) > result.get("shown", 0)
              else f"*{result['total']} cart{'s' if result['total'] != 1 else ''}*:")
    lines = []
    for c in result.get("carts", []):
        days = f" · {c['days']}d" if c.get("days") is not None else ""
        sched = c.get('rsaSchedDate') or ''
        compl = c.get('rsaComplDate') or ''
        if compl:
            date_str = f" · ✅ {compl}"
        elif sched:
            date_str = f" · 📅 {sched}"
        else:
            date_str = ""
        lines.append(f"• `{c['cartId']}` — {c.get('store') or '?'} ({c.get('state') or '?'}) · "
                     f"*{c.get('rsaStatus') or '—'}*{date_str}{days}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n" + "\n".join(lines)}}]
    blocks += _actions(_btn("📊 View Tracker", result.get("trackerUrl"), "primary"))
    return {"text": f"{result['total']} carts found", "blocks": blocks}


def format_upcoming(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    visits = result.get("visits", [])
    days_ahead = result.get("daysAhead", 60)
    store_f = result.get("storeFilter")
    cart_f  = result.get("cartFilter")

    title_parts = ["upcoming visits"]
    if store_f:
        title_parts.append(f"at _{store_f}_")
    if cart_f:
        title_parts.append(f"cart #{cart_f}")
    title_parts.append(f"(next {days_ahead} days)")
    header = " ".join(title_parts).capitalize()

    if not visits:
        return {"text": f"📅 No {header.lower()}."}

    lines = []
    for v in visits:
        vendor = v.get("rsaVendor") or ""
        vendor_str = f" · {vendor}" if vendor else ""
        status = v.get("rsaStatus") or ""
        status_str = f" · {status}" if status else ""
        source_str = " _(Truno)_" if v.get("source") == "Truno" else ""
        lines.append(
            f"• 📅 *{v['visitDate']}* — `{v['cartId']}` {v.get('store') or ''}  "
            f"({v.get('state') or '?'}){vendor_str}{status_str}{source_str}"
        )

    text = f"*{header}* — {len(visits)} visit{'s' if len(visits) != 1 else ''}\n" + "\n".join(lines)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    # Link to tracker (use the first visit's URL or fallback)
    tracker_url = next((v.get("trackerUrl") for v in visits if "trackerUrl" in v), None)
    if tracker_url:
        blocks += _actions(_btn("📊 View Tracker", tracker_url, "primary"))
    return {"text": f"{len(visits)} upcoming visit(s) found", "blocks": blocks}


def format_scheduled_no_truno(result):
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    carts = result.get("carts", [])
    if not carts:
        return {"text": "✅ All *Scheduled RSA* carts have a matching Truno row — nothing missing."}
    lines = [f"*{len(carts)} Scheduled RSA cart{'s' if len(carts) != 1 else ''} with no Truno row:*"]
    for c in carts:
        sched = f" · sched {c['rsaSchedDate']}" if c.get("rsaSchedDate") else ""
        vendor = f" · {c['rsaVendor']}" if c.get("rsaVendor") else ""
        lines.append(
            f"• `{c['cartId']}` — {c.get('store') or '?'} ({c.get('state') or '?'})"
            f"{vendor}{sched}"
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    blocks += _actions(
        _btn("📊 RSA Tracker", result.get("trackerUrl"), "primary"),
        _btn("📋 Truno Tracker", result.get("trunoUrl")),
    )
    return {"text": f"{len(carts)} Scheduled RSA cart(s) missing from Truno", "blocks": blocks}


def format_compliance(result):
    if result.get("slackText"):
        return {"text": result["slackText"]}
    if result.get("error"):
        return {"text": f"❌ {result['error']}"}
    return {"text": "No compliance info found for that store/state."}


def format_compliance_picker(query, candidates):
    """No single confident compliance match → offer the closest stores in the guide as
    buttons. Each button re-runs the lookup for that exact store; typing the full name also works."""
    head = (f"*📜 Which store did you mean?*  I couldn't pin down _{query}_ in the Compliance "
            f"Guide — here are the closest matches:")
    btns = []
    for i, c in enumerate((candidates or [])[:8]):
        store = c.get("nickname") or c.get("retailer") or "?"
        loc = ", ".join(x for x in (c.get("city"), c.get("state")) if x)
        label = (f"{store} ({loc})" if loc else store)[:74]
        btns.append({"type": "button", "text": {"type": "plain_text", "text": label},
                     "action_id": f"rsa_set_compliance_{i}",
                     "value": json.dumps({"store": store, "retailer": c.get("retailer"),
                                          "nickname": c.get("nickname"), "state": c.get("state"),
                                          "query": query})})
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for i in range(0, len(btns), 5):                       # ≤5 buttons per actions block
        blocks.append({"type": "actions", "elements": btns[i:i + 5]})
    return {"text": f"Which store did you mean for {query}?", "blocks": blocks}


def format_missing(missing, comp, kind="any"):
    label = {"tech": "missing a tech (calibration) result", "test": "missing a W&M test result",
             "both": "missing BOTH results", "any": "missing tech and/or test results"}.get(kind, "missing results")
    head = (f"*🧩 Results completeness — {comp.get('both', 0)}/{comp.get('total', 0)} carts have BOTH*\n"
            f"Missing tech: *{comp.get('missingTech', 0)}*  ·  Missing test: *{comp.get('missingTest', 0)}*  ·  "
            f"Missing both: *{comp.get('neither', 0)}*")
    if not missing:
        return {"text": head + f"\n\n✅ No carts {label}."}
    lines = [head, "", f"*{len(missing)} cart{'s' if len(missing) != 1 else ''} {label}:*"]
    for c in missing[:25]:
        tags = ([] if c["hasTech"] else ["tech"]) + ([] if c["hasTest"] else ["test"])
        lines.append(f"• `{c['cartId']}` — {c.get('store') or '?'} ({c.get('state') or '?'}) · "
                     f"*{c.get('rsaStatus') or '—'}* · missing: {', '.join(tags)}")
    if len(missing) > 25:
        lines.append(f"…and {len(missing) - 25} more.")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    blocks += _actions(_btn("📊 View Tracker", sheets.webapp_url(), "primary"))
    return {"text": f"{len(missing)} cart(s) {label}", "blocks": blocks}


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def help_text():
    return "\n".join([
        "*RSA Agent — what I can do:*",
        '• *New cart (full workflow):* "New cart P_WF_001, ShopRite Forest Hill NJ, trigger Launches"',
        '• *Several stores at once:* "Sprouts 15 — carts 3,4,5,6,7,9,10 / Sprouts 20 — carts 5,6,9, trigger Launches"',
        '• *Update status:* "Cart NJ-001 passed W&M today" or "Set cart 42 RSA to Scheduled RSA"',
        '• *Query cart:* "Status of P_WF_SHOPRITE_72_M3_001?"',
        '• *Filter list:* "Show pending RSA carts in NJ" or "stale carts for Sai"',
        '• *Summary:* "Give me a summary"',
        '• *Monthly report:* "How many visits did we complete in June?" or "What\'s scheduled for July?"',
        '• *Compliance:* "What\'s required for WF 9 - Hoboken, NJ?"',
        '• *Upload — specify the type in your message:* say *tech* or *calibration* → Tech Results; '
        'say *rsa*, *test*, or *w&m* → RSA Test Results.',
        '   _(No keyword? A spreadsheet checklist auto-files as Tech Results; anything else as a W&M test form.)_',
        '• *Gaps:* "Which carts are missing tech or test results?"',
        '• *Upcoming visits:* "When is cart 3 at Sprouts being visited?" or "Upcoming visits for ShopRite 116"',
        '• *Truno gaps:* "Which scheduled carts aren\'t in Truno?" or "Carts missing from Truno"',
    ])


def _post(say, reply, ts):
    if isinstance(reply, str):
        say(text=reply, thread_ts=ts)
    else:
        say(text=reply.get("text", ""), blocks=reply.get("blocks"), thread_ts=ts)


def _norm_state(s):
    s = str(s or "").strip().upper()
    return s or None


def _onboard_groups(intent):
    """Normalize an onboard intent into store groups: [{store, state, carts[str],
    address, notes}]. Supports the multi-store "stores" array AND the legacy
    single-store top-level fields. State is the user-TYPED value if any, else None —
    it is NOT defaulted to NJ; the real state is resolved from the Caper sheet later.
    Empty groups (no cart numbers) are dropped."""
    out = []
    raw = intent.get("stores")
    if isinstance(raw, list) and raw:
        for g in raw:
            if not isinstance(g, dict):
                continue
            nums = g.get("cartNumbers") or g.get("carts") or []
            out.append({"store": g.get("store"),
                        "state": _norm_state(g.get("state") or intent.get("state")),
                        "carts": [str(c) for c in nums],
                        "address": g.get("address") or intent.get("address"),
                        "notes": g.get("notes") or intent.get("notes")})
    else:
        nums = intent.get("cartNumbers") or []
        if nums or intent.get("store"):
            out.append({"store": intent.get("store"),
                        "state": _norm_state(intent.get("state")),
                        "carts": [str(c) for c in nums],
                        "address": intent.get("address"),
                        "notes": intent.get("notes")})
    return [g for g in out if g.get("carts")]


def _resolve_group_stores(groups):
    """Validate each group's store against the Deployed Stores directory — the user may
    only onboard a store that exists there, identified by its store name, nickname,
    external store ID, or retailer. A UNIQUE match is normalized to that store's
    canonical display name + full address (so the state lookup and the Truno tracker use
    the directory's values, not the user's free text). Anything ambiguous or unknown is
    left unresolved (storeKey=None) so the caller asks the user to pick. A previously
    locked-in selection (storeKey) re-resolves by that unique id, so it stays stable.
    Returns (groups, index_of_first_unresolved | -1)."""
    for g in groups:
        try:
            m = store_map.match_store(g.get("storeKey") or g.get("store"))
        except Exception:
            log.exception("match_store failed for %s", g.get("store"))
            m = {"status": "none"}
        if m.get("status") == "matched":
            rec = m["store"]
            g["storeKey"] = rec["store"]                       # unique, stable id
            g["store"]    = rec.get("display") or rec["store"] # friendly name for the trackers
            g["address"]  = rec.get("address") or g.get("address")
        else:
            g["storeKey"] = None
    idx = next((i for i, g in enumerate(groups) if not g.get("storeKey")), -1)
    return groups, idx


def _resolve_group_states(groups):
    """Fill each group's state from the Caper deployment ADDRESS (+ saved overrides).
    Caper/override is authoritative; a user-typed state is used only as a fallback
    when Caper can't resolve. If neither yields a state, it's left None so the caller
    asks instead of guessing. Returns (groups, index_of_first_unresolved | -1)."""
    for g in groups:
        try:
            info = store_map.store_state(g.get("store"))
        except Exception:
            log.exception("store_state lookup failed for %s", g.get("store"))
            info = {"confident": False, "candidates": []}
        if info.get("confident"):
            g["state"] = info["state"]
            g["_stateBasis"] = info["basis"]
            g["_stateCandidates"] = info.get("candidates") or []
        elif g.get("state"):                       # user-typed fallback (not a guess)
            g["state"] = _norm_state(g["state"])
            g["_stateBasis"] = "typed"
            g["_stateCandidates"] = info.get("candidates") or []
        else:
            g["state"] = None
            g["_stateBasis"] = None
            g["_stateCandidates"] = info.get("candidates") or []
    idx = next((i for i, g in enumerate(groups) if not g.get("state")), -1)
    return groups, idx


def _onboard_next(groups, trigger, vendor, user_name):
    """Shared onboarding gate used by both the message handler and the button
    callbacks. Order: confirm each store's STATE (ask if unknown) → ask the service
    TRIGGER once → ask the NJ VENDOR once → onboard every store. Returns a reply dict
    that is either a prompt or the final onboard render."""
    if any(not g.get("store") for g in groups):
        return {"text": "Which store? e.g. *onboard carts 1, 7 at Price Chopper 200*"}
    # Gate 0 — the store must be a real deployed store (by name/nickname/ID/retailer).
    groups, vidx = _resolve_group_stores(groups)
    if vidx >= 0:                                  # not a unique store → ask the user to pick
        return format_store_prompt(groups, vidx, trigger, vendor)
    groups, sidx = _resolve_group_states(groups)
    if sidx >= 0:                                  # a store's state is unknown → ask
        return format_state_prompt(groups, sidx, trigger, vendor)
    if not (trigger or "").strip():                # service trigger required (ask once)
        return format_trigger_prompt_multi(groups)
    if not vendor and any(sheets.is_nj(g.get("state")) for g in groups):
        return format_vendor_prompt_multi(groups, trigger)   # NJ rule (ask once)
    return _run_onboard_groups(groups, trigger, vendor or "TRUNO", user_name)


def handle_message(text, say, client, channel, ts, user_id):
    clean = re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()
    if not clean:
        say(text=help_text(), thread_ts=ts)
        return

    user_name = "Ops Manager"
    try:
        info = client.users_info(user=user_id)
        user_name = info["user"].get("real_name") or info["user"].get("name") or "Ops Manager"
    except Exception:
        pass

    try:
        client.reactions_add(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
    except Exception:
        pass

    try:
        intent = parse_intent(clean, user_name)
        action = intent.get("action")

        if action == "onboardCart":
            # Normalize to store groups — handles one store OR several in one message.
            groups = _onboard_groups(intent)
            trigger = (intent.get("trigger") or "").strip()
            # NJ rule: only treat a vendor as chosen if the user actually named one.
            vendor = sheets._norm_vendor(intent.get("rsaVendor"))
            if groups:
                # State (from Caper), trigger, and NJ vendor are gated here; this only
                # writes once everything needed is known.
                reply = _onboard_next(groups, trigger, vendor, user_name)
            else:
                # No cart numbers — fall back to the single-cart-by-serial (cartId) path.
                state = intent.get("state") or "NJ"
                # Same rule as the groups flow: the store must be a real deployed store.
                store_in = intent.get("store")
                m_store = store_map.match_store(store_in) if store_in else {"status": "matched"}
                if intent.get("cartId") and store_in and m_store.get("status") != "matched":
                    reply = store_text_prompt(store_in, m_store)
                    _post(say, reply, ts)
                    try:
                        client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
                    except Exception:
                        pass
                    return
                if store_in and m_store.get("status") == "matched":
                    intent["store"] = m_store["store"].get("display") or store_in   # canonical name
                need_vendor = sheets.is_nj(state) and not vendor
                if intent.get("cartId") and need_vendor:
                    reply = format_vendor_prompt(intent.get("store"), state, None, trigger,
                                                 intent.get("address"), intent.get("notes"),
                                                 cartId=intent.get("cartId"))
                elif intent.get("cartId"):
                    thinking = say(text="⚙️ Onboarding…", thread_ts=ts)
                    result = sheets.onboard_cart(
                        cartId=intent.get("cartId"), store=intent.get("store"), state=state,
                        trigger=trigger, rsaStatus=intent.get("rsaStatus"), wmStatus=intent.get("wmStatus"),
                        overallStatus=intent.get("overallStatus"), rsaVendor=vendor or "TRUNO",
                        dri=intent.get("dri"), notes=intent.get("notes"), submittedBy=user_name)
                    reply = format_onboard(result)
                    try:
                        client.chat_delete(channel=channel, ts=thinking["ts"])
                    except Exception:
                        pass
                else:
                    reply = {"text": "Which cart number(s) and which store? e.g. *onboard carts 1, 7 at Price Chopper 200*"}

        elif action == "updateCart":
            reply = format_update(sheets.update_cart(
                cartId=intent.get("cartId"), storeHint=intent.get("storeHint") or intent.get("store"),
                rsaStatus=intent.get("rsaStatus"), rsaVendor=intent.get("rsaVendor"),
                rsaSchedDate=intent.get("rsaSchedDate"), wmStatus=intent.get("wmStatus"),
                wmDate=intent.get("wmDate"), overallStatus=intent.get("overallStatus"),
                dri=intent.get("dri"), notes=intent.get("notes"),
                appendNotes=intent.get("appendNotes", True)))

        elif action == "queryCart":
            reply = format_query(sheets.query_cart(
                intent.get("cartId"), intent.get("storeHint") or intent.get("store")))

        elif action == "getSummary":
            reply = format_summary(sheets.get_summary())

        elif action == "listCarts":
            reply = format_list(sheets.list_carts(
                state=intent.get("state"), rsaStatus=intent.get("rsaStatus"),
                wmStatus=intent.get("wmStatus"), overallStatus=intent.get("overallStatus"),
                dri=intent.get("dri"), storeFragment=intent.get("storeFragment") or intent.get("store"),
                staleOnly=intent.get("staleOnly"), limit=intent.get("limit"),
                since_days=intent.get("sinceDays")))

        elif action == "complianceCheck":
            q = intent.get("store") or intent.get("storeHint")
            # Find the closest store(s) in the guide. Several plausible matches → ASK
            # (e.g. 'bigbunny-1' → 'Big Bunny' / 'Big Bunny Market'); a unique exact or a
            # single close match → use it; nothing → fall back to the plain state lookup.
            cands = sheets.compliance_candidates(q, intent.get("state"), intent.get("address"))
            exact = [c for c in cands if c.get("score", 0) >= 100]   # exact incl. the store number
            strong = [c for c in cands if c.get("score", 0) >= 80]   # exact-ish / substring matches
            if len(exact) != 1 and len(strong) >= 2:
                reply = format_compliance_picker(q, strong)          # several plausible → let the user pick
            else:
                pick = exact[0] if len(exact) == 1 else (strong[0] if strong else (cands[0] if cands else None))
                if pick:
                    reply = format_compliance(sheets.lookup_compliance(
                        pick.get("nickname") or pick.get("retailer"), pick.get("state"),
                        intent.get("trigger"), address=intent.get("address")))
                else:
                    reply = format_compliance(sheets.lookup_compliance(
                        q, intent.get("state"), intent.get("trigger"), address=intent.get("address")))

        elif action == "upcomingVisits":
            cart_num = None
            if intent.get("cartNumbers"):
                cart_num = intent["cartNumbers"][0]
            elif intent.get("cartId"):
                cart_num = intent["cartId"]
            reply = format_upcoming(sheets.upcoming_visits(
                store_fragment=intent.get("store") or intent.get("storeFragment"),
                cart_number=cart_num,
                days_ahead=int(intent.get("daysAhead") or 60),
            ))

        elif action == "scheduledNoTruno":
            reply = format_scheduled_no_truno(sheets.scheduled_no_truno())

        elif action == "missingResults":
            kind = (intent.get("resultsKind") or "any").lower()
            reply = format_missing(sheets.carts_missing_results(which=kind),
                                   sheets.results_completeness(), kind=kind)

        elif action == "monthlySummary":
            reply = format_monthly_summary(sheets.get_monthly_summary(
                month=intent.get("month"), year=intent.get("year")))

        elif action == "backfillDates":
            dry_run = bool(intent.get("dryRun"))
            reply = format_backfill(backfill_dates.run(dry_run=dry_run))

        elif action == "fixDri":
            dry_run = bool(intent.get("dryRun"))
            res = sheets.normalize_tracker(dry_run=dry_run)
            if res.get("error"):
                reply = {"text": f"❌ DRI fix failed: {res['error']}"}
            else:
                changed = res.get("changedRows", 0)
                rsa_fix = res.get("rsaFixed", 0)
                csm_n   = res.get("driCsm", 0)
                hw_n    = res.get("driHwops", 0)
                overall = res.get("overallPassed", 0)
                if dry_run:
                    reply = {"text": (
                        f"🔍 *Tracker dry-run* — would update *{changed}* row(s):\n"
                        + (f"  • RSA → Completed RSA: {rsa_fix}  (Overall=Passed, stuck at Scheduled)\n" if rsa_fix else "")
                        + f"  • DRI → CSM: {csm_n}  (Completed RSA)\n"
                        + f"  • DRI → HW Ops: {hw_n}  (all others)\n"
                        + (f"  • Overall → Passed: {overall}\n" if overall else "")
                        + f"Run again without 'dry run' to apply."
                    )}
                elif changed == 0:
                    reply = {"text": "✅ Tracker check complete — all rows already correct, nothing changed."}
                else:
                    reply = {"text": (
                        f":broom: *Tracker fixed* — updated *{changed}* row(s):\n"
                        + (f"  • RSA → Completed RSA: {rsa_fix}  (were stuck at Scheduled)\n" if rsa_fix else "")
                        + f"  • DRI → CSM: {csm_n}  (Completed RSA)\n"
                        + f"  • DRI → HW Ops: {hw_n}  (Pending / Scheduled / Failed)\n"
                        + (f"  • Overall → Passed: {overall}\n" if overall else "")
                    )}

        else:
            reply = {"text": f"🤔 {intent.get('clarification') or 'Not sure what you need.'}\n\n{help_text()}"}

        try:
            client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
        except Exception:
            pass
        _post(say, reply, ts)

    except Exception as e:
        log.exception("Handler error")
        try:
            client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
        except Exception:
            pass
        say(text=f"❌ Error: {e}", thread_ts=ts)


# ── File uploads — W&M test forms (pass/fail) OR tech calibration checklists ─────
# Routing precedence (so a user can explicitly COMMAND the destination):
#   1. Explicit in the message: say "tech"/"calibration" → Tech Results; say "rsa"/
#      "test"/"w&m"/"scale test" → RSA Test Results. The message wins over everything.
#   2. Otherwise auto-detect: a spreadsheet (the Weight-check checklist is always a
#      spreadsheet, W&M forms never are) or a tech/checklist filename → Tech Results.
#   3. Otherwise → W&M Scale Test Form (the default).
_TECH_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")
_TECH_DIRECTIVE = re.compile(r"\b(tech|technician|calibrat\w*|cal sheet)\b")
_TEST_DIRECTIVE = re.compile(r"\b(rsa|w m|wm|test|weights? and measures|scale test)\b")
_TECH_FILE_KEYWORD = re.compile(r"\b(tech|calibrat\w*|weight check|checklist)\b")
# Strong W&M-form signals in a FILENAME — specific enough to override the "spreadsheet ⇒
# tech" default, so a W&M scale-test form exported as .xlsx still files as a W&M test.
_TEST_FILE_KEYWORD = re.compile(r"\b(scale test|scaletest|w m|weights? and measures)\b")


def _route_upload(caption, filename):
    """Return 'tech' or 'test' for an uploaded file. Explicit caption directive wins."""
    cap = re.sub(r"[^a-z0-9]+", " ", (caption or "").lower())
    if _TECH_DIRECTIVE.search(cap):            # explicit "tech"/"calibration"
        return "tech"
    if _TEST_DIRECTIVE.search(cap):            # explicit "rsa"/"test"/"w&m"
        return "test"
    fn = (filename or "").lower()
    fn_clean = re.sub(r"[^a-z0-9]+", " ", fn)
    if _TEST_FILE_KEYWORD.search(fn_clean):    # a 'scale test'/'w&m' filename is a W&M form…
        return "test"                          # …even when saved as a spreadsheet
    if fn.endswith(_TECH_EXTS):                # otherwise a spreadsheet is a tech checklist
        return "tech"
    if _TECH_FILE_KEYWORD.search(fn_clean):
        return "tech"
    return "test"


def _is_tech_upload(caption, filename):        # back-compat helper
    return _route_upload(caption, filename) == "tech"


def handle_files(files, say, client, channel, ts, caption=""):
    try:
        client.reactions_add(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
    except Exception:
        pass
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    for f in files:
        name = f.get("name", "upload")
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            say(text=f"⚠️ Couldn't read `{name}` (no download URL).", thread_ts=ts)
            continue
        try:
            data = requests.get(url, headers=headers, timeout=60).content
        except Exception as e:
            say(text=f"⚠️ Couldn't download `{name}`: {e}", thread_ts=ts)
            continue
        if _route_upload(caption, name) == "tech":
            summary = tech_forms.process_upload(data, name, link=f.get("permalink"))
            say(text=format_tech_results(name, summary), thread_ts=ts)
        else:
            summary = test_forms.process_upload(data, name, link=f.get("permalink"))
            say(text=format_form_results(name, summary), thread_ts=ts)
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
    except Exception:
        pass


def format_tech_results(name, summary):
    if summary.get("error"):
        return f"❌ `{name}`: {summary['error']}"
    res = summary.get("results", [])
    if not res:
        return f"⚠️ `{name}`: parsed, but no carts matched the tracker."
    head = f"🔧 *{name}* — {summary.get('store', '?')}" + (f" · {summary['serviceDate']}" if summary.get("serviceDate") else "")
    if summary.get("technician"):
        head += f" · tech {summary['technician']}"
    lines = [head]
    for r in res:
        if not r.get("ok"):
            lines.append(f"⚠️ Cart {r['cart']}: {r['detail']}")
        else:
            lines.append(f"✅ Cart {r['cart']} → `{r['cartId']}` · {r.get('readings', 'calibration recorded')}")
    return "\n".join(lines)


def format_form_results(name, summary):
    if summary.get("error"):
        return f"❌ `{name}`: {summary['error']}"
    res = summary.get("results", [])
    if not res:
        return f"⚠️ `{name}`: parsed, but no carts matched the tracker."
    head = f"📄 *{name}* — {summary.get('store', '?')}" + (f" · {summary['serviceDate']}" if summary.get("serviceDate") else "")
    lines = [head]
    for r in res:
        if not r.get("ok"):
            lines.append(f"⚠️ Cart {r['cart']} (`{r['cartId']}`): {r['detail']}")
        else:
            mark = "✅" if r.get("passed") is True else "❌" if r.get("passed") is False else "❔"
            lines.append(f"{mark} Cart {r['cart']} → `{r['cartId']}` · {r['detail']}")
    passed = [str(r["cart"]) for r in res if r.get("ok") and r.get("passed") is True]
    if passed:
        tag = f" · cc {CSM_MENTION}" if CSM_MENTION else ""
        lines.append(f"✅ Passed RSA → *Passed* (complete): {', '.join(passed)}.{tag}")
    return "\n".join(lines)


# ── Event listeners ─────────────────────────────────────────────────────────────
@slack.event("app_mention")
def on_mention(event, say, client):
    handle_message(event.get("text", ""), say, client, event["channel"], event["ts"], event["user"])


@slack.event("message")
def on_dm(event, say, client):
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    files = event.get("files") or []
    if files:                                   # someone uploaded a test form or tech calibration sheet
        handle_files(files, say, client, event["channel"], event["ts"], caption=event.get("text", ""))
        return
    if event.get("subtype"):
        return
    handle_message(event.get("text", ""), say, client, event["channel"], event["ts"], event["user"])


def _btn_context(body, client):
    """Shared decode for the onboard buttons: the JSON payload, the resolved
    submitter name, and the channel/ts of the prompt message to update in place."""
    acts = body.get("actions") or []
    payload = json.loads((acts[0].get("value") if acts else "") or "{}")
    user_id = (body.get("user") or {}).get("id", "")
    user_name = (body.get("user") or {}).get("name") or "Ops Manager"
    try:
        info = client.users_info(user=user_id)
        user_name = info["user"].get("real_name") or info["user"].get("name") or user_name
    except Exception:
        pass
    channel = (body.get("container") or {}).get("channel_id") or (body.get("channel") or {}).get("id")
    ts = (body.get("container") or {}).get("message_ts") or (body.get("message") or {}).get("ts")
    return payload, user_name, channel, ts


def _truncate_slack_message(reply, max_chars=3000):
    """Ensure message blocks don't exceed Slack's 3000-character limit for text blocks.
    Truncates text and appends a truncation notice if needed.
    Returns (updated_reply, full_text) where full_text is non-None if truncation occurred."""
    if not reply or not reply.get("blocks"):
        return reply, None

    truncated_any = False
    full_text = None
    updated_blocks = []
    for block in reply["blocks"]:
        if block.get("type") == "section" and block.get("text", {}).get("type") == "mrkdwn":
            text = block["text"].get("text", "")
            if len(text) > max_chars:
                truncated_any = True
                if not full_text:
                    full_text = text  # Save the full text for thread reply
                truncated = text[:max_chars - 50] + f"\n\n_… (message truncated; see thread for full details)_"
                block = dict(block)  # Shallow copy to avoid mutating original
                block["text"] = dict(block["text"])  # Copy text dict
                block["text"]["text"] = truncated
        updated_blocks.append(block)

    updated_reply = {"text": reply.get("text", ""), "blocks": updated_blocks}
    return updated_reply, full_text if truncated_any else None


def _update_or_post(client, channel, ts, reply):
    """Replace the prompt message with the result; fall back to a thread reply.
    If the message was truncated, posts the full details in a thread."""
    # Ensure Slack message constraints are met
    reply, truncated_text = _truncate_slack_message(reply)

    try:
        client.chat_update(channel=channel, ts=ts, text=reply.get("text", ""), blocks=reply.get("blocks"))
    except Exception:
        try:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=reply.get("text", ""), blocks=reply.get("blocks"))
        except Exception:
            log.exception("could not update/post onboard message")

    # If truncation occurred, post the full details in the thread
    if truncated_text:
        try:
            # Split long text into chunks if needed
            max_msg_len = 4000  # Conservative limit for text messages
            if len(truncated_text) > max_msg_len:
                chunks = [truncated_text[i:i+max_msg_len] for i in range(0, len(truncated_text), max_msg_len)]
                for i, chunk in enumerate(chunks):
                    prefix = "📎 Full details (part {}/{}):".format(i+1, len(chunks)) if len(chunks) > 1 else "📎 Full details:"
                    client.chat_postMessage(channel=channel, thread_ts=ts,
                                          text=f"{prefix}\n```\n{chunk}\n```")
            else:
                client.chat_postMessage(channel=channel, thread_ts=ts,
                                      text=f"📎 Full details:\n```\n{truncated_text}\n```")
        except Exception:
            log.exception("could not post full details in thread")


def _run_onboard_groups(groups, trigger, vendor, user_name):
    """Onboard every store group with the shared trigger/vendor (the 'ask once'
    semantics). One store → the familiar single-store render; several → the combined
    multi-store render."""
    results = []
    for g in groups:
        results.append(sheets.onboard_carts(
            g.get("carts") or [], store=g.get("store"), state=g.get("state") or "NJ",
            trigger=trigger, notes=g.get("notes"), submittedBy=user_name,
            address=g.get("address"), rsaVendor=vendor))
    if len(results) == 1:
        return format_onboard_multi(results[0])
    return format_onboard_groups(results)


def _run_onboard_and_render(payload, user_name, vendor):
    """Finish an onboard from a decoded button payload using the chosen vendor.
    Handles the multi-store (groups), multi-cart (cartNumbers) and single-cart
    (cartId) shapes."""
    groups = payload.get("groups")
    if isinstance(groups, list) and groups:
        return _run_onboard_groups(groups, payload.get("trigger"), vendor, user_name)
    carts = payload.get("carts") or []
    if carts:
        result = sheets.onboard_carts(
            carts, store=payload.get("store"), state=payload.get("state") or "NJ",
            trigger=payload.get("trigger"), notes=payload.get("notes"),
            submittedBy=user_name, address=payload.get("address"), rsaVendor=vendor)
        return format_onboard_multi(result)
    if payload.get("cartId"):
        result = sheets.onboard_cart(
            cartId=payload.get("cartId"), store=payload.get("store"), state=payload.get("state") or "NJ",
            trigger=payload.get("trigger"), notes=payload.get("notes"),
            submittedBy=user_name, address=payload.get("address"), rsaVendor=vendor)
        return format_onboard(result)
    return {"text": "❌ Lost the cart context — please re-send the onboard request."}


def _complete_onboard_from_button(body, client):
    """A service-trigger button was clicked. We now have the trigger; the shared gate
    then asks the NJ vendor (once) if needed, or onboards. Legacy single-store/cartId
    payloads keep their original path."""
    payload, user_name, channel, ts = _btn_context(body, client)
    vendor = sheets._norm_vendor(payload.get("vendor"))
    groups = payload.get("groups")
    if isinstance(groups, list) and groups:
        _update_or_post(client, channel, ts,
                        _onboard_next(groups, payload.get("trigger"), vendor, user_name))
        return
    state = payload.get("state") or "NJ"
    if sheets.is_nj(state) and not vendor:
        prompt = format_vendor_prompt(payload.get("store"), state, payload.get("carts"),
                                      payload.get("trigger"), payload.get("address"),
                                      payload.get("notes"), cartId=payload.get("cartId"))
        _update_or_post(client, channel, ts, prompt)
        return
    _update_or_post(client, channel, ts, _run_onboard_and_render(payload, user_name, vendor or "TRUNO"))


def _complete_onboard_with_vendor(body, client):
    """A vendor button (TRUNO / Winter Scales) was clicked — finish the onboard with
    that vendor. TRUNO schedules as usual; Winter Scales emails the WS contacts."""
    payload, user_name, channel, ts = _btn_context(body, client)
    vendor = sheets._norm_vendor(payload.get("vendor")) or "TRUNO"
    groups = payload.get("groups")
    if isinstance(groups, list) and groups:
        _update_or_post(client, channel, ts,
                        _onboard_next(groups, payload.get("trigger"), vendor, user_name))
        return
    _update_or_post(client, channel, ts, _run_onboard_and_render(payload, user_name, vendor))


def _complete_store_from_button(body, client):
    """A store-pick button was clicked for an ambiguous/unknown store. Lock in the
    chosen store's unique id and resume the shared onboarding gate — which re-resolves
    it to the canonical name + address, then asks for state / trigger / vendor or
    finally onboards (writing the directory address to the Truno tracker)."""
    payload, user_name, channel, ts = _btn_context(body, client)
    groups = payload.get("groups") or []
    idx = payload.get("resolveIdx")
    chosen = payload.get("store")                  # the unique Deployed-Stores id
    if isinstance(idx, int) and 0 <= idx < len(groups) and chosen:
        groups[idx]["storeKey"] = chosen
        groups[idx]["store"] = chosen              # re-resolved to the display name in the gate
    _update_or_post(client, channel, ts,
                    _onboard_next(groups, payload.get("trigger"),
                                  sheets._norm_vendor(payload.get("vendor")), user_name))


def _complete_state_from_button(body, client):
    """A state button was clicked for an unresolved store. Record it (remembered as an
    override so we never ask again), then resume the shared onboarding gate — which
    asks the next unknown state, the trigger, the vendor, or finally onboards."""
    payload, user_name, channel, ts = _btn_context(body, client)
    groups = payload.get("groups") or []
    idx = payload.get("resolveIdx")
    state = (payload.get("state") or "").strip().upper()
    if isinstance(idx, int) and 0 <= idx < len(groups) and state:
        groups[idx]["state"] = state
        try:
            store_map.save_state_override(groups[idx].get("store"), state)
        except Exception:
            log.exception("could not save state override")
    _update_or_post(client, channel, ts,
                    _onboard_next(groups, payload.get("trigger"),
                                  sheets._norm_vendor(payload.get("vendor")), user_name))


def _complete_compliance_from_button(body, client):
    """A compliance-match button was clicked — re-run the lookup for that exact store
    (nickname + state) and show its pre/post requirements in place of the picker."""
    payload, user_name, channel, ts = _btn_context(body, client)
    store = payload.get("nickname") or payload.get("retailer") or payload.get("store")
    comp = sheets.lookup_compliance(store, payload.get("state"))
    _update_or_post(client, channel, ts, format_compliance(comp))


def _open_new_launch_modal(body, client):
    """'Add as New Launch' was clicked. Open a Slack modal so the ops manager can fill
    in the store details. The onboarding context is stashed in private_metadata so the
    submission handler can resume the flow once the store is written to the directory."""
    acts = body.get("actions") or []
    raw = acts[0].get("value", "{}") if acts else "{}"
    try:
        ctx = json.loads(raw)
    except Exception:
        ctx = {}
    typed = ctx.get("typedStore", "")
    trigger_id = body.get("trigger_id", "")
    if not trigger_id:
        return
    # Pre-fill nickname from the typed text so the manager only has to confirm/edit.
    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "rsa_new_launch_submit",
            "private_metadata": json.dumps(ctx),
            "title": {"type": "plain_text", "text": "Add New Launch Store"},
            "submit": {"type": "plain_text", "text": "Add Store"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"*\"{typed}\"* isn't in the deployed stores directory yet. "
                             "Fill in the details below to add it as a new launch."}},
                {"type": "input", "block_id": "bl_store",
                 "label": {"type": "plain_text", "text": "Store ID"},
                 "hint": {"type": "plain_text", "text": "Unique key, e.g. prod-shoprite-153"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "prod-retailer-123"}}},
                {"type": "input", "block_id": "bl_nickname",
                 "label": {"type": "plain_text", "text": "Store Nickname"},
                 "hint": {"type": "plain_text", "text": "Ops-facing name, e.g. ShopRite 153"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "initial_value": typed,
                             "placeholder": {"type": "plain_text", "text": "ShopRite 153"}}},
                {"type": "input", "block_id": "bl_ext_id",
                 "label": {"type": "plain_text", "text": "External Store ID"},
                 "hint": {"type": "plain_text", "text": "e.g. 153"},
                 "optional": True,
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "153"}}},
                {"type": "input", "block_id": "bl_retailer",
                 "label": {"type": "plain_text", "text": "Retailer"},
                 "hint": {"type": "plain_text", "text": "e.g. shoprite, sprouts, wakefern"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "shoprite"}}},
                {"type": "input", "block_id": "bl_street",
                 "label": {"type": "plain_text", "text": "Street Address"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "123 Main St"}}},
                {"type": "input", "block_id": "bl_city",
                 "label": {"type": "plain_text", "text": "City"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "Springfield"}}},
                {"type": "input", "block_id": "bl_state",
                 "label": {"type": "plain_text", "text": "State"},
                 "hint": {"type": "plain_text", "text": "Two-letter abbreviation, e.g. NJ"},
                 "element": {"type": "plain_text_input", "action_id": "val", "max_length": 2,
                             "placeholder": {"type": "plain_text", "text": "NJ"}}},
                {"type": "input", "block_id": "bl_zip",
                 "label": {"type": "plain_text", "text": "Zip Code"},
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "07001"}}},
                {"type": "input", "block_id": "bl_phone",
                 "label": {"type": "plain_text", "text": "Phone (optional)"},
                 "optional": True,
                 "element": {"type": "plain_text_input", "action_id": "val",
                             "placeholder": {"type": "plain_text", "text": "555-555-5555"}}},
            ],
        }
    )


@slack.view("rsa_new_launch_submit")
def _submit_new_launch_modal(ack, body, client, view):
    """The 'Add Store' button was clicked in the new-launch modal. Write the new store
    to deployed_stores.csv, then resume the onboarding flow as if the store had been
    in the directory all along."""
    def _val(block_id):
        return (view["state"]["values"].get(block_id, {})
                .get("val", {}).get("value") or "").strip()

    store    = _val("bl_store")
    nickname = _val("bl_nickname")
    ext_id   = _val("bl_ext_id")
    retailer = _val("bl_retailer")
    street   = _val("bl_street")
    city     = _val("bl_city")
    state    = _val("bl_state").upper()
    zip_     = _val("bl_zip")
    phone    = _val("bl_phone")

    errors = {}
    if not store:
        errors["bl_store"] = "Store ID is required."
    if not nickname:
        errors["bl_nickname"] = "Store nickname is required."
    if not retailer:
        errors["bl_retailer"] = "Retailer is required."
    if not street:
        errors["bl_street"] = "Street address is required."
    if not city:
        errors["bl_city"] = "City is required."
    if not state or len(state) != 2:
        errors["bl_state"] = "Enter a two-letter state abbreviation."
    if not zip_:
        errors["bl_zip"] = "Zip code is required."
    if errors:
        ack(response_action="errors", errors=errors)
        return
    ack()

    try:
        rec = store_map.add_to_deployed(store, nickname, ext_id, retailer,
                                        street, city, state, zip_, phone)
    except Exception:
        log.exception("add_to_deployed failed")
        # Fall back: post an error to the user's DM
        user_id = body.get("user", {}).get("id")
        if user_id:
            try:
                client.chat_postMessage(channel=user_id,
                    text=f"⚠️ Could not add *{nickname}* to the deployed stores directory. "
                         "Please check the server logs and try again.")
            except Exception:
                pass
        return

    # Restore the onboarding context and lock in the newly added store.
    try:
        ctx = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        ctx = {}
    groups  = ctx.get("groups") or []
    idx     = ctx.get("resolveIdx")
    trigger = ctx.get("trigger")
    vendor  = sheets._norm_vendor(ctx.get("vendor"))
    user_id   = body.get("user", {}).get("id", "")
    user_name = body.get("user", {}).get("name", user_id)
    channel   = ctx.get("channel") or user_id

    if isinstance(idx, int) and 0 <= idx < len(groups):
        groups[idx]["storeKey"] = rec.get("store") or store
        groups[idx]["store"]    = rec.get("display") or nickname
        groups[idx]["address"]  = rec.get("address") or ""

    msg = _onboard_next(groups, trigger, vendor, user_name)
    notice = f"✅ *{nickname}* added to the deployed stores directory as a new launch.\n"
    if isinstance(msg.get("blocks"), list):
        msg["blocks"].insert(0, {"type": "section",
                                 "text": {"type": "mrkdwn", "text": notice}})
    else:
        msg["text"] = notice + msg.get("text", "")

    try:
        client.chat_postMessage(channel=channel, **msg)
    except Exception:
        log.exception("could not post new-launch confirmation to %s", channel)


# Ack every block action. Plain URL buttons (open the tracker) need nothing more;
# trigger/vendor/state/compliance buttons carry context in their value → advance the flow.
@slack.action(re.compile(".*"))
def _on_action(ack, body, client):
    ack()
    try:
        acts = body.get("actions") or []
        aid = str(acts[0].get("action_id", "")) if acts else ""
        if aid.startswith("rsa_set_vendor"):
            _complete_onboard_with_vendor(body, client)
        elif aid.startswith("rsa_set_trigger"):
            _complete_onboard_from_button(body, client)
        elif aid.startswith("rsa_set_store"):
            _complete_store_from_button(body, client)
        elif aid.startswith("rsa_set_state"):
            _complete_state_from_button(body, client)
        elif aid.startswith("rsa_set_compliance"):
            _complete_compliance_from_button(body, client)
        elif aid == "rsa_new_launch_open":
            _open_new_launch_modal(body, client)
        elif aid == "rsa_home_refresh":
            _publish_home((body.get("user") or {}).get("id", ""), client, force=True)
    except Exception:
        log.exception("action handler error")


# ── App Home (Home tab) ─────────────────────────────────────────────────────────
# Rovi's Home tab: a live RSA dashboard + example commands. Published per-user when
# someone opens the App Home (app_home_opened) and re-published on the Refresh button.
# Requires: App Home → Home Tab toggle ON, and the app_home_opened bot event.
def _home_stat_blocks():
    """Home tab body: carts in progress from the TRUNO tracker + what's scheduled.
    Never raises — a Sheets hiccup degrades to a short notice so the tab still renders."""
    blocks = []

    # ── Carts in progress (TRUNO tracker) ───────────────────────────────────
    tp = sheets.truno_progress_summary(max_list=8)
    if tp.get("error"):
        blocks.append(_section("*🔧 TRUNO — carts in progress*\n"
                               "⚠️ Couldn't load the TRUNO tracker just now — try *🔄 Refresh* in a moment."))
    else:
        blocks.append(_section(
            f"*🔧 TRUNO — carts in progress: {tp.get('inProgressTotal', 0)}*\n"
            f"📅 Scheduled (date set): *{tp.get('scheduled', 0)}*    ·    "
            f"⏳ Awaiting a date: *{tp.get('pending', 0)}*\n"
            f"✅ Completed: {tp.get('completed', 0)}    ·    ✖️ Cancelled: {tp.get('cancelled', 0)}"
        ))
        rows = tp.get("inProgress") or []
        if rows:
            lines = []
            for v in rows:
                cart = f" · cart {v['cart']}" if v.get("cart") else ""
                when = v.get("visit") or "no date yet"
                lines.append(f"• *{v['store']}*{cart} — {v['status']} · {when}")
            if tp.get("moreInProgress"):
                lines.append(f"_…and {tp['moreInProgress']} more_")
            blocks.append(_section("\n".join(lines)))

    # ── What's scheduled (upcoming visits: RSA + TRUNO, soonest first) ───────
    blocks.append({"type": "divider"})
    uv = sheets.upcoming_visits(days_ahead=45)
    if uv.get("error"):
        blocks.append(_section("*📅 Scheduled visits (next 45 days)*\n"
                               "⚠️ Couldn't load scheduled visits just now."))
    else:
        visits = uv.get("visits") or []
        head = f"*📅 Scheduled visits — next 45 days: {uv.get('total', 0)}*"
        if not visits:
            blocks.append(_section(head + "\nNothing scheduled in this window."))
        else:
            lines = []
            for v in visits[:8]:
                cid = v.get("cartId", "")
                cart = f" · {cid}" if cid and "|" not in cid else ""
                vendor = f" ({v['rsaVendor']})" if v.get("rsaVendor") else ""
                lines.append(f"• *{v['visitDate']}* — {v.get('store', '')}{cart}{vendor}")
            extra = uv.get("total", 0) - min(len(visits), 8)
            if extra > 0:
                lines.append(f"_…and {extra} more_")
            blocks.append(_section(head + "\n" + "\n".join(lines)))

    return blocks


def build_home_view():
    """Assemble Rovi's App Home (Home tab) view object."""
    updated = time.strftime("%b %-d, %-I:%M %p")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🛒 Rovi — RSA Operations Agent"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"TRUNO progress & scheduling   ·   updated {updated}"}]},
        _section("Here's what's in progress with TRUNO and what's coming up. I track RSA "
                 "cart work across stores — onboarding, scheduling, results, and compliance. "
                 "DM me or @mention me and ask in plain English."),
        {"type": "divider"},
    ]
    blocks += _home_stat_blocks()
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "action_id": "rsa_home_open_truno", "style": "primary",
             "text": {"type": "plain_text", "text": "🔧 Open TRUNO Tracker"}, "url": sheets.TRUNO_URL},
            {"type": "button", "action_id": "rsa_home_open_tracker",
             "text": {"type": "plain_text", "text": "📊 RSA Tracker"}, "url": sheets.webapp_url()},
            {"type": "button", "action_id": "rsa_home_refresh",
             "text": {"type": "plain_text", "text": "🔄 Refresh"}},
        ],
    })
    blocks += [
        {"type": "divider"},
        _section(
            "*💬 What you can ask me*\n"
            "• *Onboard* — “Onboard cart 12345 at ShopRite Newark, launches”\n"
            "• *Update* — “Cart 12345 passed RSA” · “Schedule RSA for cart 900 on 6/12”\n"
            "• *Query* — “Status of cart 12345” · “Show pending RSA carts in NJ”\n"
            "• *Summary* — “Give me a summary” · “Monthly summary for June”\n"
            "• *Compliance* — “Compliance for ShopRite Newark”"
        ),
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Rovi · RSA Operations Agent"}]},
    ]
    return {"type": "home", "blocks": blocks}


def _publish_home(user_id, client, force=False):
    """Publish/refresh the Home tab for one user. Never raises. force=True busts the
    RSA row cache first so a Refresh click reflects the latest sheet."""
    if not user_id:
        return
    if force:
        for _bust in (sheets._bust_rsa_cache, sheets._bust_truno_cache):
            try:
                _bust()
            except Exception:
                pass
    try:
        client.views_publish(user_id=user_id, view=build_home_view())
        log.info("home tab published for %s", user_id)
    except Exception:
        log.exception("home tab publish failed")


@slack.event("app_home_opened")
def _on_home_opened(event, client):
    log.info("app_home_opened received: user=%s tab=%s", event.get("user"), event.get("tab"))
    # Only the Home tab needs a published view; skip Messages/About opens.
    if event.get("tab") and event.get("tab") != "home":
        return
    _publish_home(event.get("user", ""), client)


# ── Entry point ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("RSA Agent starting — model=%s via %s", AGENT_MODEL, AIGATEWAY_BASE_URL)
    log.info("Sheets backend — RSA Tracker: %s", sheets.RSA_SPREADSHEET_ID)

    # Make sure the result columns exist before anything lands: U = RSA Test Results
    # (W&M pass/fail), V/W = Tech Results + link (calibration readings).
    try:
        log.info("RSA Test Results column: %s", sheets.ensure_results_column())
    except Exception as e:
        log.warning("Could not verify RSA Test Results column (%s) — check sheet sharing.", e)
    try:
        log.info("Tech Results columns: %s", sheets.ensure_tech_results_columns())
    except Exception as e:
        log.warning("Could not verify Tech Results columns (%s) — check sheet sharing.", e)

    # Background: watch the Drive folders → write results to the tracker.
    monitor_channel = os.environ.get("RSA_MONITOR_CHANNEL", "C0ARJQWG7RP")   # all monitor/reminder/alert posts

    def _notify(text):
        if monitor_channel:
            try:
                slack.client.chat_postMessage(channel=monitor_channel, text=text)
            except Exception:
                pass

    notify = _notify if monitor_channel else None
    # Pollers are staggered by 20 s each to avoid a burst of simultaneous Sheets
    # reads on startup that trips the 60-reads/min quota (429 rate-limit errors).
    try:
        import drive_monitor
        drive_monitor.start_poller(notify=notify)               # W&M Scale Test Forms → col U
    except Exception as e:
        log.warning("Drive folder monitor not started (%s) — Slack uploads still work.", e)
    time.sleep(45)
    try:
        import tech_monitor
        tech_monitor.start_poller(notify=notify)                # tech calibration sheets → Tech Results
    except Exception as e:
        log.warning("Tech folder monitor not started (%s) — Slack uploads still work.", e)
    time.sleep(45)
    try:
        import truno_monitor
        truno_monitor.start_poller(notify=notify)               # Truno visit status → RSA Tracker (address-keyed)
    except Exception as e:
        log.warning("Truno monitor not started (%s).", e)
    time.sleep(45)
    try:
        import rsa_reminders
        rsa_reminders.start_poller(notify=notify)               # daily RSA scheduling reminders (replaces RSAAlert; RSA only)
    except Exception as e:
        log.warning("RSA reminder poster not started (%s).", e)
    time.sleep(45)
    try:
        import rsa_normalize
        rsa_normalize.start_poller(notify=notify)               # enforce Overall=Passed / DRI=CSM rules on the tracker
    except Exception as e:
        log.warning("Tracker normalizer not started (%s).", e)
    time.sleep(45)
    try:
        backfill_dates.start_poller(notify=notify)              # fill missing sched/compl dates from TRUNO (every 6h)
    except Exception as e:
        log.warning("Backfill date poller not started (%s).", e)

    SocketModeHandler(slack, SLACK_APP_TOKEN).start()
