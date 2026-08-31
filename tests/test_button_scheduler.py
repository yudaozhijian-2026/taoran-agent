from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

from taoran_agent.button_scheduler import ButtonFeedbackScheduler


def test_scheduler_derives_four_active_clicks_from_four_model_slots() -> None:
    scheduler = ButtonFeedbackScheduler(model_concurrency=4, waiting_capacity=8)

    assert scheduler.snapshot() == {
        "active": 0,
        "active_limit": 4,
        "waiting": 0,
        "waiting_capacity": 8,
    }


def test_scheduler_bounds_waiters_and_releases_in_fifo_order() -> None:
    scheduler = ButtonFeedbackScheduler(model_concurrency=1, waiting_capacity=1)
    events: list[str] = []

    def wait_for_lease() -> str:
        with scheduler.lease(1) as status:
            events.append(status)
            return status

    with ThreadPoolExecutor(max_workers=1) as executor:
        with scheduler.lease(1) as first:
            assert first == "acquired"
            future = executor.submit(wait_for_lease)
            deadline = monotonic() + 1
            while scheduler.snapshot()["waiting"] != 1 and monotonic() < deadline:
                sleep(0.005)
            assert scheduler.snapshot()["waiting"] == 1
            with scheduler.lease(0) as rejected:
                assert rejected == "queue_full"
        assert future.result(timeout=1) == "acquired"
    assert events == ["acquired"]


def test_scheduler_returns_queue_timeout_without_leaking_capacity() -> None:
    scheduler = ButtonFeedbackScheduler(model_concurrency=1, waiting_capacity=1)

    with scheduler.lease(1) as first:
        assert first == "acquired"
        with scheduler.lease(0.01) as second:
            assert second == "queue_timeout"
    assert scheduler.snapshot()["active"] == 0
    assert scheduler.snapshot()["waiting"] == 0
