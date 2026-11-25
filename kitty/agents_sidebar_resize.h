#pragma once

#include <math.h>

static inline unsigned
kitty_agents_sidebar_resize_columns(double pointer_x, double scale, double cell_width, double viewport_width) {
    if (scale < 1.0) scale = 1.0;
    if (cell_width < 1.0) cell_width = 1.0;
    double width = fmin(viewport_width * 0.75, fmax(16.0 * cell_width / scale, pointer_x));
    unsigned cols = (unsigned)llround(width * scale / cell_width);
    return cols < 16 ? 16 : cols;
}

static inline int
kitty_agents_sidebar_resize_should_commit(unsigned start, unsigned final) {
    return start != final;
}
