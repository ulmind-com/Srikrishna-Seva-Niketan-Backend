"""Web Push Service (RFC 8292 / VAPID).

Dispatches standards-compliant Web Push notifications to field staff (nurses)
and admin devices using VAPID authentication and payload encryption.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Dict, List, Optional

from pywebpush import WebPushException, webpush

from app.config import settings
from app.core.logging import get_logger
from app.models.push_subscription import PushSubscription

logger = get_logger(__name__)


class WebPushService:
    """Manages push subscriptions and dispatches browser push notifications."""

    def __init__(self) -> None:
        pass

    @property
    def public_key(self) -> str:
        """Return the VAPID public key (url-safe base64 string)."""
        return settings.VAPID_PUBLIC_KEY

    @property
    def private_key(self) -> str:
        """Return the VAPID private key (PEM format string)."""
        return settings.VAPID_PRIVATE_KEY

    @property
    def claims(self) -> Dict[str, str]:
        """Return VAPID claims dictionary."""
        email = settings.VAPID_CLAIM_EMAIL or "admin@shrikrishnanursing.com"
        return {"sub": f"mailto:{email}"}

    async def register_subscription(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        *,
        employee_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PushSubscription:
        """Create or update a browser push subscription."""
        now = dt.datetime.now(dt.timezone.utc)
        sub = await PushSubscription.find_one(PushSubscription.endpoint == endpoint)
        if sub:
            sub.p256dh = p256dh
            sub.auth = auth
            sub.is_active = True
            sub.failure_count = 0
            if employee_id is not None:
                sub.employee_id = employee_id
            if user_id is not None:
                sub.user_id = user_id
            if user_agent is not None:
                sub.user_agent = user_agent
            sub.touch()
            await sub.save()
            logger.info("Updated existing push subscription", extra={"employee_id": employee_id})
            return sub

        sub = PushSubscription(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            employee_id=employee_id,
            user_id=user_id,
            user_agent=user_agent,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        await sub.insert()
        logger.info("Registered new push subscription", extra={"employee_id": employee_id})
        return sub

    async def unregister_subscription(
        self,
        endpoint: str,
        employee_id: Optional[str] = None,
    ) -> bool:
        """Deactivate or remove a push subscription."""
        query = {"endpoint": endpoint}
        if employee_id:
            query["employee_id"] = employee_id
        sub = await PushSubscription.find_one(query)
        if sub:
            sub.is_active = False
            sub.touch()
            await sub.save()
            return True
        return False

    async def count_active_staff_devices(self) -> int:
        """Count how many active nurse devices are registered."""
        return await PushSubscription.find(
            {"employee_id": {"$ne": None}, "is_active": True}
        ).count()

    def _sync_send(self, subscription_info: dict, payload_str: str) -> None:
        """Synchronously execute webpush call."""
        webpush(
            subscription_info=subscription_info,
            data=payload_str,
            vapid_private_key=self.private_key,
            vapid_claims=self.claims,
            ttl=86400,
        )

    async def send_to_subscription(
        self,
        sub: PushSubscription,
        payload: Dict[str, Any],
    ) -> bool:
        """Deliver a push payload to a single subscription."""
        if not sub.is_active or not self.private_key or not self.public_key:
            return False

        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.p256dh,
                "auth": sub.auth,
            },
        }
        payload_str = json.dumps(payload)

        try:
            await asyncio.to_thread(self._sync_send, subscription_info, payload_str)
            sub.last_pushed_at = dt.datetime.now(dt.timezone.utc)
            sub.failure_count = 0
            await sub.save()
            return True
        except WebPushException as exc:
            # 404 Not Found or 410 Gone indicates the browser/endpoint is unregistered
            status_code = getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None
            logger.warning(
                "WebPush delivery failed",
                extra={"status_code": status_code, "error": str(exc), "endpoint": sub.endpoint[:40]},
            )
            if status_code in (404, 410):
                sub.is_active = False
            else:
                sub.failure_count += 1
                if sub.failure_count >= 5:
                    sub.is_active = False
            await sub.save()
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in webpush delivery", extra={"error": str(exc)})
            sub.failure_count += 1
            await sub.save()
            return False

    def build_payload(
        self,
        title: str,
        message: str,
        *,
        url: str = "/staff/status",
        tag: Optional[str] = None,
        urgency: str = "high",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Construct standard rich notification payload with vibration and actions."""
        extra_data = data or {}
        extra_data.setdefault("url", url)

        return {
            "title": title,
            "body": message,
            "icon": "/brand/ssn-logo.webp",
            "badge": "/favicon.svg",
            "tag": tag or f"alert-{int(dt.datetime.now().timestamp())}",
            "renotify": True,
            "vibrate": [250, 100, 250, 100, 500] if urgency == "high" else [200, 100, 200],
            "requireInteraction": urgency == "high",
            "data": extra_data,
            "actions": [
                {"action": "open", "title": "View Details 📋"},
                {"action": "dismiss", "title": "Dismiss"},
            ],
        }

    async def send_to_employee(
        self,
        employee_id: str,
        title: str,
        message: str,
        *,
        url: str = "/staff/status",
        urgency: str = "high",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Send push notifications to all registered devices of an employee."""
        subs = await PushSubscription.find(
            {"employee_id": employee_id, "is_active": True}
        ).to_list()

        if not subs:
            logger.info("No active push devices for employee", extra={"employee_id": employee_id})
            return {"sent": 0, "failed": 0, "total": 0}

        payload = self.build_payload(title, message, url=url, urgency=urgency, data=data)
        results = await asyncio.gather(*(self.send_to_subscription(s, payload) for s in subs))
        sent = sum(1 for r in results if r)
        failed = len(results) - sent
        logger.info(
            "Dispatched employee webpush",
            extra={"employee_id": employee_id, "sent": sent, "failed": failed},
        )
        return {"sent": sent, "failed": failed, "total": len(subs)}

    async def send_to_all_employees(
        self,
        title: str,
        message: str,
        *,
        url: str = "/staff/status",
        urgency: str = "normal",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Broadcast push notification to all staff nurse devices."""
        subs = await PushSubscription.find(
            {"employee_id": {"$ne": None}, "is_active": True}
        ).to_list()

        if not subs:
            return {"sent": 0, "failed": 0, "total": 0}

        payload = self.build_payload(title, message, url=url, urgency=urgency, data=data)
        results = await asyncio.gather(*(self.send_to_subscription(s, payload) for s in subs))
        sent = sum(1 for r in results if r)
        failed = len(results) - sent
        return {"sent": sent, "failed": failed, "total": len(subs)}


webpush_service = WebPushService()
