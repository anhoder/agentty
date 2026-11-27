#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kitty.constants import kitty_base_dir

from .base import BaseTest


class TestPaneAnimationCadence(BaseTest):
    def test_display_frames_are_not_throttled_by_output_batching(self):
        if not shutil.which('clang'):
            self.skipTest('Native render scheduler probe requires clang')
        source_dir = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = (source_dir / 'kitty' / 'child-monitor.c').read_text()
        start = source.index('static void\nrender(monotonic_t now, bool input_read)')
        end = source.index('\n#undef TD\n}', start) + len('\n#undef TD\n}')
        # Execute the production scheduler with a deterministic display clock.
        # No application, terminal child, graphics context or real window is started.
        prelude = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef int64_t monotonic_t;
#define MONOTONIC_T_MIN INT64_MIN
#define OPT(x) options.x
#define EVDBG(...) do {} while (0)
#define call_boss(...) do {} while (0)
#define debug_rendering(...) do {} while (0)
#define USE_RENDER_FRAMES sync_frames
#define RENDER_FRAME_READY 1
#define __APPLE__ 1
static bool sync_frames = true;
static struct {monotonic_t repaint_delay;} options;
typedef struct {bool active;} PaneAnimation;
typedef struct {bool in_progress;} LiveResize;
typedef struct {
    PaneAnimation pane_animation;
    LiveResize live_resize;
    unsigned id, keep_rendering_till_swap, render_state, painted;
    bool input_held, needs_render, visible;
} OSWindow;
static OSWindow windows[2];
static struct {
    bool check_for_active_animated_images;
    struct {unsigned os_window;} thumbnail_callback;
    size_t num_os_windows;
    OSWindow *os_windows;
} global_state = {.num_os_windows=2, .os_windows=windows};
static void set_maximum_wait(monotonic_t wait) {(void)wait;}
static bool should_os_window_be_rendered(OSWindow *w) {return w->visible;}
static bool render_os_window(OSWindow *w, monotonic_t now, bool scan) {
    (void)now; (void)scan;
    if (!w->visible || w->render_state != RENDER_FRAME_READY) return false;
    w->painted++;
    w->render_state = 0;
    return true;
}
'''
        scenario = r'''
