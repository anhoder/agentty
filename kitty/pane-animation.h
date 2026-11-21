/*
 * Copyright (C) 2026, Kovid Goyal <kovid at kovidgoyal.net>
 * Distributed under terms of the GPL3 license.
 */
#pragma once

#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    double x, y, width, height;
} PaneAnimationRect;

typedef struct {
    double damping, frequency, sine_coefficient;
} PaneAnimationSpring;

typedef struct {
    uint64_t id;
    PaneAnimationRect from, target;
    PaneAnimationSpring position_spring, size_spring;
} PaneAnimationFrame;

typedef struct {
    PaneAnimationFrame *frames, *next_frames;
    size_t count, capacity;
    uint64_t tab_id;
    int width, height;
    bool active, content_valid, reuse_content;
    double started_at, duration, texture_used_at;
    uint32_t texture_id;
    int texture_width, texture_height;
} PaneAnimation;

static inline bool
pane_animation_active_at(const PaneAnimation *a, double at) {
    return a->active && at - a->started_at < a->duration;
}

static inline bool
pane_animation_rect_equal(PaneAnimationRect a, PaneAnimationRect b) {
    return a.x == b.x && a.y == b.y && a.width == b.width && a.height == b.height;
}

static inline double
pane_animation_damping(double travel, double percentage, double maximum) {
    double fraction = percentage / 100.;
    if (travel > 0 && maximum > 0) fraction = fmin(fraction, maximum / travel);
    if (!(fraction > 0)) return 1.;
    double logarithm = log(fmin(fraction, 0.999));
    return -logarithm / sqrt(M_PI * M_PI + logarithm * logarithm);
}

// Retain only reasonably sized textures between animations, for at most two
// seconds. Active animations still use the actual viewport size.
#define PANE_ANIMATION_TEXTURE_CACHE_BYTES (64ull * 1024 * 1024)
#define PANE_ANIMATION_TEXTURE_IDLE_SECONDS 2.

static inline PaneAnimationSpring
pane_animation_spring(double damping) {
    double frequency = damping >= 1. ? 0. : sqrt(1. - damping * damping);
    return (PaneAnimationSpring){damping, frequency, frequency > 0. ? damping / frequency : 0.};
}

static inline double
pane_animation_progress(double elapsed, double duration, const PaneAnimationSpring *spring) {
    if (elapsed >= duration) return 1.;
    if (elapsed <= 0) return 0.;
    double t = 12. * elapsed / duration;
    if (spring->damping >= 1.) return 1. - exp(-t) * (1. + t);
    return 1. - exp(-spring->damping * t) *
        (cos(spring->frequency * t) + spring->sine_coefficient * sin(spring->frequency * t));
}

static inline PaneAnimationRect
pane_animation_sample(const PaneAnimationFrame *frame, double elapsed, double duration) {
    double p = pane_animation_progress(elapsed, duration, &frame->position_spring);
    double s = frame->position_spring.damping == frame->size_spring.damping ? p :
        pane_animation_progress(elapsed, duration, &frame->size_spring);
    PaneAnimationRect a = frame->from, b = frame->target;
    double width = fmax(0., a.width + (b.width - a.width) * s);
    double height = fmax(0., a.height + (b.height - a.height) * s);
    double cx = a.x + a.width / 2. + (b.x + b.width / 2. - a.x - a.width / 2.) * p;
    double cy = a.y + a.height / 2. + (b.y + b.height / 2. - a.y - a.height / 2.) * p;
    return (PaneAnimationRect){cx - width / 2., cy - height / 2., width, height};
}
