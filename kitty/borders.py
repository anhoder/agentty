#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from collections.abc import Iterable
from enum import IntFlag
from functools import partial
from math import ceil, floor
from typing import NamedTuple

from .fast_data_types import current_focused_os_window_id, get_options, pt_to_px, set_borders_rects, viewport_for_window
from .typing_compat import LayoutType
from .utils import color_as_int
from .window_list import WindowGroup, WindowList


class BorderColor(IntFlag):
    # These are indices into the array of colors in the border vertex shader
    default_bg, active, inactive, window_bg, bell, tab_bar_bg, tab_bar_margin_color, tab_bar_left_edge_color, tab_bar_right_edge_color = range(9)


class Border(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int
    color: BorderColor
    border_type: int = 0
    horizontal: bool = False
    render: bool = True
    radius: int = 0
    thickness: int = 0


class PaneFrame(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int
    radius: int
    spread: int


def pane_frame(wg: WindowGroup, radius: int, full_border: bool) -> PaneFrame | None:
    geometry = wg.geometry
    if geometry is None or geometry.right <= geometry.left or geometry.bottom <= geometry.top:
        return None
    c = geometry.compensatory
    padding = tuple(wg.effective_padding(edge) + getattr(c, edge) for edge in ('left', 'top', 'right', 'bottom'))
    border = wg.effective_border() if full_border else 0
    left = max(0, geometry.left - padding[0] - border)
    top = max(0, geometry.top - padding[1] - border)
    right = geometry.right + padding[2] + border
    bottom = geometry.bottom + padding[3] + border
    if right <= left or bottom <= top:
        return None
    margin = max(max(0, space - pad - border) for space, pad in zip(geometry.spaces, padding))
    return PaneFrame(left, top, right, bottom, min(radius, (right - left) // 2, (bottom - top) // 2), margin * 2)


class ShadowPatch(NamedTuple):
    frame: int
    left: int
    top: int
    right: int
    bottom: int
    spread: float


def shadow_patches(frames: list[PaneFrame], bounds: tuple[int, int, int, int], fraction: float) -> list[ShadowPatch]:
    """Split edges at neighbor boundaries, so each T-junction uses its local gap."""
    fraction = max(0.0, min(1.0, fraction))
    patches: list[ShadowPatch] = []
    if not fraction:
        return patches
    left, top, right, bottom = bounds
    for index, frame in enumerate(frames):
        for horizontal, before in ((False, True), (False, False), (True, True), (True, False)):
            near, far = (frame.left, frame.right) if horizontal else (frame.top, frame.bottom)
            edge = (frame.top if before else frame.bottom) if horizontal else (frame.left if before else frame.right)
            outer = (top if before else bottom) if horizontal else (left if before else right)
            candidates: list[tuple[int, int, int]] = []
            cuts = {near, far}
            for other_index, other in enumerate(frames):
                if other_index == index:
                    continue
                start, end = (other.left, other.right) if horizontal else (other.top, other.bottom)
                boundary = (other.bottom if before else other.top) if horizontal else (other.right if before else other.left)
                gap = edge - boundary if before else boundary - edge
                if gap >= 0 and start < far and end > near:
                    candidates.append((max(near, start), min(far, end), gap))
                    cuts.update((max(near, start), min(far, end)))
            ordered = sorted(cuts)
            for start, end in zip(ordered, ordered[1:]):
                fallback = min((distance for _, _, distance in candidates), default=abs(edge - outer))
                gap = min((distance for a, b, distance in candidates if a < end and b > start), default=fallback)
                spread = gap * fraction
                if spread <= 0:
                    continue
                # Extend endpoint strips around rounded corners. Adjacent strips
                # combine with MAX and every pane footprint is erased afterward.
                a = start - spread if start == near else start
                b = end + spread if end == far else end
                low, high = (edge - spread, edge) if before else (edge, edge + spread)
                x1, y1, x2, y2 = (a, low, b, high) if horizontal else (low, a, high, b)
                patches.append(ShadowPatch(index, max(left, floor(x1)), max(top, floor(y1)), min(right, ceil(x2)), min(bottom, ceil(y2)), spread))
    return [p for p in patches if p.right > p.left and p.bottom > p.top]


def vertical_edge(rects: list[Border], color: BorderColor, width: int, top: int, bottom: int, left: int, border_type: int, render: bool = True) -> None:
    if width > 0:
        rects.append(Border(left, top, left + width, bottom, color, border_type, False, render))


def horizontal_edge(rects: list[Border], color: BorderColor, height: int, left: int, right: int, top: int, border_type: int, render: bool = True) -> None:
    if height > 0:
        rects.append(Border(left, top, right, top + height, color, border_type, True, render))


def add_borders(rects: list[Border], color: BorderColor, wg: WindowGroup, radius: int = 0) -> None:
    geometry = wg.geometry
    if geometry is None:
        return
    # The compensatory padding (pixels left over after fitting whole cells) is
    # inside the border so that the frames of neighboring windows line up
    c = geometry.compensatory
    pl, pt = wg.effective_padding('left') + c.left, wg.effective_padding('top') + c.top
    pr, pb = wg.effective_padding('right') + c.right, wg.effective_padding('bottom') + c.bottom
    left = geometry.left - pl
    top = geometry.top - pt
    lr = geometry.right
    right = lr + pr
    bt = geometry.bottom
    bottom = bt + pb
    h = partial(horizontal_edge, rects, color)
    v = partial(vertical_edge, rects, color)
    width = wg.effective_border()
    bt = bottom
    lr = right
    left -= width
    top -= width
    right += width
    bottom += width
    pl = pr = pb = pt = width
    wid = wg.active_window_id
    render_edges = radius < 1
    h(pt, left, right, top, -wid, render_edges)
    h(pb, left, right, bt, wid, render_edges)
    v(pl, top, bottom, left, -wid, render_edges)
    v(pr, top, bottom, lr, wid, render_edges)
    if radius > 0:
        rects.append(Border(left, top, right, bottom, color, render=False, radius=radius, thickness=width))


class Borders:
    def __init__(self, os_window_id: int, tab_id: int):
        self.os_window_id = os_window_id
        self.tab_id = tab_id

    def __call__(
        self,
        all_windows: WindowList,
        current_layout: LayoutType,
        tab_bar_rects: Iterable[Border],
        draw_window_borders: bool = True,
    ) -> None:
        opts = get_options()
        draw_active_borders = opts.active_border_color is not None
        rects: list[Border] = []
        active_group = all_windows.active_group
        blank_color = BorderColor.default_bg
        if current_layout.name == 'stack' and active_group is not None:
            # The inset area belongs to the active pane, not the OS background.
            blank_color = (color_as_int(active_group.default_bg) << 8) | BorderColor.window_bg
        for br in current_layout.blank_rects:
            rects.append(Border(*br, blank_color))
        rects.extend(tab_bar_rects)
        bw = 0
        groups = tuple(all_windows.iter_visible_groups())
        if groups:
            bw = groups[0].effective_border()
        draw_borders = bw > 0 and draw_window_borders

        # Count visible windows
        num_visible_groups = len(groups)

        # When draw_window_borders_for_single_window is set and there's only 1 window,
        # behave like draw_minimal_borders is False (draw full borders around the window)
        if opts.draw_window_borders_for_single_window and num_visible_groups == 1:
            draw_minimal_borders = False
        else:
            draw_minimal_borders = opts.draw_minimal_borders and max(opts.window_margin_width) < 1
        if current_layout.name == 'stack':
            draw_minimal_borders = False

        # For single window with the option enabled, check OS window focus state
        # When unfocused, the border should appear inactive
        os_window_focused = True
        if opts.draw_window_borders_for_single_window and num_visible_groups == 1:
            os_window_focused = current_focused_os_window_id() == self.os_window_id

        radius_val, radius_unit = opts.window_border_radius
        radius = pt_to_px(radius_val, self.os_window_id) if radius_unit == 'pt' else round(radius_val)
        if current_layout.name == 'stack' and radius < 1:
            radius = max(8, round(pt_to_px(8, self.os_window_id)))

        if draw_borders and not draw_minimal_borders:
            for i, wg in enumerate(groups):
                window_bg = color_as_int(wg.default_bg)
                window_bg = (window_bg << 8) | BorderColor.window_bg
                if wg is active_group and draw_active_borders and os_window_focused:
                    color = BorderColor.active
                else:
                    color = BorderColor.bell if wg.needs_attention else BorderColor.inactive
                add_borders(rects, color, wg, radius)

        if draw_minimal_borders:
            for border_line in current_layout.get_minimal_borders(all_windows):
                rects.append(Border(*border_line.edges, border_line.color, border_line.window_id, border_line.horizontal))
        frames = [frame for wg in groups if (frame := pane_frame(wg, radius if not draw_minimal_borders else 0, draw_borders and not draw_minimal_borders)) is not None]
        central = viewport_for_window(self.os_window_id)[0]
        bounds = (central.left, central.top, central.left + central.width, central.top + central.height)
        patches = shadow_patches(frames, bounds, opts.pane_shadow_spread) if current_layout.name != 'stack' else [
            ShadowPatch(i, *bounds, 40.0) for i, frame in enumerate(frames) if frame.spread
        ]
        set_borders_rects(self.os_window_id, self.tab_id, rects, frames, patches)
