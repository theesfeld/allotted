# Copyright (C) 2026 William Theesfeld
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import math
import threading

import pytest

from within import (
    DeadlineExceeded,
    ManualClock,
    async_bounded,
    async_deadline,
    bounded,
    check,
    current,
    deadline,
    expired,
    io_timeout,
    propagate,
    remaining,
    shield,
    use_clock,
)


def test_no_deadline_is_unlimited() -> None:
    assert remaining() is None
    assert current() is None
    assert expired() is False
    check()
    assert io_timeout() is None
    assert io_timeout(7) == 7


def test_manual_clock_budget_is_exact() -> None:
    with ManualClock() as clock:
        with deadline(5, purpose="job") as span:
            assert span.remaining == 5
            assert remaining() == 5
            assert span.elapsed == 0
            assert not expired()
            check()
            assert io_timeout(99) == 5
            clock.advance(1.5)
            assert remaining() == 3.5
            assert span.elapsed == 1.5
            assert io_timeout() == 3.5
        assert remaining() is None
        assert current() is None


def test_zero_seconds_expires_before_the_body() -> None:
    ran = False
    with ManualClock():
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(0, purpose="zero"):
                ran = True
    assert ran is False
    assert caught.value.purpose == "zero"
    assert caught.value.overdue == 0
    assert current() is None


def test_check_and_clean_exit_report_the_deadline() -> None:
    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(2, purpose="job"):
                clock.advance(2)
                check()
    assert caught.value.purpose == "job"
    assert caught.value.overdue == 0

    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(2, purpose="forgot"):
                clock.advance(2.25)
        assert caught.value.purpose == "forgot"
        assert caught.value.overdue == pytest.approx(0.25)


def test_a_body_exception_is_not_replaced() -> None:
    with ManualClock() as clock:
        with pytest.raises(RuntimeError, match="boom"):
            with deadline(1, purpose="job"):
                clock.advance(5)
                raise RuntimeError("boom")
    assert current() is None


def test_child_cannot_extend_parent() -> None:
    with ManualClock() as clock:
        with deadline(5, purpose="outer") as outer:
            with deadline(100, purpose="inner") as inner:
                assert inner.clamped
                assert inner.deadline == outer.deadline
                assert inner.requested == 100
                assert inner.remaining == 5
                assert inner.parent is outer
            with pytest.raises(DeadlineExceeded) as caught:
                with deadline(1, purpose="tight") as tight:
                    assert not tight.clamped
                    assert tight.deadline == clock.now() + 1
                    clock.advance(1)
                    check()
            assert caught.value.purpose == "tight"
            assert remaining() == 4


def test_absolute_until_and_past_deadline() -> None:
    with ManualClock() as clock:
        with deadline(until=4, purpose="abs") as span:
            assert span.deadline == 4
            assert span.remaining == 4
            with deadline(until=100, purpose="later") as later:
                assert later.clamped
                assert later.deadline == 4
        clock.advance(10)
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(until=3, purpose="past"):
                pass
        assert caught.value.purpose == "past"


def test_three_level_chain_names_every_scope() -> None:
    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(10, purpose="request"):
                with deadline(4, purpose="handler"):
                    with deadline(50, purpose="db"):
                        clock.advance(4)
                        check()
    text = str(caught.value)
    assert caught.value.purpose == "db"
    assert [span.purpose for span in caught.value.chain] == ["db", "handler", "request"]
    assert "clamped to parent" in text
    assert "request" in text
    assert "handler" in text
    assert isinstance(caught.value, TimeoutError)


def test_origin_points_at_the_caller() -> None:
    def place() -> None:
        with deadline(0, purpose="placed"):
            pass

    with ManualClock():
        with pytest.raises(DeadlineExceeded) as caught:
            place()
    assert "place" in caught.value.span.origin
    assert "test_within.py" in caught.value.span.origin


def test_empty_purpose_uses_the_caller() -> None:
    with ManualClock():
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(0):
                pass
    assert "test_empty_purpose_uses_the_caller" in caught.value.purpose
    assert "test_within.py" in caught.value.purpose


