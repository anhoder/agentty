"""Bounded alternate-screen viewport collection, independent of terminal I/O.

The caller supplies monotonic seconds and immutable text snapshots, executes each
wheel action exactly once, and guards that execution against the same identity.
``valid`` means the original instance, alternate screen and wheel routing are
still safe. Use ``unknown``/``working`` to stop collection while allowing safe
restoration. An identity must include process/screen generation, not just a PID.
This collects visible scrollable text, never a full transcript.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    text: str
    completeness: str
    reason: str
    restoration: str
    active_read: bool
    truncated: bool


@dataclass(frozen=True)
class Action:
    kind: str
    direction: str = ''
    amount: int = 0
    result: Result | None = None


class HistoryRead:
    """Call step repeatedly, even after cancel, until Action.kind == 'done'.

    Header/footer stripping is opt-in, by fixed row counts; the stripped chrome
    must remain exactly equal to the initial chrome on every collected page.
    Limits can be lowered for tests but cannot exceed the production ceilings.
    ``redraw`` reports output activity even when the text is unchanged.
    """

    def __init__(
        self,
        initial_rows: list[str],
        columns: int,
        lines: int,
        now: float,
        *,
        passive_text: str | None = None,
        identity: object = None,
        requested_lines: int | None = None,
        strip_header: int = 0,
        strip_footer: int = 0,
        max_pages: int = 128,
        max_bytes: int = 1024 * 1024,
        max_duration: float = 15,
        restore_duration: float = 5,
    ):
        if columns < 1 or lines != len(initial_rows) or lines < 1:
            raise ValueError('invalid initial dimensions')
        if min(strip_header, strip_footer) < 0 or strip_header + strip_footer >= lines:
            raise ValueError('invalid chrome rows')
        if min(max_pages, max_bytes, max_duration, restore_duration) <= 0:
            raise ValueError('limits must be positive')
        if requested_lines is not None and requested_lines < 1:
            raise ValueError('requested_lines must be positive')
        self.requested_lines = requested_lines
        self.reached_top = False
        self.initial = tuple(initial_rows)
        self.columns, self.lines, self.identity = columns, lines, identity
        self.header, self.footer = strip_header, strip_footer
        self.passive = '\n'.join(initial_rows) if passive_text is None else passive_text
        self.max_pages, self.max_bytes = min(max_pages, 128), min(max_bytes, 1024 * 1024)
        self.end = now + min(max_duration, 15)
        self.restore_duration = min(restore_duration, 5)
        self.phase = 'settle'
        self.last = self.initial
        self.quiet = now + 0.010
        self.deadline = self.quiet
        self.previous = self._body(self.initial)
        self.history = self.previous
        self.pages = 1
        self.up_events = 0
        self.reason = ''
        self.restore_end = 0.0
        self.result: Result | None = None
        self.active = False
        self.cancelled = False

    def _body(self, rows: tuple[str, ...]) -> tuple[str, ...]:
        return rows[self.header : len(rows) - self.footer if self.footer else len(rows)]

    def cancel(self, now: float) -> None:
        """Request cancellation; no I/O occurs until the next guarded step."""
        self.cancelled = True
        self.reason = 'cancelled'

    def _finish(self, restoration: str) -> Action:
        success = self.reason in ('', 'enough_lines') and restoration == 'restored'
        reason = self.reason or 'reached_top'
        if restoration == 'failed':
            reason = 'restoration_failed'
        collected = self.history if self.requested_lines is None else self.history[-self.requested_lines :]
        truncated = not success or not self.reached_top or (self.requested_lines is not None and len(self.history) > self.requested_lines)
        self.result = Result(
            '\n'.join(collected) if success else self.passive, 'viewport_history' if success else 'passive', reason, restoration, self.active, truncated
        )
        return Action('done', result=self.result)

    def _wheel(self, direction: str, amount: int, phase: str, now: float) -> Action:
        self.active = True
        self.phase = phase
        self.deadline = now + 0.120
        self.quiet = now + 0.010
        return Action('wheel', direction, amount)

    def _restore(self, rows: tuple[str, ...], now: float) -> Action:
        self.restore_end = now + self.restore_duration
        if self.phase == 'probe':
            # A down probe can have moved even if its redraw has not arrived.
            return self._wheel('up', 3, 'restore_probe', now)
        if self.up_events:
            return self._wheel('down', self.up_events, 'restore', now)
        return self._finish('restored' if rows == self.initial else 'failed')

    def step(
        self,
        rows: Sequence[str],
        cols: int,
        now: float,
        valid: bool = True,
        *,
        identity: object = None,
        working: bool = False,
        unknown: bool = False,
        redraw: bool = False,
    ) -> Action:
        if self.result is not None:
            return Action('done', result=self.result)
        rows = tuple(rows)
        # These checks precede every possible injection, including recovery.
        if identity != self.identity or not valid:
            self.reason = 'identity_changed' if identity != self.identity else 'guard_invalid'
            return self._finish('failed' if self.active else 'not_needed')
        restoring = self.phase in ('restore', 'restore_probe')
        resized = cols != self.columns or len(rows) != self.lines
        if not restoring and (self.cancelled or resized or working or unknown or now >= self.end):
            self.reason = self.reason or ('resized' if resized else 'working' if working else 'unknown' if unknown else 'timeout')
            return self._restore(rows, now)
        if restoring and now >= self.restore_end:
            return self._finish('failed')
        if rows != self.last or redraw:
            self.last = rows
            self.quiet = now + 0.010
        if now < self.quiet:
            return Action('wait')
        if restoring:
            if cols == self.columns and rows == self.initial:
                return self._finish('restored')
            # Recovery is a single bounded inverse event batch, not an
            # unbounded search that can scroll past the original viewport.
            return Action('wait')
        if self.phase == 'settle':
            if rows != self.initial:
                self.reason = 'initial_changed'
                return self._finish('failed')
            return self._wheel('down', 3, 'probe', now)
        if now < self.deadline:
            return Action('wait')
        if self.phase == 'probe':
            if rows != self.initial:
                self.reason = 'not_at_bottom'
                return self._restore(rows, now)
        else:
            if rows == self.last_page:
                self.reached_top = True
                return self._restore(rows, now)
            if rows[: self.header] != self.initial[: self.header] or (self.footer and rows[-self.footer :] != self.initial[-self.footer :]):
                self.reason = 'chrome_changed'
                return self._restore(rows, now)
            body = self._body(rows)
            # All plausible alignments must agree. Single-row and whitespace
            # anchors are deliberately insufficient evidence of continuity.
            overlaps = [
                n for n in range(2, min(len(body), len(self.previous))) if body[-n:] == self.previous[:n] and sum(bool(x.strip()) for x in body[-n:]) >= 2
            ]
            if len(overlaps) != 1:
                self.reason = 'ambiguous_overlap' if overlaps else 'unaligned'
                return self._restore(rows, now)
            merged = body[: -overlaps[0]] + self.history
            if len('\n'.join(merged).encode('utf-8')) > self.max_bytes:
                self.reason = 'byte_limit'
                return self._restore(rows, now)
            self.history, self.previous = merged, body
            self.pages += 1
        if self.requested_lines is not None and len(self.history) >= self.requested_lines:
            self.reason = 'enough_lines'
            return self._restore(rows, now)
        if self.pages >= self.max_pages or len('\n'.join(self.history).encode('utf-8')) > self.max_bytes:
            self.reason = 'page_limit' if self.pages >= self.max_pages else 'byte_limit'
            return self._restore(rows, now)
        self.last_page = rows
        self.up_events += 3
        return self._wheel('up', 3, 'harvest', now)
