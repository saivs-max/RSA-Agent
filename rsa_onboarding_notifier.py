"""
RSA Cart Onboarding Notifier
============================
Triggered by the RSA Webapp when a cart is onboarded.
Sends parallel Slack messages to ops managers for the relevant state,
reminding them of repair & calibration compliance deadlines.

Setup:
  pip install fastapi uvicorn slack-sdk

Run:
  uvicorn rsa_onboarding_notifier:app --host 0.0.0.0 --port 8000

RSA Webapp should POST to: http://<host>:8000/cart-onboarded
"""

import concurrent.futures
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ─────────────────────────────────────────────
# SLACK CONFIG
# ─────────────────────────────────────────────
SLACK_BOT_TOKEN = "xoxb-REPLACE-WITH-YOUR-BOT-TOKEN"
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# ─────────────────────────────────────────────
# OPS MANAGER CHANNELS  ← fill in when ready
# Value can be a channel ID (#channel-name) or
# a user ID (@handle) for a direct message.
# ─────────────────────────────────────────────
OPS_MANAGER_CHANNELS: dict[str, str] = {
    # 24-hour states
    "CA": "PLACEHOLDER_CA",
    "ID": "PLACEHOLDER_ID",
    "KY": "PLACEHOLDER_KY",
    "MO": "PLACEHOLDER_MO",
    "NE": "PLACEHOLDER_NE",
    "NY": "PLACEHOLDER_NY",
    "OR": "PLACEHOLDER_OR",
    "SC": "PLACEHOLDER_SC",
    "UT": "PLACEHOLDER_UT",
    # 48-hour states
    "CT": "PLACEHOLDER_CT",
    "PA": "PLACEHOLDER_PA",
    "VT": "PLACEHOLDER_VT",
    # ~2 business days
    "IN": "PLACEHOLDER_IN",
    # 3–5 day states
    "IL": "PLACEHOLDER_IL",
    "MS": "PLACEHOLDER_MS",
    "MT": "PLACEHOLDER_MT",
    "NV": "PLACEHOLDER_NV",
    "NM": "PLACEHOLDER_NM",
    "OK": "PLACEHOLDER_OK",
    "VA": "PLACEHOLDER_VA",
    # 7-day states
    "AZ": "PLACEHOLDER_AZ",
    "ND": "PLACEHOLDER_ND",
    "OH": "PLACEHOLDER_OH",
    "SD": "PLACEHOLDER_SD",
    "WA": "PLACEHOLDER_WA",
}

# ─────────────────────────────────────────────
# COMPLIANCE TIMEFRAMES BY STATE
# ─────────────────────────────────────────────
COMPLIANCE_TIMEFRAMES: dict[str, str] = {
    # Within 24 hours
    "CA": "within 24 hours",
    "ID": "within 24 hours",
    "KY": "within 24 hours",
    "MO": "within 24 hours",
    "NE": "within 24 hours",
    "NY": "within 24 hours",
    "OR": "within 24 hours",
    "SC": "within 24 hours",
    "UT": "within 24 hours",
    # Within 48 hours
    "CT": "within 48 hours",
    "PA": "within 48 hours",
    "VT": "within 48 hours",
    # ~2 business days
    "IN": "within ~2 business days (varies by locality)",
    # 3–5 days
    "IL": "within 5 days",
    "MS": "within 3 days",
    "MT": "within 5 working days",
    "NV": "oral notice within 24 hours; written report within 5 days",
    "NM": "within 5 calendar days",
    "OK": "within 5 calendar days",
    "VA": "within 5 business days",
    # 7 days
    "AZ": "within 7 days",
    "ND": "within 7 working days",
    "OH": "within 7 days",
    "SD": "within 7 days",
    "WA": "within ~7 days",
}

# ─────────────────────────────────────────────
# INBOUND PAYLOAD SCHEMA
# ─────────────────────────────────────────────
class CartOnboardedPayload(BaseModel):
    cart_id: str
    state: str          # 2-letter state code, e.g. "CA"
    location: str = ""  # optional — store name / address


# ─────────────────────────────────────────────
# SLACK SEND HELPER
# ─────────────────────────────────────────────
def send_slack_message(channel: str, text: str) -> dict:
    try:
        response = slack_client.chat_postMessage(channel=channel, text=text)
        logger.info(f"Message sent to {channel}")
        return {"channel": channel, "ok": True}
    except SlackApiError as e:
        logger.error(f"Slack error for {channel}: {e.response['error']}")
        return {"channel": channel, "ok": False, "error": e.response["error"]}


# ─────────────────────────────────────────────
# WEBHOOK ENDPOINT
# ─────────────────────────────────────────────
@app.post("/cart-onboarded")
def cart_onboarded(payload: CartOnboardedPayload):
    state = payload.state.upper()

    if state not in COMPLIANCE_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"No compliance rule found for state: {state}")

    channel = OPS_MANAGER_CHANNELS.get(state)
    if not channel or channel.startswith("PLACEHOLDER"):
        raise HTTPException(status_code=500, detail=f"Ops manager channel not configured for {state}")

    timeframe = COMPLIANCE_TIMEFRAMES[state]
    location_info = f" ({payload.location})" if payload.location else ""

    message = (
        f"🛒 Cart onboarded in {state}{location_info} — Cart ID: `{payload.cart_id}`.\n"
        f"Per compliance requirements, repair and calibration must be completed *{timeframe}*.\n"
        f"Please initiate the process."
    )

    # Send in parallel (useful when you want to notify multiple channels per state)
    channels_to_notify = [channel] if isinstance(channel, str) else channel

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(send_slack_message, ch, message): ch for ch in channels_to_notify}
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    failed = [r for r in results if not r["ok"]]
    if failed:
        logger.warning(f"Some notifications failed: {failed}")

    return {
        "cart_id": payload.cart_id,
        "state": state,
        "timeframe": timeframe,
        "notifications_sent": len(results) - len(failed),
        "notifications_failed": len(failed),
        "details": results,
    }


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}