def test_shield_hides_checks_and_still_clamps() -> None:
    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(1, purpose="outer") as outer:
                clock.advance(1)
                with shield():
                    check()
                    assert remaining() is None
                    assert current() is None
                    assert expired() is False
                    with pytest.raises(DeadlineExceeded) as inner:
                        with deadline(5, purpose="inner"):
                            pass
                    assert inner.value.purpose == "inner"
                    assert inner.value.span.clamped
                    assert inner.value.span.parent is outer
                    check()
                check()
        assert caught.value.purpose == "outer"


def test_deadline_inside_shield_can_be_shorter_than_the_hidden_parent() -> None:
    with ManualClock():
        with deadline(10, purpose="outer") as outer:
            with shield():
                with deadline(1, purpose="inner") as inner:
                    assert not inner.clamped
                    assert inner.deadline == inner.started + 1
                    assert inner.parent is outer
                assert remaining() is None
            assert remaining() == 10


def test_io_timeout_rejects_a_spent_budget_and_bad_defaults() -> None:
    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded):
            with deadline(1, purpose="job"):
                clock.advance(1)
                io_timeout(5)
    with pytest.raises(TypeError):
        io_timeout(True)  # type: ignore[arg-type]
    assert io_timeout(0) == 0


def test_span_timeout_and_check_travel_without_context() -> None:
    with ManualClock() as clock:
        with pytest.raises(DeadlineExceeded) as caught:
            with deadline(2, purpose="job") as span:
                assert span.timeout() == 2
                clock.advance(2)
                with pytest.raises(DeadlineExceeded):
                    span.timeout()
                with pytest.raises(DeadlineExceeded):
                    span.check()
                held = span
        assert caught.value.purpose == "job"
        with pytest.raises(DeadlineExceeded):
            held.timeout()


def test_rejected_arguments() -> None:
    with pytest.raises(TypeError):
        with deadline(purpose="x"):
            pass
    with pytest.raises(TypeError):
        with deadline(1, until=2, purpose="x"):
            pass
    with pytest.raises(TypeError):
        with deadline(True, purpose="x"):  # type: ignore[arg-type]
            pass
    with pytest.raises(ValueError):
        with deadline(-1, purpose="x"):
            pass
    with pytest.raises(ValueError):
        with deadline(math.inf, purpose="x"):
            pass
    with pytest.raises(ValueError):
        with deadline(1, purpose="   "):
            pass
    with pytest.raises(TypeError):
        with deadline(1, purpose=1):  # type: ignore[arg-type]
            pass
    with pytest.raises(ValueError):
        ManualClock().advance(-1)
    with pytest.raises(TypeError):
        with use_clock(object()):  # type: ignore[arg-type]
            pass


def test_nested_manual_clock_is_a_different_clock() -> None:
    with ManualClock():
        with deadline(1, purpose="outer"):
            with ManualClock():
                with pytest.raises(RuntimeError, match="different clock"):
                    with deadline(1, purpose="inner"):
                        pass


def test_use_clock_restores_the_previous_clock() -> None:
    with ManualClock() as outer:
        outer.advance(3)
        with use_clock(ManualClock()) as inner:
            assert inner.now() == 0
            with deadline(1, purpose="inner") as span:
                assert span.started == 0
        assert outer.now() == 3


def test_propagate_copies_the_deadline_into_a_thread() -> None:
    seen: list[float | None] = []

    def worker() -> None:
        seen.append(remaining())

    with ManualClock():
        with deadline(3, purpose="main"):
            propagate(worker)()
            bare = threading.Thread(target=worker)
            carried = threading.Thread(target=propagate(worker))
            bare.start()
            carried.start()
            bare.join()
            carried.join()
    assert seen == [3.0, None, 3.0]


def test_bounded_wraps_a_sync_call() -> None:
    @bounded(4, purpose="limited")
    def work() -> str:
        check()
        assert current() is not None
        return current().purpose  # type: ignore[union-attr]

    with ManualClock():
        assert work() == "limited"
    assert current() is None

    with pytest.raises(TypeError):

        @bounded(1)
        async def nope() -> None:
            return None


def test_context_is_cleared_after_an_error() -> None:
    with ManualClock():
        with pytest.raises(DeadlineExceeded):
            with deadline(0, purpose="gone"):
                pass
    assert remaining() is None
    assert current() is None


