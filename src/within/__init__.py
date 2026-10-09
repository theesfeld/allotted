# within — one time budget for a whole call tree.
# Copyright (C) 2026 William Theesfeld
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Deadlines that follow a call through sync code, threads, and async tasks.

A nested scope can only shorten the budget it inherited. Any callee can ask
how much time is left without the caller passing a timeout argument.

Sync code cooperates by calling :func:`check` and by passing :func:`io_timeout`
into the next socket, subprocess, or lock. :func:`async_deadline` also cancels
the current task when the budget runs out.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import math
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Protocol, TypeVar

__all__ = [
    "Clock",
    "DeadlineExceeded",
    "ManualClock",
    "Span",
    "SystemClock",
    "async_bounded",
    "async_deadline",
    "bounded",
    "check",
    "current",
    "deadline",
    "expired",
    "io_timeout",
    "propagate",
    "remaining",
    "shield",
    "use_clock",
]

__version__ = "0.1.0"

_T = TypeVar("_T")
_UNSET = object()


class Clock(Protocol):
    """A monotonic clock. ``now()`` returns seconds from an arbitrary origin."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""


class SystemClock:
    """The process monotonic clock (:func:`time.monotonic`)."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that moves only when :meth:`advance` is called.

    Entering the clock installs it for the current context. Deadlines created
    under it read this clock, so a test can expire a budget without sleeping.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = _finite(start, name="start")
        self._tokens: list[contextvars.Token[Clock]] = []

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move this clock forward. Time does not run backwards."""
        step = _finite(seconds, name="seconds")
        if step < 0:
            raise ValueError("time only moves forward")
        self._now += step

    def __enter__(self) -> ManualClock:
        self._tokens.append(_clock.set(self))
        return self

    def __exit__(self, *exc: object) -> None:
        _clock.reset(self._tokens.pop())


@dataclass(frozen=True, slots=True)
class Span:
    """One deadline scope.

    ``deadline`` is an absolute reading of :attr:`clock`. ``requested`` is the
    budget the caller asked for, before a parent shortened it. ``remaining``
    is negative after the deadline.
    """

    purpose: str
    origin: str
    started: float
    requested: float
    deadline: float | None
    clamped: bool
    clock: Clock
    parent: Span | None
    shielded: bool = False

    @property
    def remaining(self) -> float:
        """Seconds left on this scope. Negative when the deadline has passed."""
        if self.deadline is None:
            raise RuntimeError("a shield has no remaining time")
        return self.deadline - self.clock.now()

    @property
    def elapsed(self) -> float:
        """Seconds since this scope was entered."""
        return self.clock.now() - self.started

    @property
    def expired(self) -> bool:
        """Whether this scope's deadline has been reached."""
        if self.deadline is None:
            return False
        return self.clock.now() >= self.deadline

    def check(self) -> None:
        """Raise :class:`DeadlineExceeded` if this scope has expired."""
        if self.expired:
            raise DeadlineExceeded(self)

    def timeout(self) -> float:
        """Seconds to pass as a blocking timeout for this scope.

        Raises :class:`DeadlineExceeded` when no time is left. A ``0`` timeout
        is not returned: sockets and subprocesses treat ``0`` as non-blocking.
        """
        left = self.remaining
        if left <= 0:
            raise DeadlineExceeded(self)
        return left

    def __repr__(self) -> str:
        if self.shielded:
            return "<Span shield>"
        return f"<Span {self.purpose!r} remaining={self.remaining:.6f}s>"


class DeadlineExceeded(TimeoutError):
    """The active deadline has been reached.

    ``chain`` runs from the scope that failed to the outermost deadline.
    ``overdue`` is how far past the deadline the clock was when this was raised.
    """

    def __init__(self, span: Span) -> None:
        self.span = span
        self.purpose = span.purpose
        self.overdue = max(0.0, -span.remaining)
        self.chain = _chain(span)
        super().__init__(_message(span, self.overdue))


class _Leaf:
    """The innermost span covered by an asyncio timer.

    The timer callback snapshots this before the task unwinds. ``finally``
    blocks then restore the previous span; ``fired`` on the arm keeps the copy.
    """

    __slots__ = ("span",)

    def __init__(self, span: Span) -> None:
        self.span = span


class _Arm(asyncio.Timeout):
    def __init__(self, when: float | None, leaf: _Leaf) -> None:
        super().__init__(when)
        self.leaf = leaf
        self.fired: Span | None = None

    def _on_timeout(self) -> None:
        self.fired = self.leaf.span
        super()._on_timeout()


