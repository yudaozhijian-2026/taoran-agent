"""Shared model capacity with a guaranteed front-button reservation."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Literal

ModelWorkload = Literal["frontend", "backend"]


@dataclass
class ModelCapacityLease:
    """A model slot released exactly once, including after caller timeouts."""

    controller: ModelCapacityController
    workload: ModelWorkload
    wait_ms: int
    _released: bool = False
    _release_lock: Lock = field(default_factory=Lock)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self.controller._release(self.workload)


class ModelCapacityController:
    """Limit total model calls and prevent background jobs using every slot."""

    def __init__(self, total: int, frontend_reserved: int) -> None:
        if total < 1:
            raise ValueError("total must be positive")
        if frontend_reserved < 0:
            raise ValueError("frontend_reserved cannot be negative")
        effective_reserved = min(frontend_reserved, max(0, total - 1))
        self.total_limit = total
        self.frontend_reserved = effective_reserved
        self.backend_limit = max(1, total - effective_reserved)
        self._total = BoundedSemaphore(total)
        self._backend = BoundedSemaphore(self.backend_limit)
        self._state_lock = Lock()
        self._active_frontend = 0
        self._active_backend = 0

    def acquire(
        self,
        workload: ModelWorkload,
        timeout: float,
    ) -> ModelCapacityLease | None:
        started = monotonic()
        deadline = started + max(0.0, timeout)
        backend_acquired = False
        if workload == "backend":
            backend_acquired = self._backend.acquire(timeout=max(0.0, timeout))
            if not backend_acquired:
                return None
        remaining = max(0.0, deadline - monotonic())
        if not self._total.acquire(timeout=remaining):
            if backend_acquired:
                self._backend.release()
            return None
        with self._state_lock:
            if workload == "frontend":
                self._active_frontend += 1
            else:
                self._active_backend += 1
        return ModelCapacityLease(
            controller=self,
            workload=workload,
            wait_ms=int((monotonic() - started) * 1000),
        )

    def _release(self, workload: ModelWorkload) -> None:
        with self._state_lock:
            if workload == "frontend":
                self._active_frontend -= 1
            else:
                self._active_backend -= 1
        self._total.release()
        if workload == "backend":
            self._backend.release()

    def snapshot(self) -> dict[str, int]:
        with self._state_lock:
            return {
                "total_limit": self.total_limit,
                "frontend_reserved": self.frontend_reserved,
                "backend_limit": self.backend_limit,
                "active_frontend": self._active_frontend,
                "active_backend": self._active_backend,
            }
