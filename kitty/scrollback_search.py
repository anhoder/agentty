#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import bisect
import json
import re
import subprocess
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

from .fast_data_types import add_timer, remove_timer, set_scrollback_search, wakeup_main_loop

if TYPE_CHECKING:
    from .window import Window

Match = tuple[int, int, int, int]
Row = tuple[str, tuple[int, ...], bool]


def find_matches(rows: tuple[Row, ...], first_row: int, query: str, case_sensitive: bool = False, regex: bool = False, whole_word: bool = False) -> tuple[Match, ...]:
    """Match logical lines, preserving physical cell coordinates and soft wraps."""
    if not query:
        return ()
    pattern = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    if whole_word:
        # Preserve global inline flags at the beginning of a regex while adding
        # boundaries outside its alternatives (cat|dog must bind as one term).
        expression = pattern.pattern
        while expression.startswith('(?'):
            flags = re.match(r'\(\?[aiLmsux]+\)', expression)
            if flags is None:
                break
            expression = expression[flags.end():]
        if pattern.flags & re.VERBOSE:
            expression += '\n'
        pattern = re.compile(r'(?<!\w)(?:' + expression + r')(?!\w)', pattern.flags)
    matches: list[Match] = []
    parts: list[str] = []
    starts: list[int] = []
    logical: list[tuple[int, Row]] = []
    length = 0

    def flush() -> None:
        for match in pattern.finditer(''.join(parts)):
            if match.start() == match.end():
                continue
            a = bisect.bisect_right(starts, match.start()) - 1
            b = bisect.bisect_right(starts, match.end() - 1) - 1
            sy, start_row = logical[a]
            ey, end_row = logical[b]
            sx = start_row[1][match.start() - starts[a]]
            end_offset = match.end() - starts[b]
            ex = end_row[1][end_offset]
            base = end_row[1][end_offset - 1]
            if ex <= base:
                k = end_offset
                while k < len(end_row[1]) and end_row[1][k] <= base:
                    k += 1
                ex = end_row[1][k] if k < len(end_row[1]) else end_row[1][-1]
            matches.append((sy, sx, ey, ex))

    for index, row in enumerate(rows):
        text, _, continued = row
        if not continued:
            flush()
            parts.clear()
            starts.clear()
            logical.clear()
            length = 0
        if text:
            starts.append(length)
            logical.append((first_row + index, row))
            parts.append(text)
            length += len(text)
    flush()
    return tuple(matches)


def regex_worker() -> None:
    import sys

    rows, first_row, query, case_sensitive, whole_word = json.load(sys.stdin)
    print(json.dumps(find_matches(rows, first_row, query, case_sensitive, True, whole_word)))


def bounded_regex_matches(rows: tuple[Row, ...], first_row: int, query: str, case_sensitive: bool, whole_word: bool = False) -> tuple[Match, ...]:
    from .constants import kitty_exe

    # Python's backtracking engine can hold the GIL indefinitely. Isolate regex
    # evaluation so an expensive expression cannot freeze terminal IO or close.
    result = subprocess.run(
        [kitty_exe(), '+runpy', 'from kitty.scrollback_search import regex_worker; regex_worker()'],
        input=json.dumps((rows, first_row, query, case_sensitive, whole_word)), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1, check=True,
    )
    return tuple(tuple(match) for match in json.loads(result.stdout))


