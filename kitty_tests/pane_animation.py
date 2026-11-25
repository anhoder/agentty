#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

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
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = (root / 'kitty/shaders.c').read_text()
        start = source.index('static void\ndraw_pane_animation(')
        end = source.index('\n#endif', start)
        prelude = r'''
#define GL_SILENCE_DEPRECATION
#include <OpenGL/OpenGL.h>
#include <OpenGL/gl3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "kitty/pane-animation.h"
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define OPT(x) options.x
#define GRAPHICS_UNIT 0
#define GRAPHICS_PREMULT_PROGRAM 0
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
static struct {color_type background;} options = {0x000000};
static GLuint program, framebuffer;
static const int width = 160, height = 120;
static float opacity = 1;
static void require(bool ok, const char *message) {
    if (!ok) {fprintf(stderr, "%s\n", message); exit(1);}
}
static double monotonic_t_to_s_double(double t) {return t;}
static void free_texture(GLuint *t) {glDeleteTextures(1, t); *t = 0;}
static void bind_vertex_array(GLuint vao) {glBindVertexArray(vao);}
static void unbind_vertex_array(void) {glBindVertexArray(0);}
static void bind_program(int unused) {(void)unused; glUseProgram(program);}
static void unbind_program(void) {glUseProgram(0);}
static GLint program_uniform_location(int unused, const char *name) {
    (void)unused; return glGetUniformLocation(program, name);
}
static void set_framebuffer_to_use_for_output(int unused) {(void)unused;}
static void bind_framebuffer_for_output(int unused) {(void)unused; glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);}
static void set_gpu_viewport(int w, int h) {glViewport(0, 0, w, h);}
static void draw_quad(bool unused, int unused2) {(void)unused; (void)unused2; glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);}
static void os_window_regions(OSWindow *w, Region *central, Region *bar) {
    (void)w; (void)bar; *central = (Region){0, 0, width, height};
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
static void clear_rect(int x, int y, int w, int h, float r, float g, float b, float a) {
    enable_scissor_using_top_left_origin((Viewport){x, y, w, h}, height);
    glClearColor(r, g, b, a); glClear(GL_COLOR_BUFFER_BIT); disable_scissor();
}
static void paint_source(PaneAnimationRect r, int offset) {
    // Red outside the source pane detects accidental sampling of adjacent panes.
    clear_rect(0, 0, width, height, 1, 0, 0, 1);
    clear_rect(r.x, r.y, r.width, r.height, 0, opacity, 0, opacity);
    clear_rect(r.x + offset, r.y + 8, 6, 10, 1, 1, 1, 1);
}
static void check_pixels(PaneAnimationFrame *f, double t, int marker_offset) {
    PaneAnimationRect p = pane_animation_sample(f, t, .35);
    unsigned char pixels[160 * 120 * 4];
    glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    // The glyph translates on the pixel grid, but never changes its 6x10 footprint.
    int dx = (int)round(p.x), dy = (int)round(p.y);
    int x0 = MAX(0, dx + marker_offset), y0 = MAX(0, dy + 8);
    int x1 = MIN(width, MIN(dx + marker_offset + 6, dx + (int)round(p.width)));
    int y1 = MIN(height, MIN(dy + 18, dy + (int)round(p.height)));
    int whites = 0;
    for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
        unsigned char *v = pixels + 4 * ((height - 1 - y) * width + x);
        bool marker = x >= x0 && x < x1 && y >= y0 && y < y1;
        bool white = v[0] > 250 && v[1] > 250 && v[2] > 250;
        require(marker == white, "glyph pixels were scaled, filtered or clipped incorrectly");
        whites += white;
        require(!(v[0] > 250 && v[1] < 5), "adjacent pane pixels leaked into animation");
    }
    require(whites == MAX(0, x1 - x0) * MAX(0, y1 - y0), "incorrect glyph footprint");
    int bx = MIN(width - 1, dx + (int)round(p.width) - 3), by = MAX(0, dy + 2);
    if (bx >= MAX(0, dx) && by < MIN(height, dy + (int)round(p.height))) {
        unsigned char *v = pixels + 4 * ((height - 1 - by) * width + bx);
        require(abs(v[1] - (int)round(255 * opacity)) <= 1 && abs(v[3] - (int)round(255 * opacity)) <= 1,
                "animated pane background or alpha was lost");
    }
    require(glGetError() == GL_NO_ERROR, "OpenGL error in pane compositor");
}
int main(void) {
    CGLPixelFormatAttribute attrs[] = {kCGLPFAOpenGLProfile, (CGLPixelFormatAttribute)kCGLOGLPVersion_3_2_Core,
        kCGLPFAColorSize, (CGLPixelFormatAttribute)24, kCGLPFAAlphaSize, (CGLPixelFormatAttribute)8, 0};
    CGLPixelFormatObj format; GLint count; CGLContextObj context;
    require(CGLChoosePixelFormat(attrs, &format, &count) == kCGLNoError, "no offscreen pixel format");
    require(CGLCreateContext(format, NULL, &context) == kCGLNoError, "no offscreen context");
    CGLDestroyPixelFormat(format); CGLSetCurrentContext(context);
    const char *vertex = "#version 150\nuniform vec4 src_rect,dest_rect;out vec2 uv;void main(){"
        "vec2 p=vec2(gl_VertexID&1,(gl_VertexID>>1)&1);uv=mix(src_rect.xy,src_rect.zw,p);"
        "gl_Position=vec4(mix(dest_rect.xy,dest_rect.zw,p),0,1);}";
    const char *fragment = "#version 150\nuniform sampler2D image;uniform float extra_alpha;in vec2 uv;out vec4 color;"
        "void main(){color=texture(image,uv)*extra_alpha;}";
    program = glCreateProgram();
    GLuint vs = shader(GL_VERTEX_SHADER, vertex), fs = shader(GL_FRAGMENT_SHADER, fragment);
    glAttachShader(program, vs); glAttachShader(program, fs); glLinkProgram(program);
    GLint linked; glGetProgramiv(program, GL_LINK_STATUS, &linked); require(linked, "shader link failed");
    GLuint output, vao; glGenVertexArrays(1, &vao);
    glGenTextures(1, &output); glBindTexture(GL_TEXTURE_2D, output);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &framebuffer); glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, output, 0);
    require(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE, "incomplete offscreen framebuffer");
    glViewport(0, 0, width, height);
    ColorProfile cp = {.overridden.default_bg.rgb=0x00ff00}; Screen screen = {&cp};
    Window window = {.id=1, .render_data.screen=&screen};
    Tab tab = {.border_rects.vao_idx=vao, .num_windows=1, .windows=&window};
    const PaneAnimationRect pairs[][2] = {
        {{10,10,120,90}, {20,20,60,45}}, // Exit Zoom.
        {{20,20,60,45}, {10,10,120,90}}, // Enter Zoom.
        {{-8,-4,120,90}, {20,20,60,45}}, // Destination clipping.
        {{20,20,100,80}, {-8,-4,60,45}}, // Source clipping.
        {{15,10,20,15}, {20,20,60,45}}, // Clip part of the glyph.
    };
    const double times[] = {0, .025, .08, .16, .35};
    for (unsigned c = 0; c < sizeof(pairs)/sizeof(pairs[0]); c++) for (unsigned translucent=0; translucent<2; translucent++) {
        opacity = translucent ? .5f : 1.f;
        PaneAnimationFrame frame = {.id=1, .from=pairs[c][0], .target=pairs[c][1]};
        frame.position_spring = frame.size_spring = pane_animation_spring(pane_animation_damping(80, 6.8, 0));
        OSWindow w = {.viewport_width=width, .viewport_height=height,
            .pane_animation={.active=true, .frames=&frame, .count=1, .duration=.35}};
        for (unsigned tick=0; tick<sizeof(times)/sizeof(times[0]); tick++) {
            w.pane_animation.reuse_content = tick != 0;
            if (!tick) paint_source(frame.target, 8);
            draw_pane_animation(&w, &tab, times[tick]); check_pixels(&frame, times[tick], 8);
        }
        // A content update during motion must refresh the unscaled source.
        paint_source(frame.target, 16); w.pane_animation.reuse_content=false;
        draw_pane_animation(&w, &tab, .04); check_pixels(&frame, .04, 16);
        free_texture(&w.pane_animation.texture_id);
    }
    puts("Unscaled glyph pixels, spring motion, clipping, live content, cached frames and alpha passed");
    glDeleteFramebuffers(1, &framebuffer); glDeleteTextures(1, &output); glDeleteVertexArrays(1, &vao);
    glDeleteShader(vs); glDeleteShader(fs); glDeleteProgram(program);
    CGLSetCurrentContext(NULL); CGLDestroyContext(context);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='pane-pixels-') as directory:
            probe, executable = Path(directory) / 'probe.c', Path(directory) / 'probe'
            probe.write_text(prelude + source[start:end] + scenario)
            result = subprocess.run(['clang', '-O2', '-I', str(root), str(probe), '-framework', 'OpenGL', '-o', str(executable)],
                                    capture_output=True, text=True)
            self.ae(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.ae(result.returncode, 0, result.stdout + result.stderr)
