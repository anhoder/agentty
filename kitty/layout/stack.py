#!/usr/bin/env python
# License: GPLv3 Copyright: 2020, Kovid Goyal <kovid at kovidgoyal.net>

from kitty.types import NeighborsMap
from kitty.typing_compat import WindowType
from kitty.window_list import WindowGroup, WindowList

from .base import Layout, layout_single_window, lgd

class Stack(Layout):
    name = 'stack'
    needs_window_borders = True
    must_draw_borders = True
    only_active_window_visible = True

    def layout_single_window_group(self, wg: WindowGroup) -> None:
        # Leave enough background visible for the shadow to read as depth.
        inset = max(16, min(lgd.cell_width, lgd.cell_height) * 2)
        xdecoration_pairs = ((wg.decoration('left') + inset, wg.decoration('right') + inset),)
        ydecoration_pairs = ((wg.decoration('top') + inset, wg.decoration('bottom') + inset),)
        wg.set_geometry(layout_single_window(xdecoration_pairs, ydecoration_pairs, xalignment=lgd.alignment_x, yalignment=lgd.alignment_y))

    def do_layout(self, windows: WindowList) -> None:
        for group in windows.iter_all_layoutable_groups():
            self.layout_single_window_group(group)

    def neighbors_for_window(self, window: WindowType, windows: WindowList) -> NeighborsMap:
        wg = windows.group_for_window(window)
        assert wg is not None
        groups = tuple(windows.iter_all_layoutable_groups())
        idx = groups.index(wg)
        before = [] if wg is groups[0] else [groups[idx - 1].id]
        after = [] if wg is groups[-1] else [groups[idx + 1].id]
        return {'top': before, 'left': before, 'right': after, 'bottom': after}
