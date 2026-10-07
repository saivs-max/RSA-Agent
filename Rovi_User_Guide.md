# Rovi — Your RSA Assistant in Slack

Rovi handles RSA cart work for you, right inside Slack. Instead of opening
spreadsheets, you tell Rovi what you need in plain English. It updates the RSA
Tracker, the TRUNO tracker, and the Compliance Guide for you — then confirms what
it did.

## How to talk to Rovi

Two ways:

- **Direct message** Rovi — find it under Apps in your Slack sidebar, or
- **@mention** Rovi in a channel it's in — e.g. `@Rovi give me a summary`.

Write naturally. There are no special commands to memorize. Rovi works out what you
mean and asks if anything is unclear.

## What you can ask

**Onboard a new cart** — tell Rovi the cart, the store, and the reason for the visit:

- "Onboard cart 12345 at ShopRite Newark, launches"
- "Add carts 12, 13, 14 at Stop & Shop Edison for cart replacement"

Rovi checks the store against the deployed-store list, files it, and (for NJ) asks
which vendor — TRUNO or Winter Scales.

**Update a status or schedule a visit:**

- "Cart 12345 passed RSA"
- "Mark cart 900 failed RSA"
- "Schedule RSA for cart 12345 on 6/12"

**Check a cart or pull a list:**

- "What's the status of cart 12345?"
- "Show pending RSA carts in NJ"
- "Which carts are stale for Sai?"

**Get a summary:**

- "Give me a summary" — overall counts and what needs attention
- "Monthly summary for June" — visits completed and scheduled that month

**Look up compliance:**

- "Compliance for ShopRite Newark" — the W&M/RSA directive for that store

## The Home tab

Open Rovi and click the **Home** tab for an at-a-glance view:

- **TRUNO carts in progress** — how many are scheduled (date set) versus still
  awaiting a date, plus completed and cancelled counts, and a short list of the
  next carts.
- **Scheduled visits** — everything booked in the next 45 days, soonest first.

Use **🔄 Refresh** to pull the latest, and the tracker buttons to jump straight to
the sheets.

## Tips

- **Be specific.** Include the cart number and store name so Rovi acts on the right
  row.
- **Rovi confirms every change.** Read the reply to be sure it did what you expected.
- **If Rovi asks a question** (which store? which vendor?), just answer in the thread.
- **Casual is fine.** "did cart 900 pass?" works as well as a formal request.

## Good to know

Rovi reads and writes the same trackers your team already uses, so its answers
reflect the live sheets. It also runs quietly in the background — pulling in
scale-test and calibration results, syncing TRUNO visit updates, and posting daily
scheduling reminders — so a lot stays current without anyone asking.

If something looks off, flag it in your RSA ops channel so it can be checked.