static void require(bool condition, const char *message) {
    if (!condition) {fprintf(stderr, "%s\n", message); exit(1);}
}
int main(int argc, char **argv) {
    require(argc == 4, "expected rate, repaint delay and sync mode");
    const unsigned hz = (unsigned)atoi(argv[1]);
    options.repaint_delay = (monotonic_t)atoi(argv[2]) * 1000000;
    sync_frames = atoi(argv[3]) != 0;
    windows[0].id=1; windows[1].id=2;
    windows[0].visible=windows[1].visible=true;
    windows[0].pane_animation.active=true;
    monotonic_t now=1000000000ll;
    for (unsigned frame=0; frame<hz; frame++) {
        windows[0].render_state=windows[1].render_state=RENDER_FRAME_READY;
        global_state.check_for_active_animated_images=true;
        now=1000000000ll+(monotonic_t)frame*1000000000ll/hz;
        render(now, false);
    }
    const unsigned ordinary_expected = (hz-1) / ((options.repaint_delay*hz+999999999ll)/1000000000ll) + 1;
    require(windows[1].painted == ordinary_expected, "animation changed ordinary window output limit");
    require(windows[0].painted == (sync_frames ? hz : ordinary_expected), "animation skipped ready display frames or bypassed unsynchronized limit");
    // Ending motion must immediately restore ordinary batching, not keep repainting.
    windows[0].pane_animation.active=false;
    windows[0].render_state=RENDER_FRAME_READY;
    unsigned before=windows[0].painted;
    render(now+1, false);
    require(windows[0].painted==before, "finished animation bypassed ordinary output delay");
    // Input remains latency-sensitive regardless of the output batching deadline.
    render(now+2, true);
    require(windows[0].painted==before+1, "pending input was delayed");
    printf("%uHz: animation=%u ordinary=%u sync=%d\n", hz, sync_frames ? hz : ordinary_expected, ordinary_expected, sync_frames);
    return 0;
}
'''
        build_dir = source_dir / 'build'
        build_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='pane-cadence-', dir=build_dir) as directory:
            probe = Path(directory) / 'probe.c'
            executable = Path(directory) / 'probe'
            probe.write_text(prelude + source[start:end] + scenario)
            compiled = subprocess.run(['clang', '-O2', str(probe), '-o', str(executable)], capture_output=True, text=True)
            self.ae(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            for hz in (60, 120, 144, 160, 240):
                for delay in (8, 10):
                    for sync in (0, 1):
                        with self.subTest(hz=hz, delay=delay, sync=sync):
                            result = subprocess.run([str(executable), str(hz), str(delay), str(sync)], capture_output=True, text=True, timeout=5)
                            self.ae(result.returncode, 0, result.stdout + result.stderr)


class TestPaneAnimationPixels(BaseTest):
    def test_pane_animation_preserves_content_pixels(self):
        from kitty.constants import is_macos

        if not is_macos or not shutil.which('clang'):
            self.skipTest('Offscreen pane compositor probe requires macOS and clang')
        from kitty.shaders.slang import glsl_shaders, load_glsl_metadata

        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = (root / 'kitty/shaders.c').read_text()
        helper_start = source.index('static void\nprepare_pane_animation_target(')
        helper_end = source.index('\n#endif', helper_start)
        start = source.index('static void\ndraw_pane_animation(')
        end = source.index('\n#endif', start)
        # Compile the real generated shader variants, including their uniform
        # metadata: a handwritten fragment shader would hide sRGB regressions.
        shaders = 'static const char *shader_sources[2][2] = {\n'
        for variant in ('premult', 'srgb_premult'):
            shaders += '{' + ','.join(json.dumps(s) for s in glsl_shaders('graphics', variant)) + '},\n'
        shaders += '};\n'
        uniforms = load_glsl_metadata()['graphics']['loose_uniforms']
        uniform_names = 'static const char *uniform_names[][2] = {\n'
        uniform_names += ''.join('{' + json.dumps(k) + ',' + json.dumps(v) + '},\n' for k, v in uniforms.items())
        uniform_names += '};\n'
        prelude = r'''
#define GL_SILENCE_DEPRECATION
#include <OpenGL/OpenGL.h>
#include <OpenGL/gl3.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include "kitty/pane-animation.h"
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define arraysz(a) (sizeof(a) / sizeof((a)[0]))
#define OPT(x) options.x
#define GRAPHICS_UNIT 0
#define GRAPHICS_PREMULT_PROGRAM 0
#define GRAPHICS_SRGB_PREMULT_PROGRAM 1
typedef double monotonic_t;
typedef unsigned color_type;
typedef struct {color_type rgb;} DynamicColor;
typedef struct {DynamicColor default_bg;} Colors;
typedef struct {Colors overridden, configured;} ColorProfile;
typedef struct {ColorProfile *color_profile;} Screen;
typedef struct {unsigned long long id; struct {Screen *screen;} render_data;} Window;
typedef struct {unsigned vao_idx;} BorderRects;
typedef struct {BorderRects border_rects; unsigned num_windows; Window *windows;} Tab;
typedef struct {PaneAnimation pane_animation; int viewport_width, viewport_height;} OSWindow;
typedef struct {int left, top, right, bottom;} Region;
typedef struct {int left, top, width, height;} Viewport;
static struct {color_type background;} options = {0x193451};
static struct {bool supports_framebuffer_srgb;} global_state;
static GLuint programs[2], framebuffer, output_framebuffer;
static const int width = 160, height = 120;
static Region central;
static float opacity = 1;
static void require(bool ok, const char *message) {
    if (!ok) {fprintf(stderr, "%s\n", message); exit(1);}
}
static void fatal(const char *format, ...) {
    va_list args; va_start(args, format); vfprintf(stderr, format, args); va_end(args); exit(1);
}
static double monotonic_t_to_s_double(double t) {return t;}
static void free_texture(GLuint *t) {glDeleteTextures(1, t); *t = 0;}
static void free_framebuffer(GLuint *f) {glDeleteFramebuffers(1, f); *f = 0;}
static const char *check_framebuffer_status(void) {
    return glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE ? NULL : "incomplete framebuffer";
}
static void bind_vertex_array(GLuint vao) {glBindVertexArray(vao);}
static void unbind_vertex_array(void) {glBindVertexArray(0);}
static void bind_program(int p) {glUseProgram(programs[p]);}
static void unbind_program(void) {glUseProgram(0);}
'''
        prelude += uniform_names + shaders + r'''