def test_async_shorter_scope_can_be_caught() -> None:
    async def scene() -> str:
        async with async_deadline(1, purpose="outer"):
            try:
                async with async_deadline(0.05, purpose="inner"):
                    await asyncio.sleep(2)
            except DeadlineExceeded as exc:
                assert exc.purpose == "inner"
                assert exc.span.parent is not None
                assert exc.span.parent.purpose == "outer"
            assert current() is not None
            assert current().purpose == "outer"  # type: ignore[union-attr]
            assert remaining() is not None and remaining() > 0
            await asyncio.sleep(0.01)
            return "continued"
        return "fell through"

    assert asyncio.run(scene()) == "continued"


def test_async_clamped_scope_is_named_in_the_outer_error() -> None:
    async def scene() -> None:
        async with async_deadline(0.05, purpose="outer"):
            async with async_deadline(5, purpose="inner"):
                await asyncio.sleep(2)

    with pytest.raises(DeadlineExceeded) as caught:
        asyncio.run(scene())
    assert caught.value.purpose == "inner"
    assert caught.value.span.clamped
    assert caught.value.span.parent is not None
    assert caught.value.span.parent.purpose == "outer"


def test_sync_deadline_tightens_the_async_timer() -> None:
    async def scene() -> None:
        async with async_deadline(1, purpose="outer"):
            with deadline(0.05, purpose="db"):
                await asyncio.sleep(2)

    with pytest.raises(DeadlineExceeded) as caught:
        asyncio.run(scene())
    assert caught.value.purpose == "db"
    assert caught.value.span.parent is not None
    assert caught.value.span.parent.purpose == "outer"


def test_shield_does_not_stop_async_cancellation() -> None:
    async def scene() -> None:
        async with async_deadline(0.05, purpose="job"):
            with shield():
                await asyncio.sleep(2)

    with pytest.raises(DeadlineExceeded) as caught:
        asyncio.run(scene())
    assert caught.value.purpose == "job"


def test_async_tasks_keep_separate_budgets() -> None:
    async def probe(name: str, found: list[str]) -> None:
        async with async_deadline(1, purpose=name):
            await asyncio.sleep(0.01)
            assert current() is not None
            found.append(current().purpose)  # type: ignore[union-attr]

    async def scene() -> list[str]:
        found: list[str] = []
        await asyncio.gather(probe("one", found), probe("two", found))
        return found

    assert sorted(asyncio.run(scene())) == ["one", "two"]


def test_external_cancellation_stays_cancellation() -> None:
    async def body() -> None:
        async with async_deadline(5, purpose="job"):
            await asyncio.sleep(10)

    async def scene() -> None:
        task = asyncio.create_task(body())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scene())


def test_user_timeout_error_is_not_rewritten() -> None:
    async def scene() -> None:
        async with async_deadline(1, purpose="job"):
            raise TimeoutError("socket closed")

    with pytest.raises(TimeoutError, match="socket closed") as caught:
        asyncio.run(scene())
    assert not isinstance(caught.value, DeadlineExceeded)


def test_async_origin_points_at_the_caller() -> None:
    async def place() -> None:
        async with async_deadline(0, purpose="placed"):
            pass

    with pytest.raises(DeadlineExceeded) as caught:
        asyncio.run(place())
    assert "place" in caught.value.span.origin
    assert "test_within.py" in caught.value.span.origin


def test_async_manual_clock_is_refused() -> None:
    async def scene() -> None:
        async with async_deadline(1, purpose="job"):
            pass

    with ManualClock():
        with pytest.raises(RuntimeError, match="manual clock"):
            asyncio.run(scene())


def test_async_bounded_cancels() -> None:
    @async_bounded(0.05, purpose="capped")
    async def slow() -> str:
        await asyncio.sleep(2)
        return "finished"

    with pytest.raises(DeadlineExceeded) as caught:
        asyncio.run(slow())
    assert caught.value.purpose == "capped"

    with pytest.raises(TypeError):

        @async_bounded(1)
        def sync_nope() -> None:
            return None


def test_deadline_restores_a_tightened_timer_when_the_inner_scope_ends() -> None:
    async def scene() -> str:
        async with async_deadline(0.4, purpose="outer"):
            with deadline(0.05, purpose="blip"):
                left = remaining()
                assert left is not None and left <= 0.05
            # The await must use the outer budget again. A timer left at the
            # inner instant would cancel this sleep.
            await asyncio.sleep(0.15)
            return "kept"

    assert asyncio.run(scene()) == "kept"