_clock: contextvars.ContextVar[Clock] = contextvars.ContextVar(
    "within.clock",
    default=SystemClock(),
)
_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "within.span",
    default=None,
)
_arm: contextvars.ContextVar[_Arm | None] = contextvars.ContextVar(
    "within.arm",
    default=None,
)
_leaf: contextvars.ContextVar[_Leaf | None] = contextvars.ContextVar(
    "within.leaf",
    default=None,
)


def deadline(
    seconds: float | None = None,
    *,
    until: float | None = None,
    purpose: str = "",
) -> AbstractContextManager[Span]:
    """Run the block within a deadline.

    Pass ``seconds`` as a duration, or ``until`` as an absolute reading of the
    active clock. When a parent deadline exists, the earlier instant wins.

    The block is cooperative. Call :func:`check` before starting work, and pass
    :func:`io_timeout` to blocking I/O. If the block finishes after the
    deadline with no exception in flight, leaving it raises
    :class:`DeadlineExceeded`.

    A strictly shorter scope inside :func:`async_deadline` reschedules that
    task's cancellation. That error is raised at the ``async with`` which owns
    the timer, and the message names the innermost scope.
    """
    return _Scope(
        seconds,
        until=until,
        purpose=purpose,
        origin=_caller(2),
        tighten=True,
    ).bind()


def async_deadline(
    seconds: float | None = None,
    *,
    until: float | None = None,
    purpose: str = "",
) -> AbstractAsyncContextManager[Span]:
    """:func:`deadline` plus cancellation of the current task.

    A nested call installs its own timer when its deadline is strictly sooner
    than its parent's, so the failure can be caught at the inner ``async with``.
    A nested scope the parent already covers shares the parent's timer. The
    error names the innermost scope that was active when time ran out.

    :class:`ManualClock` does not drive task cancellation.
    """
    return _async_deadline(seconds, until=until, purpose=purpose, origin=_caller(2))


def bounded(seconds: float, *, purpose: str = "") -> Callable[[_T], _T]:
    """Decorate a function so each call runs inside :func:`deadline`."""

    budget = _finite(seconds, name="seconds")
    if budget < 0:
        raise ValueError("seconds must be >= 0")

    def decorate(fn: _T) -> _T:
        if inspect.iscoroutinefunction(fn):
            raise TypeError(f"{_qualname(fn)} is async; use async_bounded")
        label = purpose or _qualname(fn)
        origin = _function_origin(fn)

        @functools.wraps(fn)  # type: ignore[arg-type]
        def wrapper(*args: object, **kwargs: object) -> object:
            with _Scope(budget, until=None, purpose=label, origin=origin, tighten=True).bind():
                return fn(*args, **kwargs)  # type: ignore[operator]

        return wrapper  # type: ignore[return-value]

    return decorate


def async_bounded(seconds: float, *, purpose: str = "") -> Callable[[_T], _T]:
    """Decorate a coroutine function so each call runs inside :func:`async_deadline`."""

    budget = _finite(seconds, name="seconds")
    if budget < 0:
        raise ValueError("seconds must be >= 0")

    def decorate(fn: _T) -> _T:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"{_qualname(fn)} is synchronous; use bounded")
        label = purpose or _qualname(fn)
        origin = _function_origin(fn)

        @functools.wraps(fn)  # type: ignore[arg-type]
        async def wrapper(*args: object, **kwargs: object) -> object:
            async with _async_deadline(budget, until=None, purpose=label, origin=origin):
                return await fn(*args, **kwargs)  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorate


def remaining() -> float | None:
    """Seconds left on the visible deadline.

    ``None`` means no deadline is visible, including inside :func:`shield`.
    A negative number means the deadline has already passed.
    """
    span = _visible()
    if span is None:
        return None
    return span.remaining


def expired() -> bool:
    """Whether the visible deadline has been reached.

    ``False`` when no deadline is visible.
    """
    span = _visible()
    return span is not None and span.expired


def current() -> Span | None:
    """The visible deadline.

    ``None`` outside a deadline and inside :func:`shield`.
    """
    return _visible()


def check() -> None:
    """Raise :class:`DeadlineExceeded` if the visible deadline has been reached.

    Does nothing when no deadline is visible.
    """
    span = _visible()
    if span is not None and span.expired:
        raise DeadlineExceeded(span)


def io_timeout(default: float | None = None) -> float | None:
    """A timeout value for the next blocking call.

    With no visible deadline, ``default`` is returned. With time left, the
    remaining seconds are returned. A deadline that has already passed raises
    :class:`DeadlineExceeded`.

    This never returns ``0``. Socket and subprocess timeouts treat ``0`` as
    non-blocking rather than as "already out of time".
    """
    left = remaining()
    if left is None:
        if default is not None:
            _finite(default, name="default")
        return default
    if left <= 0:
        check()
        raise AssertionError("deadline check returned")
    return left