class ScrollbackSearch:
    def __init__(self, window: 'Window') -> None:
        self.window_ref = weakref.ref(window)
        self.query = ''
        self.case_sensitive = self.regex = self.whole_word = False
        self.error = ''
        self.matches: tuple[Match, ...] = ()
        self.current = -1
        self.timer: int | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='scrollback-search')
        self.future: Future[tuple[Match, ...]] | None = None
        self.future_query = ''
        self.revision = self.future_revision = 0
        self.future_generation = self.future_history_added = 0
        self.future_rows: tuple[Row, ...] = ()
        self.future_first_row = 0
        self.generation = -1
        self.rows: list[Row] = []
        self.next_row = self.first_row = self.end_row = 0
        self.capturing = False
        self.closed = False
        self.first_result = True
        self.capture_retries = 0
        self.requested = False
        self.submitted_revision = -1

    @property
    def status(self) -> str:
        if self.submitted_revision != self.revision:
            return ''
        if self.error:
            return self.error
        if self.requested or self.capturing or self.future is not None:
            return '…'
        return f'{self.current + 1}/{len(self.matches)}' if self.matches else '0/0'

    def show(self, initial: str = '') -> None:
        window = self.window_ref()
        if window is None:
            return
        if initial and not self.query:
            self.update(initial)
        set_scrollback_search(window.os_window_id, window.id, self.query, self.status, True, True)

    def notify(self) -> None:
        window = self.window_ref()
        if window is not None and not window.destroyed:
            set_scrollback_search(window.os_window_id, window.id, self.query, self.status, True)
            window.refresh()

    def update(self, query: str) -> None:
        if self.closed:
            return
        self.query = query
        self.revision += 1
        self.error = ''
        self.requested = False
        self.submitted_revision = -1
        self.matches = ()
        self.current = -1
        self.first_result = True
        self.capture_retries = 0
        window = self.window_ref()
        if window is None or window.destroyed:
            self.close()
            return
        window.screen.clear_search()
        self.capturing = False
        self.rows.clear()
        self.generation = -1
        self.notify()

    def submit(self, query: str, backwards: bool = False) -> None:
        if self.closed:
            return
        if query != self.query:
            self.update(query)
        window = self.window_ref()
        if window is None or window.destroyed:
            return
        if self.submitted_revision == self.revision and self.generation == window.screen.search_generation:
            self.navigate(-1 if backwards else 1)
            return
        self.matches = ()
        self.current = -1
        self.error = ''
        self.generation = -1
        self.first_result = True
        self.requested = bool(self.query)
        self.submitted_revision = self.revision
        window.screen.clear_search()
        if self.regex and self.query:
            try:
                re.compile(self.query)
            except re.error as error:
                self.error = f'Invalid regex: {error}'
                self.requested = False
        if self.requested and self.timer is None:
            self.timer = add_timer(self.tick, 0.02, True)
        self.notify()

    def set_option(self, name: str, enabled: bool) -> None:
        if name == 'case_sensitive':
            self.case_sensitive = enabled
        elif name == 'regex':
            self.regex = enabled
        elif name == 'whole_word':
            self.whole_word = enabled
        self.update(self.query)

    def tick(self, _: int | None) -> None:
        window = self.window_ref()
        if self.closed or window is None or window.destroyed:
            self.close()
            return
        if not self.requested and self.future is None:
            return
        screen = window.screen
        if self.future is not None:
            if not self.future.done():
                return
            try:
                matches = self.future.result()
            except subprocess.TimeoutExpired:
                matches = ()
                if self.future_revision == self.revision:
                    self.error = 'Search timed out'
            query = self.future_query
            self.future = None
            valid = query == self.query and self.future_revision == self.revision
            changed = self.future_generation != screen.search_generation
            if valid and changed:
                # Output can advance the history ring while the worker runs.
                # Publish only occurrences whose captured cells still agree;
                # never apply stale coordinates or starve on continuous output.
                shift = screen.search_history_added - self.future_history_added
                first = -screen.historybuf.count if screen.is_main_linebuf() else 0
                checked: dict[int, bool] = {}
                retained: list[Match] = []
                for sy, sx, ey, ex in matches:
                    intact = True
                    for y in range(sy, ey + 1):
                        if y not in checked:
                            live_y = y - shift
                            checked[y] = first <= live_y < screen.lines and screen.search_rows(live_y, 1)[0] == self.future_rows[y - self.future_first_row]
                        if not checked[y]:
                            intact = False
                            break
                    if intact:
                        retained.append((sy - shift, sx, ey - shift, ex))
                matches = tuple(retained)
            self.future_rows = ()
            if valid:
                self.requested = False
                previous = self.matches[self.current] if self.matches and self.current >= 0 else None
                self.matches = matches
                self.current = matches.index(previous) if previous in matches else -1
                if self.current < 0 and matches:
                    top = -screen.scrolled_by
                    self.current = next((i for i, match in enumerate(matches) if match[0] >= top), len(matches) - 1)
                self.apply(scroll=self.first_result)
                self.first_result = False
                self.capture_retries = 0
                self.notify()
                if changed:
                    self.generation = -1
            else:
                self.generation = -1
                self.capture_retries += 1
        if not self.requested or not self.query or self.error:
            return
        if self.capturing and self.generation != screen.search_generation:
            self.capturing = False
            self.generation = -1
            self.capture_retries += 1
        if not self.capturing and self.generation == screen.search_generation:
            return
        if not self.capturing:
            self.generation = screen.search_generation
            self.first_row = -screen.historybuf.count if screen.is_main_linebuf() else 0
            self.next_row = self.first_row
            self.end_row = screen.lines
            self.rows.clear()
            self.capturing = True
            self.notify()
        # Bound main-thread capture work, but converge under continuous output.
        chunk = 256 << min(self.capture_retries, 6)
        count = min(chunk, self.end_row - self.next_row)
        self.rows.extend(screen.search_rows(self.next_row, count))
        self.next_row += count
        if self.next_row >= self.end_row:
            self.capturing = False
            if self.generation != screen.search_generation:
                self.generation = -1
                self.capture_retries += 1
                return
            self.future_query = self.query
            self.future_revision = self.revision
            self.future_generation = self.generation
            self.future_history_added = screen.search_history_added
            self.future_first_row = self.first_row
            self.future_rows = tuple(self.rows)
            if self.regex:
                self.future = self.executor.submit(bounded_regex_matches, self.future_rows, self.first_row, self.query, self.case_sensitive, self.whole_word)
            else:
                self.future = self.executor.submit(find_matches, self.future_rows, self.first_row, self.query, self.case_sensitive, False, self.whole_word)
            self.rows.clear()

    def apply(self, scroll: bool) -> None:
        window = self.window_ref()
        if window is None or window.destroyed:
            return
        screen = window.screen
        if scroll and self.current >= 0:
            y = self.matches[self.current][0]
            window.finish_scroll_animation()
            screen.scroll_to_absolute(max(0, -y + screen.lines // 2))
        screen.set_search_matches(self.matches, self.current)
        window.refresh()

    def navigate(self, step: int) -> None:
        window = self.window_ref()
        if window is not None and self.generation != window.screen.search_generation:
            return
        if not self.matches:
            return
        self.current = (self.current + step) % len(self.matches)
        self.apply(scroll=True)
        self.notify()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            remove_timer(self.timer)
            self.timer = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.rows.clear()
        self.future_rows = ()
        self.matches = ()
        window = self.window_ref()
        if window is not None:
            if hasattr(window, 'screen'):
                window.screen.clear_search()
            set_scrollback_search(window.os_window_id, window.id, '', '', False)
            window.scrollback_search = None
            if not window.destroyed:
                window.refresh()
        wakeup_main_loop()
