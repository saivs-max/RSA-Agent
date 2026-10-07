#!/usr/bin/env python3
"""
RSA test-form parser — reads TRUNO 'Scale Test Form' inspection files (digital PDFs
or scans) via the AI gateway's vision, extracts each cart's Pass/Fail, and writes the
result onto the matching RSA Tracker cart(s).

Used by both the Slack-upload path (user drops a form in chat) and, later, the
Drive-folder monitor — they share extract_test_form() + apply_test_form().
"""

import os
import re
import json
import base64
import logging
from io import BytesIO

from openai import OpenAI, APIConnectionError   # APITimeoutError subclasses APIConnectionError

import rsa_sheets as sheets

log = logging.getLogger("rsa-agent.forms")

AIGATEWAY_BASE_URL = os.environ.get(
    "AIGATEWAY_BASE_URL", "https://aigateway.instacart.tools/proxy/rovi_agent/openai/v1")
AIGATEWAY_API_KEY = os.environ.get("AIGATEWAY_API_KEY", "gateway-no-token-needed")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
# Vision parses are slower than a chat turn, so allow more time; still bounded + retried
# so a flaky gateway can't hang the form/Drive poller indefinitely.
AIGATEWAY_TIMEOUT     = float(os.environ.get("AIGATEWAY_VISION_TIMEOUT", "90"))
AIGATEWAY_MAX_RETRIES = int(os.environ.get("AIGATEWAY_MAX_RETRIES", "3"))

_llm = OpenAI(base_url=AIGATEWAY_BASE_URL, api_key=AIGATEWAY_API_KEY,
              timeout=AIGATEWAY_TIMEOUT, max_retries=AIGATEWAY_MAX_RETRIES)

_EXTRACT_PROMPT = (
    "You read TRUNO 'Scale Test Form' weights-and-measures inspection forms. "
    "Extract the store and EACH cart's pass/fail. Return ONLY JSON, no prose:\n"
    '{"retailer": str, "store": str, "serviceDate": str, "trunoCall": str, '
    '"carts": [{"cart": str, "serial": str, "passed": true|false|null, "comments": str}]}\n'
    "\"passed\" comes from the 'Passed? Yes/No' checkbox: Yes=true, No=false, unclear=null. "
    "\"store\" should be the retailer + store number (e.g. 'Shoprite #0116'). "
    "List every cart block on every page."
)


# ── PDF / image → PNG page images ───────────────────────────────────────────────
def _to_pngs(file_bytes, filename, max_pages=10):
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import fitz  # PyMuPDF
        out = []
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc[:max_pages]:
            out.append(page.get_pixmap(dpi=150).tobytes("png"))
        doc.close()
        return out
    return [file_bytes]   # already an image (png/jpg/etc.)


def _shrink(img_bytes, max_bytes=4_500_000, max_dim=2200):
    """Re-encode an image as JPEG kept under Bedrock's 5 MB-per-image limit."""
    try:
        from PIL import Image
    except Exception:
        return img_bytes
    try:
        im = Image.open(BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return img_bytes
    if max(im.size) > max_dim:
        im.thumbnail((max_dim, max_dim))
    for q in (85, 75, 65, 55, 45):
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=q)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    im.thumbnail((1600, 1600))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=55)
    return buf.getvalue()


def _b64_data_url(jpeg_bytes):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()


# ── Parse a form via the gateway (vision) ───────────────────────────────────────
def extract_test_form(file_bytes, filename):
    try:
        pages = _to_pngs(file_bytes, filename)
    except ImportError:
        return {"error": "PDF support isn't installed — run: pip install pymupdf"}
    except Exception as e:
        return {"error": f"could not read the file ({type(e).__name__}): {e}"}
    if not pages:
        return {"error": "could not render any pages from the file"}
    content = [{"type": "text", "text": _EXTRACT_PROMPT}]
    for raw in pages:
        content.append({"type": "image_url", "image_url": {"url": _b64_data_url(_shrink(raw))}})
    try:
        resp = _llm.chat.completions.create(
            model=AGENT_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": content}])
        text = (resp.choices[0].message.content or "{}").strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.I)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        return json.loads(text)
    except APIConnectionError as e:
        log.warning("vision gateway unreachable (%s) after retries", type(e).__name__)
        return {"error": "couldn't reach the assistant backend to read the form "
                         "(network/gateway). It'll retry on the next pass — or re-upload."}
    except Exception as e:
        return {"error": f"vision parse failed ({type(e).__name__}): {e}"}


