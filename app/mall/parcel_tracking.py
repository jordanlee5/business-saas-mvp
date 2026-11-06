"""Read-only tracking adapter. No provider is enabled in production yet."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from threading import BoundedSemaphore, Lock
from time import monotonic

from ..time_utils import UTC8_TIMEZONE

STATUSES = {"PENDING", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION"}


class TrackingNotConfigured(Exception):
    pass


class DisabledProvider:
    def query(self, carrier, tracking_number, *, timeout):
        raise TrackingNotConfigured()


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid tracking time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Tracking time must include timezone")
    return parsed.astimezone(UTC8_TIMEZONE).isoformat(timespec="seconds")


def normalize(value):
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        raise ValueError("Invalid tracking state")
    source = value.get("source")
    events = value.get("events")
    if not isinstance(source, str) or not source.strip() or len(source) > 80:
        raise ValueError("Invalid tracking source")
    if not isinstance(events, list) or len(events) > 100:
        raise ValueError("Invalid tracking events")
    clean = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Invalid tracking event")
        text = event.get("description")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("Invalid tracking description")
        clean.append({"time": timestamp(event.get("time")), "description": text.strip()})
    clean.sort(key=lambda event: event["time"], reverse=True)
    return {"status": value["status"], "source": source.strip(), "events": clean,
            "updated_at": datetime.now(UTC8_TIMEZONE).isoformat(timespec="seconds")}


class TrackingService:
    """Bounded per-process cache and in-flight work; authorize BEFORE calling.

    A real adapter must enforce the passed total network timeout, sanitize data,
    and never log credentials. Virtual adapters are injected only by tests/tools.
    This cache is not shared across web workers and is not a billing quota.
    """
    def __init__(self, provider=None, *, clock=monotonic, ttl=300, cooldown=30,
                 stale_ttl=3600, max_entries=512):
        self.provider = provider or DisabledProvider()
        self.clock, self.ttl, self.cooldown, self.stale_ttl = clock, ttl, cooldown, stale_ttl
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.lock = Lock()
        self.slots = BoundedSemaphore(2)
        self.inflight = set()

    def _view(self, entry, now, availability=None):
        good = entry.get("good") if entry else None
        if good and now - entry["good_at"] < self.stale_ttl:
            return {**deepcopy(good), "availability": availability or "STALE"}
        return {"status": "PENDING", "events": [], "source": "", "updated_at": None,
                "availability": "NOT_CONFIGURED" if entry and entry.get("disabled") else "UNAVAILABLE"}

    def query(self, key, carrier, tracking_number):
        now = self.clock()
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                self.entries.move_to_end(key)
                if now < entry["retry_at"]:
                    return self._view(entry, now, "CACHED" if entry.get("ok") else None)
            if key in self.inflight or not self.slots.acquire(blocking=False):
                return self._view(entry, now)
            self.inflight.add(key)
        try:
            good = normalize(self.provider.query(carrier, tracking_number, timeout=3.0))
            result = dict(good=good, good_at=self.clock(), ok=True,
                          retry_at=self.clock()+self.ttl)
        except Exception as exc:
            # Intentionally never echo provider exception text or HTTP bodies.
            result = dict(entry or {}, ok=False, disabled=isinstance(exc, TrackingNotConfigured),
                          retry_at=self.clock()+self.cooldown)
        finally:
            with self.lock:
                if 'result' in locals():
                    self.entries[key] = result
                    self.entries.move_to_end(key)
                    while len(self.entries) > self.max_entries:
                        self.entries.popitem(last=False)
                self.inflight.discard(key)
                self.slots.release()
        return self._view(result, self.clock(), "LIVE" if result["ok"] else None)


service = TrackingService()


def tracking_service():
    return service
