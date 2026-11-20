// Exercise the real scheduler with a deterministic display clock and callback queue.
#define _GLFW_COCOA
#include "../glfw/internal.h"
#include <CoreVideo/CVDisplayLink.h>
#include <stdio.h>
#include <stdlib.h>
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
#pragma clang diagnostic ignored "-Wunused-parameter"

typedef struct { bool running; unsigned refs; } TestLink;
static unsigned creates, starts, stops, errors, queued, frames;
static monotonic_t clock_now = 1000000000ll;
static void (*dispatch_callback)(void *);
static void *dispatch_data;
static bool request_again;
static TestLink *test_link(CVDisplayLinkRef link) { return (TestLink *)link; }
static CVReturn test_create(CGDirectDisplayID display, CVDisplayLinkRef *out) {
    TestLink *link = calloc(1, sizeof(*link));
    link->refs = 1;
    *out = (CVDisplayLinkRef)link;
    creates++;
    return kCVReturnSuccess;
}
static CVReturn test_set_callback(CVDisplayLinkRef l, CVDisplayLinkOutputCallback c, void *data) { return kCVReturnSuccess; }
static bool test_running(CVDisplayLinkRef link) { return test_link(link)->running; }
static CVReturn test_start(CVDisplayLinkRef link) { test_link(link)->running = true; starts++; return kCVReturnSuccess; }
static CVReturn test_stop(CVDisplayLinkRef link) { test_link(link)->running = false; stops++; return kCVReturnSuccess; }
static CVDisplayLinkRef test_retain(CVDisplayLinkRef link) { test_link(link)->refs++; return link; }
static void test_release(CVDisplayLinkRef link) { if (!--test_link(link)->refs) free(test_link(link)); }
static void test_dispatch(dispatch_queue_t queue, void *data, dispatch_function_t callback) {
    queued++;
    dispatch_callback = callback;
    dispatch_data = data;
}
#define CVDisplayLinkCreateWithCGDisplay test_create
#define CVDisplayLinkSetOutputCallback test_set_callback
#define CVDisplayLinkIsRunning test_running
#define CVDisplayLinkStart test_start
#define CVDisplayLinkStop test_stop
#define CVDisplayLinkRetain test_retain
#define CVDisplayLinkRelease test_release
#define dispatch_async_f test_dispatch
#include "../glfw/cocoa_displaylink.m"

_GLFWlibrary _glfw;
monotonic_t glfwGetTime(void) { return clock_now; }
unsigned long long _glfwPlatformAddTimer(monotonic_t i, bool r, GLFWuserdatafun c, void *d, GLFWuserdatafun f) { return 1; }
void _glfwPlatformUpdateTimer(unsigned long long timer, monotonic_t interval, bool enabled) {}
void _glfwInputError(int code, const char *format, ...) { errors++; }

@interface TestDisplayWindow : NSObject
@end
@implementation TestDisplayWindow
- (NSScreen *)screen { return [NSScreen mainScreen]; }
@end

static void require(bool condition, const char *message) {
    if (!condition) { fprintf(stderr, "%s\n", message); exit(1); }
}
static void request(_GLFWwindow *w) { w->ns.renderFrameRequested = true; _glfwRequestRenderFrame(w); }
static void frame(GLFWwindow *window) { frames++; if (request_again) request((_GLFWwindow *)window); }
static void tick(void) { displayLinkCallback(NULL, NULL, NULL, 0, NULL, &displayLinks.entries[0]); }
static void drain(void) {
    void (*callback)(void *) = dispatch_callback;
    dispatch_callback = NULL;
    require(callback != NULL, "display tick must queue a render callback");
    callback(dispatch_data);
}
int main(void) { @autoreleasepool {
    TestDisplayWindow *native = [TestDisplayWindow new];
    _GLFWwindow w = {0};
    w.ns.object = native;
    w.ns.renderFrameCallback = frame;
    _glfw.windowListHead = &w;
    request(&w);
    require(creates == 1 && starts == 1, "first request starts one link");
    tick();
    clock_now += s_to_monotonic_t(2ll);
    request(&w);
    tick();
    require(creates == 1 && errors == 0, "queued main dispatch is not a stuck display link");
    require(queued == 1, "repeated requests must not queue duplicate dispatches");
    request_again = true;
    drain();
    require(frames == 1 && w.ns.renderFrameRequested, "callback can request next frame");
    tick();
    require(queued == 2, "callback request survives dispatch completion");
    request_again = false;
    drain();
    require(frames == 2 && stops == 1, "last frame stops unused link");
    request(&w);
    clock_now += s_to_monotonic_t(2ll);
    request(&w);
    require(creates == 2 && errors == 1, "missing display callback still triggers watchdog recovery");
    tick(); drain();
    require(frames == 3, "recreated link delivers requested frame");
    _glfwShutdownCVDisplayLink(0, NULL);
    request(&w); tick(); drain();
    require(creates == 2 && frames == 4, "idle shutdown restarts existing link");
    _glfwClearDisplayLinks();
    [native release];
    puts("display-link scheduling probe passed");
    return 0;
} }