static GLint program_uniform_location(int p, const char *name) {
    for (size_t i = 0; i < arraysz(uniform_names); i++)
        if (!strcmp(name, uniform_names[i][0])) return glGetUniformLocation(programs[p], uniform_names[i][1]);
    fatal("Unknown graphics uniform: %s", name); return -1;
}
static void set_framebuffer_to_use_for_output(GLuint f) {output_framebuffer = f;}
static void bind_framebuffer_for_output(GLuint f) {
    glBindFramebuffer(GL_FRAMEBUFFER, f ? f : (output_framebuffer ? output_framebuffer : framebuffer));
}
static void set_gpu_viewport(int w, int h) {glViewport(0, 0, w, h);}
static void draw_quad(bool blend, unsigned instances) {
    require(!blend && !instances, "unexpected pane compositor draw mode");
    glDisable(GL_BLEND); glBlendFunc(GL_ONE, GL_ZERO); glDrawArrays(GL_TRIANGLE_FAN, 0, 4);
}
static void os_window_regions(OSWindow *w, Region *region, Region *bar) {
    (void)w; (void)bar; *region = central;
}
static void enable_scissor_using_top_left_origin(Viewport v, unsigned h) {
    glEnable(GL_SCISSOR_TEST); glScissor(v.left, h - v.top - v.height, v.width, v.height);
}
static void disable_scissor(void) {glDisable(GL_SCISSOR_TEST);}
static float effective_os_window_alpha(OSWindow *w) {(void)w; return opacity;}
static DynamicColor colorprofile_to_color(ColorProfile *cp, DynamicColor value, DynamicColor fallback) {
    (void)cp; (void)fallback; return value;
}
'''
        scenario = r'''
