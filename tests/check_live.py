#!/usr/bin/env python3
"""
RSA Agent — LIVE checks against your real Google Sheets / Drive / AI gateway.

Uses your real credentials (.env + sa-key.json in the project folder). Run from the
project folder so those files are found:

    python3 tests/check_live.py connect     # read-only: prove everything is wired
    python3 tests/check_live.py parse        # read-only: parse one real form via vision
    python3 tests/check_live.py write        # writes a TEMP cart, then DELETES it
    python3 tests/check_live.py all          # connect + parse (no write)

'connect' and 'parse' never modify your trackers. 'write' adds one obviously-fake
cart (P_ZZTEST_AGENT_M3_999 at "ZZ TEST — delete me"), confirms the result lands,
then removes the row in a finally block. It never touches the Truno sheet.
"""
import os
import sys
import argparse
import warnings

warnings.filterwarnings("ignore")   # quiet the google/urllib3 Python-3.9 EOL notices

# Find the app modules + .env + sa-key.json whether this file sits in
# <project>/tests/ or directly alongside rsa_agent.py (e.g. a flat run folder).
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

OK, BAD = "  \033[92m✓\033[0m", "  \033[91m✗\033[0m"
TEST_SERIAL = "P_ZZTEST_AGENT_M3_999"
TEST_STORE = "ZZ TEST — delete me"


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass
    # rsa_sheets resolves sa-key.json relative to CWD; default to the project folder.
    os.environ.setdefault("GOOGLE_SA_KEY_FILE", os.path.join(ROOT, "sa-key.json"))


def _sa_email():
    import json
    try:
        with open(os.environ.get("GOOGLE_SA_KEY_FILE", os.path.join(ROOT, "sa-key.json"))) as f:
            return json.load(f).get("client_email", "(client_email in sa-key.json)")
    except Exception:
        return "(the client_email in sa-key.json)"


def _need(import_ok=True):
    try:
        import rsa_sheets  # noqa
        return None
    except Exception as e:
        return (f"Could not import the app — install deps first:\n"
                f"    pip install -r requirements.txt\n  ({type(e).__name__}: {e})")


# ── connect: read-only proof the whole stack is reachable ────────────────────────
def cmd_connect():
    import rsa_sheets as S
    print("\n=== 1) Service account → Google Sheets (read-only) ===")
    sheets = [("RSA Tracker", S.RSA_SPREADSHEET_ID, S.RSA_SHEET),
              ("TRUNO Tracker", S.TRUNO_SPREADSHEET_ID, S.TRUNO_SHEET),
              ("Compliance Guide", S.COMPLIANCE_SPREADSHEET_ID, S.COMPLIANCE_SHEET)]
    ok_all = True
    for label, sid, name in sheets:
        try:
            vals = S._ws(sid, name).get_all_values()
            print(f"{OK} {label}: opened '{name}', {len(vals)} rows")
        except Exception as e:
            ok_all = False
            print(f"{BAD} {label}: {type(e).__name__}: {e}")
            print(f"      → share this sheet with the service-account email (Viewer/Editor).")

    print("\n=== 2) RSA Test Results column (U) header ===")
    try:
        hdr = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET).cell(S.RSA_HEADER_ROW, S.C_RSA_RESULTS).value
        if hdr:
            print(f"{OK} col U header present: '{hdr}'")
        else:
            print(f"{BAD} col U header is blank — start the bot once (it auto-creates it) "
                  f"or run ensure_results_column().")
    except Exception as e:
        print(f"{BAD} could not read col U: {e}")

    print("\n=== 3) Truno 'Forms sent = Yes' rows (the ingest trigger) ===")
    try:
        rows = S.truno_confirmed_rows()
        print(f"{OK} {len(rows)} store(s) currently marked Forms sent = Yes")
        for r in rows[:8]:
            print(f"      • {r.get('store')}  (cart {r.get('cart')}, visit {r.get('visit') or '—'})")
    except Exception as e:
        print(f"{BAD} truno_confirmed_rows failed: {e}")

    print("\n=== 4) Drive folder (read-only) ===")
    try:
        import drive_monitor as DM
        # A folder the SA can't see returns an EMPTY list (no error), so confirm
        # visibility explicitly before trusting a 0-file count.
        visible = True
        try:
            meta = DM._service().files().get(
                fileId=DM.RSA_FORMS_FOLDER_ID, fields="id,name", supportsAllDrives=True).execute()
            print(f"{OK} folder visible to the service account: '{meta.get('name')}'")
        except Exception as e:
            visible = False
            print(f"{BAD} the service account CANNOT see this folder (it isn't shared with it): {type(e).__name__}")
            print(f"      → in Drive, share the folder with:  {_sa_email()}  (Viewer)")
        files = DM._list_folder_files()
        print(f"      lists {len(files)} PDF/image file(s)")
        for f in files[:10]:
            print(f"        • {f['name']}")
        if not files and visible:
            print("      (folder is shared but currently has no PDF/image files)")
        elif not files and not visible:
            print("      (0 because the folder isn't shared with the SA — fix the sharing above, then re-run)")
    except Exception as e:
        print(f"{BAD} Drive failed: {type(e).__name__}: {e}")
        print("      → enable the Drive API, and share the folder with the service account (Viewer).")

    print("\n=== 5) AI gateway (tiny completion) ===")
    try:
        from openai import OpenAI
        base = os.environ.get("AIGATEWAY_BASE_URL", "https://aigateway.instacart.tools/proxy/rovi_agent/openai/v1")
        key = os.environ.get("AIGATEWAY_API_KEY", "gateway-no-token-needed")
        model = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
        r = OpenAI(base_url=base, api_key=key).chat.completions.create(
            model=model, max_tokens=5, messages=[{"role": "user", "content": "Reply with the single word: OK"}])
        print(f"{OK} gateway responded: {r.choices[0].message.content.strip()!r}  (model {model})")
    except Exception as e:
        print(f"{BAD} gateway call failed: {type(e).__name__}: {e}")
        print("      → check AIGATEWAY_BASE_URL / network access from this machine.")
    print("\nDone (read-only).")


