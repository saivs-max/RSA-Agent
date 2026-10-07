# RSA Agent — test suite

Two layers. Run both from the **project folder** (`~/Documents/Claude/Projects/RSA Agent`).

```bash
cd ~/Documents/Claude/Projects/RSA\ Agent
```

---

## 1. `test_offline.py` — logic, no secrets, no network, no writes

Replaces Google Sheets / Drive / Slack / the AI gateway with in-memory fakes, then
exercises the whole app. Safe to run anytime, as often as you like.

```bash
python3 tests/test_offline.py
```

Expect `RESULT: 81 passed, 0 failed` and exit code 0. A non-zero exit lists the
failing checks. Run this after **any** code change — it's the fast regression net.

What it proves: cart lookups, summary counts, filtered lists, status updates (with
note-append + Last-Updated stamping), add/duplicate guard, the new RSA-results path
(Pass → Completed RSA + Pending W&M + col U + link; Fail → Failed RSA; unclear →
text only), the `RSA Test Results` header creation, serial match-vs-derive (incl.
leading zeros and `3A` suffixes), compliance (pre/post only, address match, the
"Confirm Locally" flag, unknown-state warning), multi-cart onboarding, form
apply (matched/failed/untracked carts), the Drive monitor (only `Forms sent = Yes`
stores ingest, the year-token match fix, idempotency on re-run, missing-form alert),
the Slack Block Kit formatters, and full `handle_message` dispatch with a stubbed LLM.

---

## 1b. `test_tech_offline.py` — tech-results pipeline, no secrets / network / writes

Same fake harness, focused on the calibration path.

```bash
python3 tests/test_tech_offline.py
```

Expect `RESULT: 63 passed, 0 failed`. What it proves: the `Tech Results` / `Tech
Results Link` columns (auto-create + resolve-by-header, idempotent), `set_tech_result`
(readings → col V, link → col W, Last-Updated stamped, RSA status untouched), the
calibration summary formatting + `apply_tech_form` matching (matched/comment/untracked),
gap tracking (`results_completeness` + `carts_missing_results` for tech/test/both/any),
the tech Drive monitor (no Truno gate, filename gating, idempotency, self-heal, backfill,
gap report), and the Slack bot's keyword routing (`tech`/`calibration` → tech path; no
keyword → test path) plus the `missingResults` dispatch.

---

## 2. `check_live.py` — real Sheets / Drive / gateway

Uses your real `.env` + `sa-key.json` (must be in the project folder). Install deps
first if you haven't: `pip install -r requirements.txt`.

```bash
python3 tests/check_live.py connect   # read-only — prove the stack is wired
python3 tests/check_live.py parse     # read-only — parse one real form via vision
python3 tests/check_live.py all       # connect + parse (still no writes)
python3 tests/check_live.py write     # adds a TEMP cart, verifies, then DELETES it
```

**`connect`** (read-only) checks, in order: the service account can open all three
sheets; the col U `RSA Test Results` header exists; which stores are currently
`Forms sent = Yes`; the Drive folder is reachable and lists files; the AI gateway
answers a tiny prompt. Each line is ✓ or ✗ with a one-line fix hint on failure.

**`parse`** (read-only) downloads the newest PDF/image in the folder and runs it
through the gateway vision parser, printing the extracted JSON. It does **not** write
results to the tracker — use it to eyeball that pass/fail extraction is correct.

**`write`** is the only command that touches a sheet. It adds one obviously-fake cart
(`P_ZZTEST_AGENT_M3_999` at `ZZ TEST — delete me`), writes a fake pass result, reads
it back to confirm Completed RSA / Pending W&M / col U / link all landed, then deletes
the row in a `finally` block. It never touches the Truno sheet. If you ever see a
leftover `ZZ TEST` row, delete it manually — but cleanup runs even on failure.

---

### Suggested order when validating a deployment

1. `python3 tests/test_offline.py` → all green.
2. `python3 tests/check_live.py connect` → all ✓ (fix any sharing/API issues it flags).
3. `python3 tests/check_live.py parse` → confirm a real form extracts correctly.
4. `python3 tests/check_live.py write` → confirm a result actually lands and cleans up.

Then start the bot and post a message / drop a form to confirm the Slack path.