static GLuint shader(GLenum type, const char *text) {
    GLuint s = glCreateShader(type); glShaderSource(s, 1, &text, NULL); glCompileShader(s);
    GLint ok; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {char log[2048]; glGetShaderInfoLog(s, sizeof(log), NULL, log); fprintf(stderr, "%s", log); exit(1);}
    return s;
}
static unsigned char source_pixels[160 * 120 * 4];
static void rgba(color_type rgb, float alpha, unsigned char *pixel) {
    pixel[0] = (unsigned char)round(((rgb >> 16) & 255) * alpha);
    pixel[1] = (unsigned char)round(((rgb >> 8) & 255) * alpha);
    pixel[2] = (unsigned char)round((rgb & 255) * alpha);
    pixel[3] = (unsigned char)round(255 * alpha);
}
static void paint_rect(int x, int y, int w, int h, color_type rgb, float alpha) {
    unsigned char pixel[4]; rgba(rgb, alpha, pixel);
    enable_scissor_using_top_left_origin((Viewport){x, y, w, h}, height);
    glClearColor(pixel[0] / 255.f, pixel[1] / 255.f, pixel[2] / 255.f, pixel[3] / 255.f);
    glClear(GL_COLOR_BUFFER_BIT); disable_scissor();
    for (int py = MAX(0, y); py < MIN(height, y + h); py++)
        for (int px = MAX(0, x); px < MIN(width, x + w); px++)
            memcpy(source_pixels + 4 * (py * width + px), pixel, 4);
}
static void paint_source(OSWindow *w, const color_type *backgrounds, int marker_offset) {
    bind_framebuffer_for_output(w->pane_animation.framebuffer_id);
    glDisable(GL_FRAMEBUFFER_SRGB);
    // Final content is already premultiplied and sRGB-encoded, as with the
    // production final-content pass. An sRGB sampler must decode and re-encode
    // these stored RGB channels without unpremultiplying or changing alpha.
    paint_rect(0, 0, width, height, 0xff0000, 1);
    for (size_t i = 0; i < w->pane_animation.count; i++) {
        PaneAnimationRect r = w->pane_animation.frames[i].target;
        paint_rect(r.x, r.y, r.width, r.height, backgrounds[i], opacity);
        paint_rect(r.x + marker_offset, r.y + 8, 6, 10, 0xffffff, 1);
        paint_rect(r.x + 2, r.y + 3, 5, 4, i ? 0x5b79c9 : 0x91b75d, opacity);
    }
    // Four distinct strips catch a missing restore, wrong origin or accidental
    // clearing of the tab bar and margins, including on freshly rendered frames.
    paint_rect(0, 0, width, central.top, 0x526a91, opacity);
    paint_rect(0, central.bottom, width, height - central.bottom, 0xa46c39, opacity);
    paint_rect(0, central.top, central.left, central.bottom - central.top, 0x388b74, opacity);
    paint_rect(central.right, central.top, width - central.right, central.bottom - central.top, 0x8564ab, opacity);
}
static void poison_output(void) {
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glDisable(GL_FRAMEBUFFER_SRGB); disable_scissor();
    glClearColor(1, 0, 1, 0); glClear(GL_COLOR_BUFFER_BIT);
}
static void check_pixels(OSWindow *w, const color_type *backgrounds, double t) {
    PaneAnimation *a = &w->pane_animation;
    unsigned char pixels[160 * 120 * 4];
    GLint bound; glGetIntegerv(GL_FRAMEBUFFER_BINDING, &bound);
    require((GLuint)bound == framebuffer, "compositor did not bind the output framebuffer");
    glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    Viewport presentations[2];
    require(a->count <= arraysz(presentations), "too many test panes");
    for (size_t i = 0; i < a->count; i++) {
        PaneAnimationRect p = pane_animation_sample(a->frames + i, t, a->duration);
        presentations[i] = (Viewport){round(p.x), round(p.y), round(p.width), round(p.height)};
    }
    int whites = 0, expected_whites = 0;
    for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
        unsigned char expected[4];
        memcpy(expected, source_pixels + 4 * (y * width + x), 4);
        if (x >= central.left && x < central.right && y >= central.top && y < central.bottom) {
            rgba(options.background, opacity, expected);
            // The chosen zoom pane must win all overlap from t=0, regardless
            // of its position in the layout's frame array.
            for (unsigned layer = 0; layer < 2; layer++) for (size_t i = 0; i < a->count; i++) {
                if ((a->frames[i].id == a->foreground_id) != (layer == 1)) continue;
                PaneAnimationFrame *f = a->frames + i;
                Viewport p = presentations[i];
                int dx = x - p.left, dy = y - p.top;
                if (dx < 0 || dy < 0 || dx >= p.width || dy >= p.height) continue;
                rgba(backgrounds[i], opacity, expected);
                int sx = (int)f->target.x + dx, sy = (int)f->target.y + dy;
                if (dx < f->target.width && dy < f->target.height && sx >= 0 && sx < width && sy >= 0 && sy < height)
                    memcpy(expected, source_pixels + 4 * (sy * width + sx), 4);
            }
        }
        unsigned char *v = pixels + 4 * ((height - 1 - y) * width + x);
        bool white = v[0] > 250 && v[1] > 250 && v[2] > 250;
        bool marker = expected[0] == 255 && expected[1] == 255 && expected[2] == 255;
        require(marker == white, "glyph pixels were scaled, filtered or clipped incorrectly");
        whites += white; expected_whites += marker;
        for (int channel = 0; channel < 4; channel++) if (abs(v[channel] - expected[channel]) > 2)
            fatal("Pixel (%d,%d) channel %d: got %u expected %u (sRGB=%d alpha=%.1f t=%.3f panes=%zu)\n",
                  x, y, channel, v[channel], expected[channel], global_state.supports_framebuffer_srgb, opacity, t, a->count);
        require(!(v[0] > 250 && v[1] < 5), "adjacent pane pixels leaked into animation");
    }
    require(whites == expected_whites, "incorrect unscaled glyph footprint");
    // Drawing a presentation frame must never overwrite the unscaled cache.
    bind_framebuffer_for_output(a->framebuffer_id);
    glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    for (int y = 0; y < height; y++)
        require(!memcmp(pixels + 4 * (y * width), source_pixels + 4 * ((height - 1 - y) * width), 4 * width),
                "compositor modified its source framebuffer");
    require(glGetError() == GL_NO_ERROR, "OpenGL error in pane compositor");
}
int main(void) {
    CGLPixelFormatAttribute attrs[] = {kCGLPFAOpenGLProfile, (CGLPixelFormatAttribute)kCGLOGLPVersion_3_2_Core,
        kCGLPFAColorSize, (CGLPixelFormatAttribute)24, kCGLPFAAlphaSize, (CGLPixelFormatAttribute)8, 0};
    CGLPixelFormatObj format; GLint count; CGLContextObj context;
    require(CGLChoosePixelFormat(attrs, &format, &count) == kCGLNoError, "no offscreen pixel format");
    require(CGLCreateContext(format, NULL, &context) == kCGLNoError, "no offscreen context");
    CGLDestroyPixelFormat(format); CGLSetCurrentContext(context);
    GLuint shader_ids[2][2];
    for (int p = 0; p < 2; p++) {
        programs[p] = glCreateProgram();
        shader_ids[p][0] = shader(GL_VERTEX_SHADER, shader_sources[p][0]);
        shader_ids[p][1] = shader(GL_FRAGMENT_SHADER, shader_sources[p][1]);
        glAttachShader(programs[p], shader_ids[p][0]); glAttachShader(programs[p], shader_ids[p][1]);
        glLinkProgram(programs[p]);
        GLint linked; glGetProgramiv(programs[p], GL_LINK_STATUS, &linked); require(linked, "shader link failed");
    }
    GLuint output, vao; glGenVertexArrays(1, &vao);
    glGenTextures(1, &output); glBindTexture(GL_TEXTURE_2D, output);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &framebuffer); glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, output, 0);
    require(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE, "incomplete offscreen framebuffer");
    glViewport(0, 0, width, height);
    const color_type backgrounds[] = {0x5387b3, 0xad6d39};
    ColorProfile profiles[] = {{.overridden.default_bg.rgb=0x5387b3}, {.overridden.default_bg.rgb=0xad6d39}};
    Screen screens[] = {{profiles}, {profiles + 1}};
    Window windows[] = {{.id=1, .render_data.screen=screens}, {.id=2, .render_data.screen=screens + 1}};
    Tab tab = {.border_rects.vao_idx=vao, .num_windows=2, .windows=windows};
    const PaneAnimationRect pairs[][2] = {
        {{10,10,120,90}, {20,20,60,45}}, // Exit Zoom.
        {{20,20,60,45}, {10,10,120,90}}, // Enter Zoom.
        {{-8,-4,120,90}, {20,20,60,45}}, // Destination clipping.
        {{20,20,100,80}, {-8,-4,60,45}}, // Source clipping.
        {{15,10,20,15}, {20,20,60,45}}, // Clip part of the glyph.
    };
    const double times[] = {0, .025, .08, .16, .35};
    for (unsigned srgb = 0; srgb < 2; srgb++) for (unsigned translucent = 0; translucent < 2; translucent++)
    for (unsigned c = 0; c <= arraysz(pairs) + 2; c++) {
        global_state.supports_framebuffer_srgb = srgb;
        opacity = translucent ? .5f : 1.f;
        bool two_panes = c >= arraysz(pairs);
        bool zoom_overlap = c > arraysz(pairs);
        central = two_panes ? (Region){6, 12, width - 7, height - 9} : (Region){0, 0, width, height};
        PaneAnimationFrame frames[2] = {
            {.id=1, .from=two_panes ? (PaneAnimationRect){8,16,58,80} : pairs[c][0],
             .target=two_panes ? (PaneAnimationRect){18,24,45,60} : pairs[c][1]},
            {.id=2, .from={90,20,45,60}, .target={94,28,48,70}},
        };
        if (zoom_overlap) {
            // Both presentations overlap at animation start, then separate.
            // Exercise a zoom pane both before and after its sibling in order.
            frames[0].from = frames[1].from = (PaneAnimationRect){8,16,130,90};
        }
        for (unsigned i = 0; i < arraysz(frames); i++)
            frames[i].position_spring = frames[i].size_spring = pane_animation_spring(pane_animation_damping(80, 6.8, 0));
        OSWindow w = {.viewport_width=width - 1, .viewport_height=height - 1,
            .pane_animation={.active=true, .frames=frames, .count=two_panes ? 2 : 1, .duration=.35,
                             .foreground_id=zoom_overlap ? (c == arraysz(pairs) + 1 ? 1 : 2) : 0}};
        // Exercise size-mismatch recreation using only small attachments.
        prepare_pane_animation_target(&w); w.pane_animation.content_valid = true;
        w.viewport_width = width; w.viewport_height = height;
        prepare_pane_animation_target(&w);
        require(!w.pane_animation.content_valid, "resizing retained stale cached content");
        GLuint texture = w.pane_animation.texture_id, target = w.pane_animation.framebuffer_id;
        glBindTexture(GL_TEXTURE_2D, texture);
        GLint format, tw, th;
        glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_INTERNAL_FORMAT, &format);
        glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, &tw);
        glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, &th);
        require(format == (srgb ? GL_SRGB8_ALPHA8 : GL_RGBA8) && tw == width && th == height,
                "incorrect cache texture format or dimensions");
        for (unsigned tick = 0; tick < arraysz(times) + 2; tick++) {
            bool refresh = tick == arraysz(times);
            double t = tick < arraysz(times) ? times[tick] : (refresh ? .04 : .07);
            w.pane_animation.reuse_content = tick != 0 && !refresh;
            prepare_pane_animation_target(&w);
            require(w.pane_animation.texture_id == texture && w.pane_animation.framebuffer_id == target,
                    "size-matched cache attachment was reallocated");
            if (!w.pane_animation.reuse_content) paint_source(&w, backgrounds, refresh ? 16 : 8);
            // Swapped output is undefined even on a live refresh. Route output
            // to the cache first, so the compositor must explicitly reset it.
            poison_output(); set_framebuffer_to_use_for_output(target);
            if (tick & 1) bind_framebuffer_for_output(0);
            bool enabled_srgb = (tick & 1) != 0;
            if (enabled_srgb) glEnable(GL_FRAMEBUFFER_SRGB);
            draw_pane_animation(&w, &tab, t);
            require((glIsEnabled(GL_FRAMEBUFFER_SRGB) != 0) == enabled_srgb, "sRGB state was not restored");
            require(w.pane_animation.content_valid, "fresh cache content was not marked valid");
            check_pixels(&w, backgrounds, t);
        }
        w.pane_animation.active = false;
        draw_pane_animation(&w, &tab, .35);
        require(!w.pane_animation.content_valid, "inactive animation retained valid cached content");
        free_framebuffer(&w.pane_animation.framebuffer_id); free_texture(&w.pane_animation.texture_id);
    }
    puts("Glyph clipping, two panes, midtone RGB, sRGB, alpha, outside strips, cache reuse and live refresh passed");
    glDeleteFramebuffers(1, &framebuffer); glDeleteTextures(1, &output); glDeleteVertexArrays(1, &vao);
    for (int p = 0; p < 2; p++) {
        glDeleteShader(shader_ids[p][0]); glDeleteShader(shader_ids[p][1]); glDeleteProgram(programs[p]);
    }
    CGLSetCurrentContext(NULL); CGLDestroyContext(context);
    return 0;
}
'''
        build_dir = root / 'build'
        build_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='pane-pixels-', dir=build_dir) as directory:
            probe, executable = Path(directory) / 'probe.c', Path(directory) / 'probe'
            probe.write_text(prelude + source[helper_start:helper_end] + source[start:end] + scenario)
            result = subprocess.run(['clang', '-O2', '-I', str(root), str(probe), '-framework', 'OpenGL', '-o', str(executable)],
                                    capture_output=True, text=True)
            self.ae(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.ae(result.returncode, 0, result.stdout + result.stderr)
