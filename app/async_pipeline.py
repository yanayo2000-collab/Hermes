from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional


class TokenBucketRateLimiter:
    def __init__(self, rate: int, window_seconds: int = 60) -> None:
        self.rate = max(1, int(rate))
        self.window_seconds = max(1, int(window_seconds))
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            cutoff = now - self.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.rate:
                return False
            bucket.append(now)
            return True


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_timeout_seconds: int = 30) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout_seconds = max(1, int(reset_timeout_seconds))
        self.failure_count = 0
        self.opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return False
            if (time.time() - self.opened_at) >= self.reset_timeout_seconds:
                self.opened_at = None
                self.failure_count = 0
                return False
            return True

    def call(self, func: Callable[[], Any]) -> Any:
        if self.is_open():
            raise RuntimeError('circuit breaker open')
        try:
            result = func()
        except Exception:
            with self._lock:
                self.failure_count += 1
                if self.failure_count >= self.failure_threshold:
                    self.opened_at = time.time()
            raise
        with self._lock:
            self.failure_count = 0
            self.opened_at = None
        return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint_payload(*, ingress_type: str, payload: Dict[str, Any]) -> str:
    basis = canonical_json({'ingress_type': ingress_type, 'payload': payload})
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()
