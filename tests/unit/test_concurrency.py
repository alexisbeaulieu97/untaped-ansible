"""Tests for the bounded thread-pool mapping helper."""

from __future__ import annotations

import threading

import pytest

from untaped_ansible._concurrency import bounded_map


def test_serial_path_preserves_input_order() -> None:
    seen: list[tuple[int, str]] = []

    bounded_map(
        lambda item: f"r{item}",
        [3, 1, 2],
        concurrency=1,
        on_each=lambda item, result: seen.append((item, result)),
    )

    assert seen == [(3, "r3"), (1, "r1"), (2, "r2")]


def test_worker_error_propagates_and_cancels_queued_items() -> None:
    """A propagating error must cancel queued work instead of draining it."""
    items = list(range(10))
    started: set[int] = set()
    gate = threading.Event()
    lock = threading.Lock()

    def work(item: int) -> int:
        with lock:
            started.add(item)
        if item == 0:
            raise ValueError("boom")
        gate.wait(timeout=5.0)
        return item

    # Release the one in-flight worker shortly after the failure has been
    # consumed and the queued futures have been cancelled.
    releaser = threading.Timer(0.5, gate.set)
    releaser.start()
    try:
        with pytest.raises(ValueError, match="boom"):
            bounded_map(work, items, concurrency=2, on_each=lambda item, result: None)
    finally:
        gate.set()
        releaser.cancel()

    # Besides the failing item, only the in-flight items ran (each worker
    # thread may dequeue one more item before the main thread cancels); the
    # remaining queued items were cancelled when the error escaped the loop,
    # not drained.
    assert 0 in started
    assert started <= {0, 1, 2}
