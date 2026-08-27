# app/core/webhook.py
import uuid, datetime, httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import os

PARTNER_WEBHOOK_URL = os.getenv("PARTNER_WEBHOOK_URL")
PARTNER_WEBHOOK_TOKEN = os.getenv("PARTNER_WEBHOOK_TOKEN")

def kst_now_iso():
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).isoformat(timespec="seconds")


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(min=2, max=20),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
)
def send_partner_webhook_sync(created_ids, note="stt.completed", *, created_at_iso=None):
    payload = {
        "createdCount": len(created_ids),
        "count": len(created_ids),
        "created_at": created_at_iso or kst_now_iso(),
        "created_ids": created_ids,
        "note": note,
    }
    headers = {
        "X-Webhook-Token": PARTNER_WEBHOOK_TOKEN,
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    with httpx.Client(timeout=10) as client:
        r = client.post(PARTNER_WEBHOOK_URL, headers=headers, json=payload)
        r.raise_for_status()