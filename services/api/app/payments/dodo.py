"""Dodo Payments checkout. With no DODO_PAYMENTS_API_KEY and DEV_DODO=true, a mock session is returned and
completed through routers/dev_dodo.py, which sends signed webhooks down the real verification path."""

import base64
import secrets

from ..config import get_settings

MOCK_SESSIONS: dict[str, dict] = {}
DEV_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"creatorloop-dev-webhook-secret!!").decode()


def webhook_secret() -> str | None:
    st = get_settings()
    return st.dodo_payments_webhook_secret or (DEV_WEBHOOK_SECRET if st.dev_dodo else None)


async def create_checkout(*, product_id: str | None, metadata: dict[str, str], return_url: str,
                          name: str, email: str | None) -> dict:
    st = get_settings()
    if not st.dodo_payments_api_key:
        if not st.dev_dodo:
            raise RuntimeError("Dodo is not configured")
        sid = "cks_" + secrets.token_hex(8)
        MOCK_SESSIONS[sid] = {"metadata": metadata, "email": email, "kind": metadata["kind"]}
        return {"checkout_url": f"{st.public_base_url}/dev/dodo/pay/{sid}", "session_id": sid}
    from dodopayments import AsyncDodoPayments

    client = AsyncDodoPayments(bearer_token=st.dodo_payments_api_key, environment=st.dodo_environment)
    customer = {"name": name, **({"email": email} if email else {})}
    session = await client.checkout_sessions.create(
        product_cart=[{"product_id": product_id, "quantity": 1}], customer=customer,
        return_url=return_url, metadata=metadata)
    return {"checkout_url": session.checkout_url, "session_id": session.session_id}
