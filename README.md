# allotted

[![PyPI](https://img.shields.io/pypi/v/allotted.svg)](https://pypi.org/project/allotted/)

`allotted` keeps one time budget for a whole call. You set it at the edge of a request, a job, or a command. Every function inside that call can see the time that is left, and a nested scope can only shorten the budget it inherited.

```python
from allotted import deadline, io_timeout, check

def fetch(url: str) -> bytes:
    import urllib.request

    with deadline(10, purpose="fetch"):
        check()
        with urllib.request.urlopen(url, timeout=io_timeout(10)) as response:
            return response.read()

def page(url: str) -> bytes:
    with deadline(2.5, purpose="render page"):
        return fetch(url)
```

`fetch` takes no timeout argument. It reads the budget `page` already opened. Ten seconds would run past the two and a half seconds still left, so `fetch` is cut down to what remains. When the time is spent, the error names every scope that was open:

```text
deadline exceeded: fetch (0.012000s over)
  fetch: requested 10.000000s, clamped to parent
    at page.py:6 in fetch
  render page: requested 2.500000s
    at page.py:12 in page
```

The same budget works in synchronous code, in `asyncio`, and in a worker thread. Tests move the clock by hand.

## Install

Python 3.11 or newer. The library has no runtime dependencies. Releases are on [PyPI](https://pypi.org/project/allotted/).

Add it to a project with [uv](https://docs.astral.sh/uv/):

```sh
uv add allotted
```

Install it into the current environment:

```sh
uv pip install allotted
```

```sh
pip install allotted
```

```python
from allotted import deadline, io_timeout, check
```

## Contents

- [Open a budget](#open-a-budget)
- [Nesting](#nesting)
- [Read the time left](#read-the-time-left)
- [Pass the time left to I/O](#pass-the-time-left-to-io)
- [When the error is raised](#when-the-error-is-raised)
- [Catch a missed deadline](#catch-a-missed-deadline)
- [The exception](#the-exception)
- [Synchronous code](#synchronous-code)
- [Asyncio](#asyncio)
- [Threads](#threads)
- [Tests and clocks](#tests-and-clocks)
- [Decorators](#decorators)
- [Recipes](#recipes)
- [API](#api)
- [Limits](#limits)
- [Development](#development)
- [License](#license)

## Open a budget

`deadline` is a context manager. Pass a duration in seconds, and an optional `purpose` that shows up in the error.

```python
from allotted import deadline, check

with deadline(2.5, purpose="render page"):
    check()
    render()
```

The block yields a `Span`. Hold that object when you want to ask that particular scope, including from another thread.

```python
with deadline(2.5, purpose="render page") as span:
    render(timeout=span.timeout())
```

`purpose` is a short name for the scope. Leave it out and the message uses the file, line, and function that opened the deadline.

An absolute instant uses `until`. The number is a reading of the active clock, the same clock as `time.monotonic()` in production. It is a duration from an arbitrary origin, so pass a value from that clock.

```python
import time
from allotted import deadline

with deadline(until=time.monotonic() + 2.5, purpose="render page"):
    render()
```

Pass exactly one of `seconds` or `until`. `seconds` is a finite number greater than or equal to zero. `True` and `False` are rejected, because `bool` is a subclass of `int`. A duration of `0` is already due: the body does not run, and `DeadlineExceeded` is raised as the block is entered.

## Nesting

An inner scope ends at the earlier of the instant it asked for and the instant its parent already had.

```python
from allotted import deadline

with deadline(2.5, purpose="render page"):
    with deadline(10, purpose="fetch") as fetch:
        fetch.clamped          # True
        fetch.requested        # 10.0, what fetch asked for
        fetch.remaining        # at most 2.5
    with deadline(0.4, purpose="thumbnail") as thumb:
        thumb.clamped          # False; 0.4 ends sooner than the parent
```

`Span.parent` is the outer scope. `Span.clamped` is true when the parent moved the deadline earlier than this scope requested. The chain in the error lists every scope from the one that failed out to the oldest.

A scope entered inside `shield()` is still clamped to the hidden parent. Hiding the budget from checks does not grant extra time.

## Read the time left

These four functions read the deadline that is visible in the current context.

| Call | No deadline visible | Time left | Deadline reached |
| --- | --- | --- | --- |
| `remaining()` | `None` | seconds left, possibly a fraction | a negative number |
| `expired()` | `False` | `False` | `True` |
| `current()` | `None` | the `Span` | the `Span` |
| `check()` | returns | returns | raises `DeadlineExceeded` |

`None` from `remaining()` means the caller may take as long as its own default allows. A negative number means the deadline is already in the past. Zero means it is due at this instant.

`shield()` hides the current deadline from all four calls. Inside the shield, `remaining()` is `None`, `expired()` is `False`, `current()` is `None`, and `check()` returns. A deadline opened inside the shield is visible again, and it is still clamped to the outer budget.

```python
from allotted import deadline, shield, check, remaining

with deadline(2.5, purpose="render page"):
    with shield():
        check()                 # the page budget is hidden
        assert remaining() is None
        cleanup_bookkeeping()
    check()                     # the page budget applies again
```

The `Span` you kept from `with deadline(...) as span` is independent of that lookup. `span.remaining`, `span.expired`, `span.check()`, and `span.timeout()` follow the span even after the context has moved on, and even in a thread that never inherited the context.

## Pass the time left to I/O

`io_timeout(default)` is the number to give a socket, a subprocess, a database driver, or a lock.

- With no deadline visible, it returns `default`. `default` may be `None` when the callee treats that as "wait as long as you need".
- With time left, it returns the seconds remaining. `default` is ignored.
- With the deadline already reached, it raises `DeadlineExceeded`.

It never returns `0` while a deadline is active. A timeout of `0` means non-blocking on sockets and on `subprocess`, which is a different instruction from "there is no time left". If you pass `default=0` and no deadline is open, that `0` is returned unchanged, because you asked for non-blocking I/O.

```python
import subprocess
from allotted import deadline, io_timeout, check

def convert(src: str, dest: str) -> None:
    check()
    subprocess.run(
        ["convert", src, dest],
        check=True,
        timeout=io_timeout(30),
    )

with deadline(5, purpose="upload"):
    convert("in.png", "out.png")
```

`span.timeout()` is the same rule for one span you are holding. It raises when that span is spent, and the value it returns is greater than zero.

Check before the work, then bound the blocking call. A tight loop that never calls `check` and never blocks in I/O can run past the deadline until it next looks. See [Limits](#limits).

## When the error is raised

`DeadlineExceeded` is raised in four situations.

1. Entering a scope whose deadline is already due, including `deadline(0)` and an `until` that is already in the past.
2. `check()`, `io_timeout()`, `span.check()`, or `span.timeout()` observes a spent deadline.
3. A `with` block finishes with no exception in flight, and its deadline has passed. Forgetting a check still fails the scope on the way out.
4. An `async_deadline` timer cancels the task. The error is raised at the `async with` that owns the timer.

An exception that the block is already raising is left alone. A `RuntimeError` inside a spent scope comes out as that `RuntimeError`. The deadline error is raised on the way out only when the block would otherwise have completed.

Catch the deadline outside the `with`. A handler inside the same scope is still inside a budget that has run out, so leaving the block raises again.

```python
from allotted import DeadlineExceeded, deadline

try:
    with deadline(1, purpose="fetch"):
        fetch(url)
except DeadlineExceeded:
    return cached(url)
```

## Catch a missed deadline

The scope you can catch is the scope whose `with` raises. For synchronous code, that is the `deadline` block itself.

For asyncio, a nested `async_deadline` that ends strictly sooner than its parent owns its own timer. Catch that inner block and the outer budget continues:

```python
import asyncio
from allotted import DeadlineExceeded, async_deadline

async def render(url: str) -> bytes:
    async with async_deadline(2.5, purpose="render page"):
        try:
            async with async_deadline(1, purpose="fetch"):
                body = await fetch(url)
        except DeadlineExceeded:
            body = cached(url)
        return await compose(body)
```

A nested async scope that asks for as much time as the parent, or more, shares the parent's timer. The parent's `async with` raises, and `purpose` on the exception is the inner scope that was active. Handle that failure at the outer boundary, or give the inner scope a strictly shorter budget when the inner failure should be recoverable.

A synchronous `deadline` inside `async_deadline` pulls the task timer earlier, so an `await` stops at the tighter instant. The `DeadlineExceeded` is raised by the `async with` that owns the timer, and its `purpose` is the synchronous scope. Wrap a recoverable step in its own shorter `async_deadline` when you want to catch it and keep going.

## The exception

`DeadlineExceeded` is a `TimeoutError`, so an existing `except TimeoutError` catches it. It carries the scope that failed.

| Attribute | Meaning |
| --- | --- |
| `purpose` | Name of the scope that failed |
| `overdue` | Seconds past the deadline when the error was created. `0` when it failed exactly on time |
| `span` | That scope |
| `chain` | Tuple of scopes from the failure out to the outermost deadline |

`str(exc)` is the block shown at the top of this document. Each line gives the purpose, the seconds that scope requested, whether a parent clamped it, and the file, line, and function that opened it.

A `TimeoutError` raised by your own code, such as a socket library, is left as that error. `allotted` rewrites a timeout only when its own timer fired.

Cancelling a task with `task.cancel()` stays a `CancelledError`. A deadline and an outside cancellation are different events.

## Synchronous code

Put the budget on the operation the user is waiting for. Deeper functions call `check` and `io_timeout` and stay free of timeout parameters.

```python
from allotted import deadline, io_timeout, check

def query(sql: str, args: tuple) -> list:
    check()
    with connect() as db:
        db.execute(sql, args, timeout=io_timeout(30))
        return db.fetchall()

def report(user_id: int) -> dict:
    with deadline(2, purpose="report"):
        rows = query("select * from events where user = %s", (user_id,))
        return summarize(rows)
```

`io_timeout(30)` means "30 seconds when nobody has set a budget". Under `report`, the driver receives whatever part of the 2 seconds is left.

`shield` is for bookkeeping that should proceed after you already know the budget is spent, such as writing a metric. It hides `check` and `remaining`. It does not add time, and a deadline opened inside it is clamped to the outer scope.

```python
from allotted import deadline, shield

with deadline(2, purpose="report"):
    try:
        build()
    finally:
        with shield():
            record_timing()
```

## Asyncio

`async_deadline` follows the same nesting rules and cancels the current task when the budget runs out. The cancellation uses the event loop clock, which is the same clock as `time.monotonic()`.

```python
import asyncio
from allotted import async_deadline, deadline

async def render(url: str) -> bytes:
    async with async_deadline(2.5, purpose="render page"):
        with deadline(1, purpose="fetch"):
            return await asyncio.to_thread(fetch, url)
```

`asyncio.to_thread` copies the context into the worker, so `fetch` sees the budget and `io_timeout` bounds the socket. The wait on the event-loop side is cancelled with the task. The worker thread itself keeps running until its socket timeout returns. A raw `threading.Thread` does not copy the context. Use [propagate](#threads) for that.

Each task has its own budget. Two tasks created under the same parent each open their own `async_deadline`, and each sees only the scope it entered.

`async_deadline` requires the system clock. Opening one while a `ManualClock` is installed raises `RuntimeError`. Drive async tests with a short real duration, and use `ManualClock` for the synchronous checks.

Cleanup that must run as the task is cancelled belongs in `finally`. `shield` hides cooperative checks. It does not disarm the task timer, so an `await` inside a shield is still cancelled when the owning `async_deadline` fires.

```python
async with async_deadline(2.5, purpose="render page"):
    try:
        await work()
    finally:
        write_access_log()
```

## Threads

A new thread starts with an empty context. `remaining()` in that thread is `None` unless you copy the context at the moment you submit the work.

```python
from concurrent.futures import ThreadPoolExecutor
from allotted import deadline, propagate

def work(item: str) -> str:
    return transform(item)

with ThreadPoolExecutor() as pool:
    with deadline(5, purpose="batch"):
        futures = [pool.submit(propagate(work), item) for item in items]
```

Call `propagate` inside the deadline, where the work is submitted. `propagate(work)` copies the context immediately. A wrapper built before the deadline was opened carries an empty context.

The other way to cross a thread is to pass the `Span` and use the methods on it. Those read the span's own clock and ignore the worker's context.

```python
def work(span, item):
    span.check()
    return transform(item, timeout=span.timeout())

with deadline(5, purpose="batch") as span:
    pool.submit(work, span, item)
```

`propagate` also works with `threading.Thread`:

```python
import threading
from allotted import deadline, propagate

with deadline(5, purpose="batch"):
    thread = threading.Thread(target=propagate(work), args=(item,))
    thread.start()
```

## Tests and clocks

Production code reads `SystemClock`, a thin wrapper around `time.monotonic()`. Tests install `ManualClock`. It moves only when `advance` is called, so a budget can expire without sleeping.

```python
from allotted import DeadlineExceeded, ManualClock, check, deadline, remaining

def test_report_stops_at_the_budget() -> None:
    with ManualClock() as clock:
        with deadline(5, purpose="report"):
            assert remaining() == 5
            clock.advance(5)
            try:
                check()
            except DeadlineExceeded as exc:
                assert exc.purpose == "report"
```

Deadlines capture the clock that is active when they open. A nested deadline that finds a different clock raises `RuntimeError`. Exit the outer scope before switching clocks. `advance` rejects a negative step.

`use_clock` installs any object with a `now()` method that returns a finite number of seconds. `ManualClock` is that object plus `advance` and a context manager.

```python
from allotted import deadline, use_clock

class OffsetClock:
    def __init__(self) -> None:
        self.t = 0.0
    def now(self) -> float:
        return self.t

with use_clock(OffsetClock()) as clock:
    with deadline(until=10, purpose="job") as span:
        assert span.remaining == 10
```

`async_deadline` does not run under `ManualClock` or `use_clock`. The task timer follows the event loop.

## Decorators

`bounded` opens a `deadline` for one synchronous call. `async_bounded` opens an `async_deadline` for one coroutine function. The default purpose is the function's qualified name. The origin recorded on the span is the function definition.

```python
from allotted import async_bounded, bounded

@bounded(1, purpose="parse")
def parse(raw: str) -> dict:
    ...

@async_bounded(1, purpose="parse")
async def parse_async(raw: str) -> dict:
    ...
```

Decorating a coroutine function with `bounded`, or a plain function with `async_bounded`, raises `TypeError`. The duration is checked when the decorator is applied: a negative number, a non-number, or a boolean raises before the function is called.

## Recipes

### One budget for an HTTP handler

Open the budget where the request enters. Clients, queries, and renders underneath call `check` and `io_timeout`.

```python
from allotted import async_deadline, io_timeout, check

async def handle(request):
    async with async_deadline(2.5, purpose=f"{request.method} {request.path}"):
        user = await users.get(request.user_id)
        return await render(user)

async def get(user_id: int):
    check()
    return await db.fetchrow(
        "select * from users where id = $1",
        user_id,
        timeout=io_timeout(2.5),
    )
```

### Try a fast source, then a slow one

The fast attempt has to end strictly sooner than the outer budget, and the handler has to sit outside that inner block.

```python
from allotted import DeadlineExceeded, async_deadline

async def load(url: str) -> bytes:
    async with async_deadline(3, purpose="load"):
        try:
            async with async_deadline(0.4, purpose="cache"):
                return await cache.get(url)
        except DeadlineExceeded:
            pass
        async with async_deadline(2, purpose="origin"):
            return await origin.get(url)
```

### Subprocess

```python
import subprocess
from allotted import deadline, io_timeout

def identify(path: str) -> str:
    with deadline(5, purpose="identify"):
        completed = subprocess.run(
            ["identify", path],
            check=True,
            capture_output=True,
            text=True,
            timeout=io_timeout(5),
        )
        return completed.stdout
```

### Absolute deadline shared by several steps

`until` is one instant on the clock. Later steps see whatever remains. They do not each receive a fresh copy of the original duration.

```python
import time
from allotted import deadline

done_at = time.monotonic() + 10
with deadline(until=done_at, purpose="import"):
    read_files()
    write_rows()
    refresh_index()
```

## API

### `deadline(seconds=None, *, until=None, purpose="")`

Context manager. Opens a cooperative deadline and yields a `Span`.

`seconds` is the duration. `until` is an absolute reading of the active clock. Pass one of them. `purpose` labels the scope. An empty purpose is replaced with the caller's file, line, and function.

The earlier of this scope and its parent wins. Entering a scope that is already due raises `DeadlineExceeded` before the body runs. Leaving a scope that has expired, with no other exception in flight, raises `DeadlineExceeded`.

Inside an active `async_deadline`, a strictly shorter `deadline` reschedules that task's timer. The resulting `DeadlineExceeded` is raised at the owning `async with`.

### `async_deadline(seconds=None, *, until=None, purpose="")`

Async context manager. Same accounting as `deadline`, and the current task is cancelled when the budget runs out.

A nested call installs its own timer when its deadline is strictly sooner than its parent's. Otherwise it shares the parent's timer, and the error still names the inner scope.

Raises `RuntimeError` when a `ManualClock` or any other non-system clock is active.

### `remaining() -> float | None`

Seconds left on the visible deadline. `None` when no deadline is visible, including inside `shield`. Negative when the deadline has passed.

### `expired() -> bool`

`True` when the visible deadline has been reached. `False` when no deadline is visible.

### `current() -> Span | None`

The visible `Span`, or `None`.

### `check() -> None`

Raises `DeadlineExceeded` when the visible deadline has been reached. Returns immediately when no deadline is visible.

### `io_timeout(default=None) -> float | None`

Timeout argument for the next blocking call. See [Pass the time left to I/O](#pass-the-time-left-to-io).

### `shield()`

Context manager. Hides the current deadline from `remaining`, `expired`, `current`, and `check`. A deadline opened inside the shield is clamped to the hidden parent. An `async_deadline` timer keeps running.

### `propagate(fn) -> fn`

Returns a wrapper that runs `fn` in a copy of the context taken at the moment `propagate` is called. Use it when submitting work to a thread or a pool.

### `bounded(seconds, *, purpose="")`

Decorator. Each call to a synchronous function runs inside `deadline`. `purpose` defaults to the qualified name. A coroutine function is rejected with `TypeError`.

### `async_bounded(seconds, *, purpose="")`

Decorator. Each call to a coroutine function runs inside `async_deadline`. A synchronous function is rejected with `TypeError`.

### `use_clock(clock)`

Context manager. Installs `clock` for the current context. `clock.now()` returns a finite number of seconds. Nested deadlines opened under a different clock raise `RuntimeError`.

### `ManualClock(start=0.0)`

A clock that moves only when `advance(seconds)` is called. Using it as a context manager installs it. `advance` rejects a negative or non-finite step. Entering the same clock twice nests cleanly.

### `SystemClock`

`now()` returns `time.monotonic()`. This is the clock used when no other clock is installed. `async_deadline` requires it.

### `Clock`

Protocol with a single method, `now() -> float`.

### `Span`

One deadline scope, yielded by `deadline` and `async_deadline`. Frozen. The fields are:

| Field | Meaning |
| --- | --- |
| `purpose` | Label, or the call site when no purpose was given |
| `origin` | `file.py:line in function` that opened the scope |
| `started` | Clock reading when the scope opened |
| `requested` | Seconds the caller asked for, before clamping |
| `deadline` | Absolute clock reading when the scope is due. `None` on a shield |
| `clamped` | `True` when a parent made the deadline earlier than requested |
| `clock` | The clock this scope reads |
| `parent` | Outer scope, or `None` |
| `shielded` | `True` for the marker `shield()` pushes. User code sees this only by walking `parent` links; `current()` skips shields |
| `remaining` | `deadline - clock.now()`. Negative after the deadline. Reading it on a shield raises `RuntimeError` |
| `elapsed` | Seconds since `started` |
| `expired` | `True` when `now >= deadline` |

`span.check()` raises `DeadlineExceeded` when the span is expired. `span.timeout()` returns the seconds left and raises when none are left. Both follow the span you hold, in any thread.

### `DeadlineExceeded`

Subclass of `TimeoutError`. Attributes are `purpose`, `overdue`, `span`, and `chain`. `chain` runs from the failed scope to the outermost deadline. `overdue` is fixed when the exception is created.

## Limits

Synchronous code cooperates. `allotted` runs your function to its next `check`, `io_timeout`, or blocking call that honors the timeout you passed. A pure Python loop that never looks at the budget runs until it does.

One blocking call can finish after the deadline by as long as that call takes to notice its own timeout. The scope then fails on the way out, or at the next check.

`asyncio` cancellation applies to the task. `await` points inside the owning `async_deadline` are cancelled. A thread started with `asyncio.to_thread` or `propagate` is not killed. Bound that thread's blocking calls with `io_timeout` or `span.timeout()`.

`shield` affects cooperative lookups. The task timer still fires.

`async_deadline` follows the event loop clock. `ManualClock` is for synchronous tests.

Context variables do not cross a `threading.Thread` or a `ThreadPoolExecutor` worker on their own. Copy them with `propagate`, or pass the `Span`.

A deadline and its parent must share one clock object. Mixing `ManualClock` with `SystemClock` inside one nested budget raises `RuntimeError`.

## Development

```sh
uv venv
uv pip install -e '.[dev]'
uv run pytest
```

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests cover nesting, the cooperative checks, thread propagation, and the asyncio timer. Async tests use short real sleeps. Synchronous tests use `ManualClock`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