# ── Apply parsed results to the RSA Tracker ─────────────────────────────────────
def apply_test_form(parsed, link=None, store_hint=None):
    if parsed.get("error"):
        return {"error": parsed["error"]}
    # Prefer the caller's store (the Truno store the file was matched to) over the
    # store name printed on the form — the form often shows the retailer's own
    # number (e.g. "Sprouts Farmers Market #180") which won't match the tracker.
    raw_store = store_hint or parsed.get("store") or parsed.get("retailer") or ""
    # Build a list of candidate store names to try, mirroring _candidate_store_nums
    # in truno_monitor.  The reconciler may have used the INTERNAL caper number
    # (e.g. "Sprouts 15" from prod-sprouts-15) instead of the external/ops number
    # ("Sprouts 886") when the Truno ISC-code column was blank at the time it ran.
    # Trying both names here keeps the RSA path consistent with the Truno path.
    store_candidates = []
    try:
        import store_map as _sm
        resolved = _sm.resolve(raw_store) or raw_store
        store_candidates.append(resolved)
        # Look up the deployed-stores directory — same resolution the Truno monitor
        # uses via match_store(q) inside _candidate_store_nums.
        m = _sm.match_store(resolved)
        if m.get("status") == "matched":
            rec = m["store"]
            display = rec.get("display")
            if display and display not in store_candidates:
                store_candidates.append(display)
            # Derive the "internal number" variant: prod-sprouts-15 → "Sprouts 15".
            # The reconciler falls back to this when the ISC code column is blank.
            iid = rec.get("internal_id") or ""
            im = re.search(r"(\d+)$", iid)
            if im:
                int_num = im.group(1).lstrip("0") or im.group(1)
                banner = _sm.banner_of(rec.get("retailer", ""))
                if banner and int_num:
                    int_name = f"{banner} {int_num}"
                    if int_name not in store_candidates:
                        store_candidates.append(int_name)
    except Exception:
        pass
    if not store_candidates:
        store_candidates = [raw_store]
    store = store_candidates[0]   # canonical name for the result summary

    carts = parsed.get("carts") or []
    if not store or not carts:
        return {"error": "form parsed but no store / carts found", "parsed": parsed}
    try:
        rows = sheets._rsa_data()
    except Exception as e:
        return {"error": f"tracker read failed: {e}"}
    results = []
    for c in carts:
        num = str(c.get("cart") or c.get("serial") or "").strip()
        if not num:
            continue
        passed = c.get("passed")
        comments = (c.get("comments") or "").strip()
        result_text = ("Passed" if passed is True else "Failed" if passed is False else "Result unclear")
        if comments:
            result_text += f" — {comments}"
        # Try each candidate name in order (external number first, internal as fallback).
        cid = None
        matched_store = store
        for candidate in store_candidates:
            cid = sheets.match_cart(candidate, num, rows)
            if cid:
                matched_store = candidate
                break
        if not cid:
            results.append({"cart": num, "cartId": None, "passed": passed, "ok": False,
                            "detail": "no matching cart in the RSA Tracker"})
            continue
        res = sheets.set_test_result(cid, store=matched_store, result_text=result_text, passed=passed, link=link)
        results.append({"cart": num, "cartId": cid, "passed": passed,
                        "ok": not res.get("error"), "detail": res.get("error") or ", ".join(res.get("updated", []))})
    return {"store": store, "serviceDate": parsed.get("serviceDate"),
            "trunoCall": parsed.get("trunoCall"), "results": results}


def process_upload(file_bytes, filename, link=None, store_hint=None):
    """Full path for an uploaded form: parse via vision, write results to carts.
    store_hint (the Truno store the file was matched to) takes precedence over the
    store name printed on the form for cart matching."""
    parsed = extract_test_form(file_bytes, filename)
    return apply_test_form(parsed, link=link, store_hint=store_hint)
