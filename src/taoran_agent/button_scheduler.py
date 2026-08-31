"""Bounded FIFO scheduling for each button click's single model request."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Condition
from time import monotonic
from typing import Literal

ButtonLeaseStatus = Literal["acquired", "queue_full", "queue_timeout"]


class ButtonFeedbackScheduler:
    """Admit a bounded number of clicks without oversubscribing model slots."""

    def __init__(self, model_concurrency: int, waiting_capacity: int) -> None:
        if model_concurrency < 1:
            raise ValueError("model_concurrency must be positive")
        if waiting_capacity < 0:
            raise ValueError("waiting_capacity cannot be negative")
        self.active_limit = model_concurrency
        self.waiting_capacity = waiting_capacity
        self._condition = Condition()
        self._active = 0
        self._waiting: deque[object] = deque()

    @contextmanager
    def lease(self, wait_seconds: float) -> Iterator[ButtonLeaseStatus]:
        token = object()
        acquired = False
        status: ButtonLeaseStatus
        deadline = monotonic() + max(0.0, wait_seconds)
        with self._condition:
            if self._active < self.active_limit and not self._waiting:
                self._active += 1
                acquired = True
                status = "acquired"
            elif len(self._waiting) >= self.waiting_capacity:
                status = "queue_full"
            else:
                self._waiting.append(token)
                while True:
                    if self._waiting[0] is token and self._active < self.active_limit:
                        self._waiting.popleft()
                        self._active += 1
                        acquired = True
                        status = "acquired"
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        self._waiting.remove(token)
                        self._condition.notify_all()
                        status = "queue_timeout"
                        break
                    self._condition.wait(remaining)
        try:
            yield status
        finally:
            if acquired:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "active": self._active,
                "active_limit": self.active_limit,
                "waiting": len(self._waiting),
                "waiting_capacity": self.waiting_capacity,
            }