# ── parse: run one real form through the vision parser (read-only) ───────────────
def cmd_parse():
    import drive_monitor as DM
    import test_forms as TF
    import json
    print("\n=== Parse one real form from the Drive folder (read-only) ===")
    try:
        files = DM._list_folder_files()
    except Exception as e:
        print(f"{BAD} could not list the folder: {e}")
        return
    if not files:
        print("  (no PDF/image files in the folder to parse)")
        return
    f = files[0]                       # newest
    print(f"  parsing newest file: {f['name']}")
    try:
        data = DM._download(f["id"])
        parsed = TF.extract_test_form(data, f["name"])
        print(json.dumps(parsed, indent=2)[:2000])
        if parsed.get("error"):
            print(f"{BAD} parser returned an error (see above)")
        else:
            n = len(parsed.get("carts", []))
            print(f"{OK} parsed store={parsed.get('store')!r}, {n} cart(s)  (NOT written to the tracker)")
    except Exception as e:
        print(f"{BAD} parse failed: {type(e).__name__}: {e}")


# ── write: temp cart → pass result → verify → delete ─────────────────────────────
def _delete_test_row(S):
    ws = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
    vals = ws.get_all_values()
    for i in range(len(vals) - 1, -1, -1):          # bottom-up; only our exact serial
        if (vals[i][0] if vals[i] else "").strip() == TEST_SERIAL:
            ws.delete_rows(i + 1)
            return i + 1
    return None


def cmd_write():
    import rsa_sheets as S
    print("\n=== Controlled write test (adds a TEMP cart, then deletes it) ===")
    print(f"  using throwaway serial {TEST_SERIAL} at '{TEST_STORE}'")
    created = False
    try:
        add = S.add_cart(TEST_SERIAL, TEST_STORE, state="NJ", trigger="HW Ops Inspection",
                         notes="temporary validation row — safe to delete")
        if add.get("error"):
            print(f"{BAD} add_cart: {add['error']}")
            return
        created = True
        print(f"{OK} temp cart added at row {add.get('row')}")

        res = S.set_test_result(TEST_SERIAL, store=TEST_STORE,
                                result_text="Passed — validation run", passed=True,
                                link="https://example.com/validation")
        if res.get("error"):
            print(f"{BAD} set_test_result: {res['error']}")
            return
        print(f"{OK} set_test_result wrote: {', '.join(res.get('updated', []))}")

        q = S.query_cart(TEST_SERIAL)
        ws = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
        vals = ws.get_all_values()
        urow = next((r for r in vals if (r[0] if r else "").strip() == TEST_SERIAL), [])
        ucol = urow[S.C_RSA_RESULTS - 1] if len(urow) >= S.C_RSA_RESULTS else ""
        tcol = urow[S.C_TECH_LINK - 1] if len(urow) >= S.C_TECH_LINK else ""

        def show(label, cond, got):
            print(f"{OK if cond else BAD} {label}: {got!r}")

        show("RSA Status = Completed RSA", q.get("rsaStatus") == "Completed RSA", q.get("rsaStatus"))
        show("Overall = Pending W&M", q.get("overallStatus") == "Pending W&M", q.get("overallStatus"))
        show("col U has the result text", "Passed" in (ucol or ""), ucol)
        show("report link stored", "example.com" in (tcol or ""), tcol)
    finally:
        if created:
            row = _delete_test_row(S)
            print(f"{OK} cleanup: deleted temp row {row}" if row else f"{BAD} cleanup: temp row not found — check the sheet for {TEST_SERIAL}")
    print("\nDone (write + cleanup).")


def main():
    ap = argparse.ArgumentParser(description="RSA Agent live checks")
    ap.add_argument("cmd", choices=["connect", "parse", "write", "all"], nargs="?", default="connect")
    args = ap.parse_args()

    _load_env()
    err = _need()
    if err:
        print(err); sys.exit(2)

    if args.cmd in ("connect", "all"):
        cmd_connect()
    if args.cmd in ("parse", "all"):
        cmd_parse()
    if args.cmd == "write":
        cmd_write()


if __name__ == "__main__":
    main()
