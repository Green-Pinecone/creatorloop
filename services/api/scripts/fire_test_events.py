"""Send one of every outbound event to the configured viaSocket flows (or the local sink).

    cd services/api && .venv/Scripts/python scripts/fire_test_events.py

Uses the same emitter as production, so signing, URLs and payload shapes are exactly what flows will see.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.automations.events import FLOW_FIELDS, emit  # noqa: E402
from app.config import get_settings  # noqa: E402

SAMPLES = {
    "payment.succeeded": {"payment_id": "pay_test", "kind": "supporter_pass", "creator_id": "c-1",
                          "amount_minor": 14900, "currency": "INR"},
    "payment.failed": {"payment_id": "pay_test_failed"},
    "subscription.at_risk": {"subscription_id": "sub_test", "account_id": "a-1"},
    "fraud.case_opened": {"case_id": "case-1", "kind": "collusion_ring", "severity": 5, "size": 8},
    "fraud.case_recommended": {"case_id": "case-1", "recommendation": "freeze"},
    "dispute.opened": {"payment_id": "pay_test"},
    "ops.alert": {"title": "test alert", "detail": "from fire_test_events.py", "severity": 2},
    "ledger.entry": {"txn_id": "t-1", "ref": "pay_test", "account_type": "platform_revenue",
                     "amount_minor": 4470, "currency": "INR", "held": False},
    "digest.creator": {"week": "2026-W44", "handle": "alice", "display_name": "Alice", "new_supporters": 3,
                       "tip_total_wei": "0", "tip_count": 0, "top_post_id": 1, "top_post_reactions": 12},
}


async def main() -> None:
    st = get_settings()
    mode = "local sink" if st.viasocket_local_sink else "live flows"
    print(f"firing {len(FLOW_FIELDS)} events to {mode}")
    for event in FLOW_FIELDS:
        try:
            await emit({"event": event, **SAMPLES[event]})
            print(f"  ok    {event}")
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            print(f"  FAIL  {event}: {e}")


asyncio.run(main())