def shield() -> AbstractContextManager[None]:
    """Hide the current deadline from :func:`check` and :func:`remaining`.

    A deadline entered inside the shield is still clamped to the hidden parent.
    The shield does not stop an :func:`async_deadline` timer. An await inside
    the shield is cancelled when that timer fires. Cleanup that must run during
    cancellation belongs in ``finally``.
    """
    return _Shield(_caller(2))


def propagate(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Return ``fn`` bound to a copy of the current context.

    :class:`threading.Thread` and executor workers start with an empty context.
    Call :func:`propagate` at the moment the work is submitted:

    ``executor.submit(propagate(fn), arg)``

    The :class:`Span` object itself can be passed anywhere. :meth:`Span.remaining`
    and :meth:`Span.check` read the span's own clock and do not need this.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> _T:
        return ctx.run(fn, *args, **kwargs)

    return wrapper


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the current context."""
    if not callable(getattr(clock, "now", None)):
        raise TypeError("clock must provide now()")
    token = _clock.set(clock)
    try:
        yield clock
    finally:
        _clock.reset(token)


class _Scope:
    """One deadline's contextvar, leaf, and optional timer tightening."""

    def __init__(
        self,
        seconds: float | None,
        *,
        until: float | None,
        purpose: str,
        origin: str,
        tighten: bool,
    ) -> None:
        self._seconds = seconds
        self._until = until
        self._purpose = _purpose(purpose, origin)
        self._origin = origin
        self._tighten = tighten
        self._token: contextvars.Token[Span | None] | None = None
        self._leaf: _Leaf | None = None
        self._previous_leaf: Span | None = None
        self._touched_leaf = False
        self._arm: _Arm | None = None
        self._previous_when: float | None | object = _UNSET
        self.span: Span | None = None

    def open(self) -> Span:
        span = _make_span(self._seconds, self._until, self._purpose, self._origin)
        self.span = span
        self._token = _span.set(span)
        try:
            self._push_leaf(span)
            if self._tighten:
                self._tighten_arm(span)
            if span.expired:
                raise DeadlineExceeded(span)
            return span
        except BaseException:
            self._restore_arm()
            self._teardown()
            raise

    def close(self, exc_info: tuple[type[BaseException] | None, BaseException | None, object]) -> None:
        try:
            self._restore_arm()
            if exc_info[0] is None and self.span is not None and self.span.expired:
                raise DeadlineExceeded(self.span)
        finally:
            self._teardown()

    @contextmanager
    def bind(self) -> Iterator[Span]:
        span = self.open()
        try:
            yield span
        finally:
            self.close(sys.exc_info())

    def _push_leaf(self, span: Span) -> None:
        leaf = _leaf.get()
        if leaf is None:
            return
        self._leaf = leaf
        self._previous_leaf = leaf.span
        self._touched_leaf = True
        leaf.span = span

    def _tighten_arm(self, span: Span) -> None:
        arm = _arm.get()
        if arm is None or span.deadline is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        previous = arm.when()
        new_when = loop.time() + max(span.remaining, 0.0)
        if previous is not None and new_when >= previous:
            return
        arm.reschedule(new_when)
        self._arm = arm
        self._previous_when = previous

    def _restore_arm(self) -> None:
        if self._arm is None or self._previous_when is _UNSET:
            return
        arm = self._arm
        previous = self._previous_when
        self._arm = None
        self._previous_when = _UNSET
        if arm.fired is not None:
            return
        when = previous if isinstance(previous, float) or previous is None else None
        try:
            arm.reschedule(when)
        except RuntimeError:
            return

    def _teardown(self) -> None:
        if self._touched_leaf and self._leaf is not None:
            self._leaf.span = self._previous_leaf  # type: ignore[assignment]
            self._touched_leaf = False
        if self._token is not None:
            _span.reset(self._token)
            self._token = None


class _Shield:
    def __init__(self, origin: str) -> None:
        self._origin = origin
        self._token: contextvars.Token[Span | None] | None = None

    def __enter__(self) -> None:
        parent = _span.get()
        marker = Span(
            purpose="shield",
            origin=self._origin,
            started=0.0,
            requested=0.0,
            deadline=None,
            clamped=False,
            clock=_clock.get(),
            parent=parent,
            shielded=True,
        )
        self._token = _span.set(marker)

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _span.reset(self._token)
            self._token = None


@asynccontextmanager
async def _async_deadline(
    seconds: float | None,
    *,
    until: float | None,
    purpose: str,
    origin: str,
) -> AsyncIterator[Span]:
    if not isinstance(_clock.get(), SystemClock):
        raise RuntimeError(
            "async_deadline follows the event loop clock, and a manual clock "
            "is active. Use deadline() under the manual clock, or leave the "
            "manual clock before entering async_deadline()."
        )
    scope = _Scope(seconds, until=until, purpose=purpose, origin=origin, tighten=False)
    span = scope.open()
    arm: _Arm | None = None
    arm_token: contextvars.Token[_Arm | None] | None = None
    leaf_token: contextvars.Token[_Leaf | None] | None = None
    try:
        if _should_own(span):
            loop = asyncio.get_running_loop()
            leaf = _Leaf(span)
            leaf_token = _leaf.set(leaf)
            arm = _Arm(loop.time() + max(span.remaining, 0.0), leaf)
            arm_token = _arm.set(arm)
            try:
                async with arm:
                    yield span
            except DeadlineExceeded:
                raise
            except TimeoutError as exc:
                # A TimeoutError raised by user code leaves arm.fired unset.
                # Only the timer callback sets it, and only that case is ours.
                if arm.fired is None:
                    raise
                raise DeadlineExceeded(arm.fired) from exc
        else:
            yield span
    finally:
        try:
            scope.close(sys.exc_info())
        finally:
            if arm_token is not None:
                _arm.reset(arm_token)
            if leaf_token is not None:
                _leaf.reset(leaf_token)


def _should_own(span: Span) -> bool:
    """Install a new task timer only when this scope ends strictly sooner.

    Two asyncio timers scheduled for the same instant both cancel the task, and
    the outer one is what surfaces. Sharing the parent's timer keeps a single
    cancellation and still names this scope, because the leaf points here.
    """
    if _arm.get() is None:
        return True
    parent = span.parent
    if parent is None or parent.deadline is None or span.deadline is None:
        return True
    return span.deadline < parent.deadline


def _make_span(
    seconds: float | None,
    until: float | None,
    purpose: str,
    origin: str,
) -> Span:
    if (seconds is None) == (until is None):
        raise TypeError("pass exactly one of seconds or until")
    clock = _clock.get()
    now = _read_clock(clock)
    if seconds is not None:
        requested = _finite(seconds, name="seconds")
        if requested < 0:
            raise ValueError("seconds must be >= 0")
        absolute = now + requested
    else:
        assert until is not None
        absolute = _finite(until, name="until")
        requested = absolute - now
    base = _clamp_base()
    clamped = False
    if base is not None:
        if base.clock is not clock:
            raise RuntimeError(
                "this deadline uses a different clock from its parent. "
                "Exit the parent before switching clocks."
            )
        assert base.deadline is not None
        if absolute > base.deadline:
            absolute = base.deadline
            clamped = True
    if not math.isfinite(absolute):
        raise ValueError("deadline is not finite")
    return Span(
        purpose=purpose,
        origin=origin,
        started=now,
        requested=requested,
        deadline=absolute,
        clamped=clamped,
        clock=clock,
        parent=base,
    )


def _clamp_base() -> Span | None:
    span = _span.get()
    while span is not None and span.shielded:
        span = span.parent
    return span


def _visible() -> Span | None:
    span = _span.get()
    if span is None or span.shielded:
        return None
    return span


def _chain(span: Span) -> tuple[Span, ...]:
    items: list[Span] = []
    current: Span | None = span
    while current is not None:
        if not current.shielded:
            items.append(current)
        current = current.parent
    return tuple(items)


def _message(span: Span, overdue: float) -> str:
    lines = [f"deadline exceeded: {span.purpose} ({overdue:.6f}s over)"]
    for item in _chain(span):
        note = ", clamped to parent" if item.clamped else ""
        lines.append(f"  {item.purpose}: requested {item.requested:.6f}s{note}")
        lines.append(f"    at {item.origin}")
    return "\n".join(lines)


def _purpose(purpose: str, origin: str) -> str:
    if not isinstance(purpose, str):
        raise TypeError("purpose must be a string")
    if purpose == "":
        return origin
    if purpose.strip() == "":
        raise ValueError("purpose must not be empty")
    return purpose


def _finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _read_clock(clock: Clock) -> float:
    return _finite(clock.now(), name="clock.now()")


def _caller(depth: int) -> str:
    frame = sys._getframe(depth)
    code = frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.f_lineno} in {code.co_name}"


def _qualname(fn: object) -> str:
    return getattr(fn, "__qualname__", getattr(fn, "__name__", "function"))


def _function_origin(fn: object) -> str:
    code = getattr(fn, "__code__", None)
    if code is None:
        return _qualname(fn)
    return f"{os.path.basename(code.co_filename)}:{code.co_firstlineno} in {_qualname(fn)}"
