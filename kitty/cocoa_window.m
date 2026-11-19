/*
 * cocoa_window.m
 * Copyright (C) 2017 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */


#include "state.h"
#include "cleanup.h"
#include "cocoa_window.h"
#include "crypto.h"
#include <Availability.h>
#include <Carbon/Carbon.h>
#include <Cocoa/Cocoa.h>
#import <IOKit/IOKitLib.h>
#include <UserNotifications/UserNotifications.h>
#import <AudioToolbox/AudioServices.h>
#include <Security/Security.h>
#import <QuartzCore/QuartzCore.h>
#include "gl.h"
#include "glfw-wrapper.h"

#include <AvailabilityMacros.h>
// Needed for _NSGetProgname
#include <crt_externs.h>
#include <objc/runtime.h>

static inline void
cleanup_cfrelease(void *__p) {
    CFTypeRef *tp = (CFTypeRef *)__p;
    CFTypeRef cf = *tp;
    if (cf) { CFRelease(cf); }
}
#define RAII_CoreFoundation(type, name, initializer) __attribute__((cleanup(cleanup_cfrelease))) type name = initializer

#if (MAC_OS_X_VERSION_MAX_ALLOWED < 101300)
#define NSControlStateValueOn NSOnState
#define NSControlStateValueOff NSOffState
#define NSControlStateValueMixed NSMixedState
#endif
#if (MAC_OS_X_VERSION_MAX_ALLOWED < 101200)
#define NSWindowStyleMaskResizable NSResizableWindowMask
#define NSEventModifierFlagOption NSAlternateKeyMask
#define NSEventModifierFlagCommand NSCommandKeyMask
#define NSEventModifierFlagControl NSControlKeyMask
#endif
#if (MAC_OS_X_VERSION_MAX_ALLOWED < 110000)
#define UNNotificationPresentationOptionList (1 << 3)
#define UNNotificationPresentationOptionBanner (1 << 4)
#endif

typedef int CGSConnectionID;
typedef int CGSWindowID;
typedef int CGSWorkspaceID;
typedef enum _CGSSpaceSelector { kCGSSpaceCurrent = 5, kCGSSpaceAll = 7 } CGSSpaceSelector;
extern CGSConnectionID _CGSDefaultConnection(void);
CFArrayRef CGSCopySpacesForWindows(CGSConnectionID Connection, CGSSpaceSelector Type, CFArrayRef Windows);

static NSMenuItem *title_menu = NULL;

static const void *kitty_agents_sidebar_key = &kitty_agents_sidebar_key;
static const void *kitty_agents_sidebar_toggle_key = &kitty_agents_sidebar_toggle_key;
extern bool cocoa_dispatch_agents_sidebar_shortcut(void *, const GLFWkeyevent *);

// A transient native overlay leaves the OpenGL view and its input handling intact.
typedef struct {
    id_type id;
    CGRect rect;
} PaneFrame;

@interface KittyPaneAnimationView : NSView {
    PaneFrame *frames;
    size_t count, capacity;
    id_type tab_id;
    unsigned pixel_width, pixel_height;
    NSUInteger generation;
    NSMutableDictionary<NSNumber *, CALayer *> *panes;
    CALayer *backdrop;
}
- (void)updateForWindow:(OSWindow *)osw;
@end

static CGRect
pane_animation_rect(const Window *w) {
    const WindowGeometry *g = &w->render_data.geometry;
    const WindowRenderData *title = &w->window_title_render_data;
    CGFloat top = g->top, bottom = g->bottom;
    if (title->screen && title->geometry.bottom > title->geometry.top) {
        top = MIN(top, title->geometry.top);
        bottom = MAX(bottom, title->geometry.bottom);
    }
    CGFloat left = (CGFloat)g->left - g->spaces.left;
    top -= g->spaces.top;
    return CGRectMake(left, top, g->right + g->spaces.right - left, bottom + g->spaces.bottom - top);
}

static void
release_pane_snapshot(void *info UNUSED, const void *data, size_t size UNUSED) {
    free((void *)data);
}

static CGImageRef
pane_snapshot(OSWindow *osw) {
    size_t width = osw->viewport_width, height = osw->viewport_height;
    if (!width || !height || width > SIZE_MAX / 4 / height) return NULL;
    uint8_t *pixels = malloc(width * height * 4);
    if (!pixels) return NULL;
    GLint alignment, row_length, pack_buffer;
    glGetIntegerv(GL_PACK_ALIGNMENT, &alignment);
    glGetIntegerv(GL_PACK_ROW_LENGTH, &row_length);
    glGetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING, &pack_buffer);
    glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glPixelStorei(GL_PACK_ROW_LENGTH, 0);
    glReadPixels(0, 0, (GLsizei)width, (GLsizei)height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    glPixelStorei(GL_PACK_ALIGNMENT, alignment);
    glPixelStorei(GL_PACK_ROW_LENGTH, row_length);
    glBindBuffer(GL_PIXEL_PACK_BUFFER, (GLuint)pack_buffer);
    CGDataProviderRef provider = CGDataProviderCreateWithData(NULL, pixels, width * height * 4, release_pane_snapshot);
    if (!provider) {
        free(pixels);
        return NULL;
    }
    CGColorSpaceRef colors = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGImageRef image =
        CGImageCreate(width, height, 8, 32, width * 4, colors, (CGBitmapInfo)kCGImageAlphaPremultipliedLast, provider, NULL, false, kCGRenderingIntentDefault);
    CGColorSpaceRelease(colors);
    CGDataProviderRelease(provider);
    return image;
}

// Peak overshoot of an underdamped spring, as a fraction of the distance it
// travels, is exp(-z*pi / sqrt(1 - z*z)) for damping ratio z. For mass 1,
// stiffness f*f and critical damping 2*f, so damping is 2*z*f. Invert the
// overshoot formula to express the configured amplitude as a damping ratio.
static double
jelly_damping(double overshoot_fraction) {
    if (!(overshoot_fraction > 0)) return 2.0;
    if (overshoot_fraction > 0.999) overshoot_fraction = 0.999;
    double l = log(overshoot_fraction);
    return -2.0 * l / sqrt(M_PI * M_PI + l * l);
}

static double
jelly_overshoot_fraction(double travel, double percentage, double max_overshoot) {
    double fraction = percentage / 100.0;
    if (!(travel > 0) || !(max_overshoot > 0)) return fraction;
    return MIN(fraction, max_overshoot / travel);
}

static CASpringAnimation *
pane_spring(NSString *key, id from, id to, double duration, double overshoot_fraction) {
    CASpringAnimation *animation = [CASpringAnimation animationWithKeyPath:key];
    animation.mass = 1;
    double frequency = 12.0 / duration;
    animation.stiffness = frequency * frequency;
    animation.damping = jelly_damping(overshoot_fraction) * frequency;
    animation.fromValue = from;
    animation.toValue = to;
    animation.duration = duration;
    return animation;
}

@implementation KittyPaneAnimationView
- (instancetype)initWithFrame:(NSRect)frame {
    self = [super initWithFrame:frame];
    if (self)
        [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self
                                                           selector:@selector(motionPreferenceChanged:)
                                                               name:NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification
                                                             object:nil];
    return self;
}
- (void)motionPreferenceChanged:(NSNotification *)notification {
    (void)notification;
    if (NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion) [self stopAnimation];
}
- (BOOL)isFlipped {
    return YES;
}
- (NSView *)hitTest:(NSPoint)point {
    (void)point;
    return nil;
}
- (void)dealloc {
    [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self];
    free(frames);
    [panes release];
    [super dealloc];
}
- (void)stopAnimation {
    generation++;
    [self removeFromSuperview];
    [backdrop removeFromSuperlayer];
    backdrop = nil;
    for (CALayer *layer in panes.allValues) [layer removeFromSuperlayer];
    [panes removeAllObjects];
}
- (void)updateContentsFromImage:(CGImageRef)image forWindow:(OSWindow *)osw {
    // Replacing contents must not implicitly cross-fade or restart the springs.
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    Tab *tab = osw->tabs + osw->active_tab;
    for (unsigned i = 0; i < tab->num_windows; i++) {
        Window *w = tab->windows + i;
        CALayer *layer = panes[@(w->id)];
        if (!layer) continue;
        CGRect crop = CGRectIntersection(pane_animation_rect(w), CGRectMake(0, 0, osw->viewport_width, osw->viewport_height));
        if (CGRectIsEmpty(crop)) continue;
        // OpenGL rows are bottom-up; the image layer already flips them.
        crop.origin.y = osw->viewport_height - CGRectGetMaxY(crop);
        CGImageRef part = CGImageCreateWithImageInRect(image, crop);
        if (!part) continue;
        layer.contents = (id)part;
        CGImageRelease(part);
    }
    [CATransaction commit];
}
- (void)updateForWindow:(OSWindow *)osw {
    Tab *tab = osw->tabs + osw->active_tab;
    NSWindow *window = glfwGetCocoaWindow(osw->handle);
    NSView *content = window.contentView;
    double duration = OPT(window_jelly_duration);
    double bounce = OPT(window_jelly_bounce), max_bounce = OPT(window_jelly_bounce_max);
    BOOL dragging = global_state.active_drag_resize && os_window_for_kitty_window(global_state.active_drag_resize) == osw;
    BOOL enabled = OPT(window_jelly_enabled) && duration > 0 && !dragging && !osw->suppress_pane_animation &&
                   !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    osw->suppress_pane_animation = false;
    BOOL compatible = tab_id == tab->id && pixel_width == (unsigned)osw->viewport_width && pixel_height == (unsigned)osw->viewport_height;
    BOOL changed = NO;
    size_t visible = 0;
    for (unsigned i = 0; i < tab->num_windows; i++) {
        Window *w = tab->windows + i;
        if (!w->visible || !w->render_data.screen) continue;
        CGRect rect = pane_animation_rect(w);
        if (visible >= count || frames[visible].id != w->id || !CGRectEqualToRect(frames[visible].rect, rect)) changed = YES;
        visible++;
    }
    changed |= visible != count;
    if ((!enabled || !compatible || osw->live_resize.in_progress) && self.superview) [self stopAnimation];
    if (!changed && compatible) {
        // This hook runs after each actual terminal render, including cursor
        // trails/custom shaders. Keep the overlay live without extending its life.
        if (self.superview) {
            CGImageRef image = pane_snapshot(osw);
            if (image) {
                [self updateContentsFromImage:image forWindow:osw];
                CGImageRelease(image);
            } else [self stopAnimation];
        }
        return;
    }

    CGImageRef image = enabled && compatible && count && !osw->live_resize.in_progress ? pane_snapshot(osw) : NULL;
    if (image) {
        if (!panes) panes = [[NSMutableDictionary alloc] init];
        self.frame = content.bounds;
        self.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        self.wantsLayer = YES;
        self.layer.geometryFlipped = YES;
        self.layer.masksToBounds = YES;
        if (!self.superview) [content addSubview:self positioned:NSWindowAbove relativeTo:nil];
        CGFloat scale = window.backingScaleFactor;
        NSMutableDictionary<NSNumber *, CALayer *> *next = [NSMutableDictionary dictionary];
        [CATransaction begin];
        [CATransaction setDisableActions:YES];
        NSUInteger current_generation = ++generation;
        [CATransaction setCompletionBlock:^{
          if (generation == current_generation) [self stopAnimation];
        }];
        if (!backdrop) {
            backdrop = [CALayer layer];
            [self.layer insertSublayer:backdrop atIndex:0];
        }
        Region central = {0}, tab_bar = {0};
        os_window_regions(osw, &central, &tab_bar);
        backdrop.frame = CGRectMake(central.left / scale, central.top / scale, (central.right - central.left) / scale, (central.bottom - central.top) / scale);
        color_type bg = OPT(background);
        backdrop.backgroundColor = [NSColor colorWithSRGBRed:((bg >> 16) & 255) / 255.0
                                                       green:((bg >> 8) & 255) / 255.0
                                                        blue:(bg & 255) / 255.0
                                                       alpha:effective_os_window_alpha(osw)]
                                       .CGColor;
        for (unsigned i = 0; i < tab->num_windows; i++) {
            Window *w = tab->windows + i;
            if (!w->visible || !w->render_data.screen) continue;
            CGRect rect = pane_animation_rect(w);
            NSNumber *key = @(w->id);
            CALayer *layer = panes[key];
            CGRect previous = rect;
            BOOL existed = NO;
            for (size_t j = 0; j < count; j++)
                if (frames[j].id == w->id) {
                    previous = frames[j].rect;
                    existed = YES;
                    break;
                }
            CGRect target = CGRectMake(rect.origin.x / scale, rect.origin.y / scale, rect.size.width / scale, rect.size.height / scale);
            CGRect start = CGRectMake(previous.origin.x / scale, previous.origin.y / scale, previous.size.width / scale, previous.size.height / scale);
            if (layer.presentationLayer) start = layer.presentationLayer.frame;
            if (!layer) {
                layer = [CALayer layer];
                layer.affineTransform = CGAffineTransformMakeScale(1, -1);
                [self.layer addSublayer:layer];
                if (!existed) start = CGRectInset(target, target.size.width * 0.04, target.size.height * 0.04);
            }
            layer.contentsScale = scale;
            layer.frame = target;
            double position_bounce =
                MIN(jelly_overshoot_fraction(fabs(CGRectGetMidX(target) - CGRectGetMidX(start)), bounce, max_bounce),
                    jelly_overshoot_fraction(fabs(CGRectGetMidY(target) - CGRectGetMidY(start)), bounce, max_bounce));
            double bounds_bounce =
                MIN(jelly_overshoot_fraction(fabs(target.size.width - start.size.width), bounce, max_bounce),
                    jelly_overshoot_fraction(fabs(target.size.height - start.size.height), bounce, max_bounce));
            [layer addAnimation:pane_spring(
                                    @"position",
                                    [NSValue valueWithPoint:NSMakePoint(CGRectGetMidX(start), CGRectGetMidY(start))],
                                    [NSValue valueWithPoint:NSMakePoint(CGRectGetMidX(target), CGRectGetMidY(target))],
                                    duration,
                                    position_bounce)
                         forKey:@"pane-position"];
            [layer addAnimation:pane_spring(
                                    @"bounds",
                                    [NSValue valueWithRect:NSMakeRect(0, 0, start.size.width, start.size.height)],
                                    [NSValue valueWithRect:NSMakeRect(0, 0, target.size.width, target.size.height)],
                                    duration,
                                    bounds_bounce)
                         forKey:@"pane-bounds"];
            next[key] = layer;
        }
        for (NSNumber *key in panes)
            if (!next[key]) [panes[key] removeFromSuperlayer];
        [panes setDictionary:next];
        [self updateContentsFromImage:image forWindow:osw];
        [CATransaction commit];
        CGImageRelease(image);
    }
    if (visible > capacity) {
        capacity = MAX(visible, capacity * 2);
        frames = realloc(frames, capacity * sizeof(*frames));
        if (!frames) fatal("Out of memory storing pane animation geometry");
    }
    count = 0;
    for (unsigned i = 0; i < tab->num_windows; i++) {
        Window *w = tab->windows + i;
        if (w->visible && w->render_data.screen) frames[count++] = (PaneFrame){w->id, pane_animation_rect(w)};
    }
    tab_id = tab->id;
    pixel_width = osw->viewport_width;
    pixel_height = osw->viewport_height;
}
@end

void
cocoa_animate_pane_layout(void *ptr) {
    OSWindow *osw = ptr;
    NSWindow *window = glfwGetCocoaWindow(osw->handle);
    if (!window.contentView) return;
    static const void *key = &key;
    KittyPaneAnimationView *view = objc_getAssociatedObject(window, key);
    if (!view) {
        view = [[KittyPaneAnimationView alloc] initWithFrame:window.contentView.bounds];
        objc_setAssociatedObject(window, key, view, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        [view release];
    }
    [view updateForWindow:osw];
}

static void
draw_agents_sidebar_icon(NSRect rect) {
    CGFloat inset = MAX(1.0, MIN(rect.size.width, rect.size.height) * 0.12);
    NSRect outline_rect = NSInsetRect(rect, inset, inset);
    NSBezierPath *outline = [NSBezierPath bezierPathWithRoundedRect:outline_rect xRadius:2.0 yRadius:2.0];
    outline.lineWidth = 1.35;
    [outline stroke];
    CGFloat divider_x = NSMinX(outline_rect) + outline_rect.size.width * 0.34;
    NSBezierPath *divider = [NSBezierPath bezierPath];
    [divider moveToPoint:NSMakePoint(divider_x, NSMinY(outline_rect))];
    [divider lineToPoint:NSMakePoint(divider_x, NSMaxY(outline_rect))];
    [divider stroke];
    CGFloat line_start = divider_x + 2.5;
    CGFloat line_end = NSMaxX(outline_rect) - 2.5;
    for (NSUInteger i = 0; i < 3; i++) {
        CGFloat y = NSMinY(outline_rect) + 4.0 + i * 3.0;
        NSBezierPath *line = [NSBezierPath bezierPath];
        [line moveToPoint:NSMakePoint(line_start, y)];
        [line lineToPoint:NSMakePoint(line_end, y)];
        [line stroke];
    }
}


@interface KittyAgentsSidebarToggleButton : NSButton {
    NSTrackingArea *hover_tracking_area;
    BOOL hovered;
}
@end
@implementation KittyAgentsSidebarToggleButton
- (BOOL)acceptsFirstMouse:(NSEvent *)event {
    (void)event;
    return YES;
}

// AppKit draws a blue focus ring around a keyboard-focusable titlebar button.
// This control is mouse-only, so the ring is pure visual noise next to the
// traffic lights.
- (NSFocusRingType)focusRingType {
    return NSFocusRingTypeNone;
}

- (BOOL)canBecomeKeyView {
    return NO;
}

- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (hover_tracking_area) {
        [self removeTrackingArea:hover_tracking_area];
        [hover_tracking_area release];
    }
    NSTrackingAreaOptions options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect;
    hover_tracking_area = [[NSTrackingArea alloc] initWithRect:self.bounds options:options owner:self userInfo:nil];
    [self addTrackingArea:hover_tracking_area];
}

- (void)resetCursorRects {
    [super resetCursorRects];
    [self addCursorRect:self.bounds cursor:[NSCursor pointingHandCursor]];
}

- (void)mouseEntered:(NSEvent *)event {
    (void)event;
    hovered = YES;
    [self setNeedsDisplay:YES];
}

- (void)mouseExited:(NSEvent *)event {
    (void)event;
    hovered = NO;
    [self setNeedsDisplay:YES];
}

- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    BOOL expanded = self.state == NSControlStateValueOn;
    NSRect rect = NSInsetRect(self.bounds, 0.5, 0.5);
    NSBezierPath *background = [NSBezierPath bezierPathWithRoundedRect:rect xRadius:7.0 yRadius:7.0];
    if (expanded || hovered || self.highlighted) {
        CGFloat alpha = expanded ? (hovered ? 0.32 : 0.24) : (hovered ? 0.24 : 0.35);
        [[[NSColor controlBackgroundColor] colorWithAlphaComponent:alpha] setFill];
        [background fill];
    }

    NSColor *foreground = expanded ? [NSColor labelColor] : [NSColor secondaryLabelColor];
    [foreground setStroke];
    draw_agents_sidebar_icon(NSMakeRect(5.0, (self.bounds.size.height - 18.0) / 2.0, 18.0, 18.0));
    CGFloat center_x = self.bounds.size.width - 10.0;
    CGFloat center_y = NSMidY(self.bounds);
    CGFloat direction = expanded ? -1.0 : 1.0;
    NSBezierPath *chevron = [NSBezierPath bezierPath];
    chevron.lineWidth = 1.5;
    chevron.lineCapStyle = NSLineCapStyleRound;
    chevron.lineJoinStyle = NSLineJoinStyleRound;
    [chevron moveToPoint:NSMakePoint(center_x - direction * 2.0, center_y - 3.5)];
    [chevron lineToPoint:NSMakePoint(center_x + direction * 2.0, center_y)];
    [chevron lineToPoint:NSMakePoint(center_x - direction * 2.0, center_y + 3.5)];
    [chevron stroke];
}

- (void)dealloc {
    [hover_tracking_area release];
    [super dealloc];
}
@end

static void
agents_sidebar_update_toggle_state(NSButton *button, BOOL visible) {
    if (!button) return;
    NSString *verb = visible ? @"Hide" : @"Show";
    button.toolTip = [NSString stringWithFormat:@"%@ Agents sidebar", verb];
    button.accessibilityValue = visible ? @"Visible" : @"Hidden";
    button.accessibilityHelp = [NSString stringWithFormat:@"%@ the Agents sidebar", verb];
    button.state = visible ? NSControlStateValueOn : NSControlStateValueOff;
    [button setNeedsDisplay:YES];
}

static void
agents_sidebar_toggle_button_pressed(id sender) {
    (void)sender;
    set_cocoa_pending_action(AGENTS_SIDEBAR_TOGGLE, NULL);
}

static NSButton *
agents_sidebar_toggle_button(NSWindow *window) {
    if (!window) return nil;
    NSButton *button = objc_getAssociatedObject(window, kitty_agents_sidebar_toggle_key);
    if (button) return button;
    if (!(window.styleMask & NSWindowStyleMaskTitled)) return nil;
    if (![window standardWindowButton:NSWindowZoomButton]) return nil;
    button = [KittyAgentsSidebarToggleButton buttonWithTitle:@"Agents sidebar" target:window action:@selector(agentsSidebarToggleFromTitlebar:)];
    button.bordered = NO;
    button.frame = NSMakeRect(0, 0, 44.0, 28.0);
    button.identifier = @"kitty-agents-sidebar-toggle";
    button.accessibilityRole = NSAccessibilityButtonRole;
    button.accessibilityLabel = @"Agents sidebar";
    // Let AppKit reserve title space instead of overlaying its title view.
    NSTitlebarAccessoryViewController *accessory = [[NSTitlebarAccessoryViewController alloc] init];
    accessory.view = button;
    accessory.layoutAttribute = NSLayoutAttributeLeft;
    [window addTitlebarAccessoryViewController:accessory];
    [accessory release];
    agents_sidebar_update_toggle_state(button, NO);
    objc_setAssociatedObject(window, kitty_agents_sidebar_toggle_key, button, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    return button;
}

void
cocoa_initialize_agents_sidebar_toggle(void *ptr) {
    NSWindow *window = (NSWindow *)ptr;
    NSButton *button = agents_sidebar_toggle_button(window);
    button.hidden = NO;
    agents_sidebar_update_toggle_state(button, NO);
}

@interface NSWindow (KittyAgentsSidebarToggle)
- (void)agentsSidebarToggleFromTitlebar:(id)sender;
@end

@implementation NSWindow (KittyAgentsSidebarToggle)
- (void)agentsSidebarToggleFromTitlebar:(id)sender {
    (void)sender;
    agents_sidebar_toggle_button_pressed(self);
}
@end
@class KittyAgentsSidebarView;

@interface KittyAgentsOutlineView : NSOutlineView
@property BOOL keyboardFocusRequested;
@end

@interface KittyAgentsScrollView : NSScrollView
@end
@interface KittyAgentsSidebarCell : NSTableCellView
@property(assign) KittyAgentsSidebarView *sidebar;
@property(retain) id node;
@end

@interface KittyAgentsSidebarRowView : NSTableRowView {
    NSTrackingArea *hover_tracking_area;
}
@property(assign) KittyAgentsSidebarView *sidebar;
@property BOOL activeAgent;
@property BOOL groupHeading;
@property BOOL pointerHovered;
@end


@interface KittyAgentsSidebarView : NSView <NSOutlineViewDataSource, NSOutlineViewDelegate> {
    unsigned os_window_id;
    NSArray<NSNumber *> *window_ids;
    // NSOutlineView owns selection; IDs survive tracker refreshes and reordering.
    NSArray<NSString *> *groups, *group_keys, *agents, *labels, *details;
    NSArray<NSNumber *> *states, *active_rows;
    NSArray *root_nodes;
    NSDictionary *group_children;
    NSDictionary *row_indices;
    NSScrollView *scroll_view;
    KittyAgentsOutlineView *outline;
    NSTextField *summary;
    NSVisualEffectView *background_view;
    NSHashTable *previous_responders;
    id remembered_selection;
    BOOL syncing_outline, resizing, resize_hovered;
    BOOL has_entered_navigation;
    unsigned cell_width_pixels;
    NSMutableSet<NSString *> *collapsed_groups;
    NSTimer *spinner_timer;
    CGFloat spinner_phase;
    NSTrackingArea *resize_tracking_area;
}
@property(copy) NSString *grouping;
@property BOOL resizePending;
@property(copy) NSString *iconsDir;
- (instancetype)initWithOSWindowID:(unsigned)window_id width:(CGFloat)width cellWidth:(unsigned)cell_width;
- (void)setRows:(const unsigned long long *)new_window_ids
         groups:(const char **)new_groups
      groupKeys:(const char **)new_group_keys
         agents:(const char **)new_agents
         labels:(const char **)new_labels
        details:(const char **)new_details
         states:(const unsigned char *)new_states
         active:(const bool *)active
          count:(size_t)count;
- (NSVisualEffectView *)backgroundView;
- (void)stopAnimating;
- (BOOL)isAnimating;
- (BOOL)kittySidebarHasKeyboardFocus;
- (void)kittySidebarCancelNavigation;
- (BOOL)kittySidebarHandleKeyEvent:(NSEvent *)event;
- (void)focusSidebar;
- (void)drawNode:(id)node inRect:(NSRect)rect;
- (NSString *)labelForNode:(id)node;
- (NSMenu *)menuForNode:(id)node;
- (void)rebuildOutline;
- (void)setGroup:(id)group expanded:(BOOL)expanded animated:(BOOL)animated;
+ (NSMutableSet<NSString *> *)sharedCollapsedGroups;
+ (void)setSharedCollapsedGroups:(const char **)keys count:(size_t)count;
@end

@implementation KittyAgentsOutlineView
// Mouse activation returns to the terminal; only explicit keyboard navigation
// may acquire sidebar focus and show its accent border.
- (BOOL)acceptsFirstResponder { return self.keyboardFocusRequested; }
- (BOOL)becomeFirstResponder {
    BOOL accepted = [super becomeFirstResponder];
    [self setNeedsDisplay:YES];
    return accepted;
}
- (BOOL)resignFirstResponder {
    BOOL accepted = [super resignFirstResponder];
    if (accepted) self.keyboardFocusRequested = NO;
    [self setNeedsDisplay:YES];
    return accepted;
}
- (NSRect)frameOfCellAtColumn:(NSInteger)column row:(NSInteger)row {
    NSRect rect = [super frameOfCellAtColumn:column row:row];
    CGFloat left = [[self itemAtRow:row] isKindOfClass:[NSString class]] ? 0 : 6;
    rect.origin.x = left;
    CGFloat right = left == 0 ? 0 : 6;
    rect.size.width = MAX(0, self.enclosingScrollView.contentSize.width - left - right);
    return rect;
}
- (NSRect)frameOfOutlineCellAtRow:(NSInteger)row {
    // Keep native expansion semantics while the group cell draws its own chevron.
    (void)row;
    return NSZeroRect;
}
- (void)cursorUpdate:(NSEvent *)event {
    (void)event;
    [[NSCursor pointingHandCursor] set];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    [self addCursorRect:self.visibleRect cursor:[NSCursor pointingHandCursor]];
}
- (void)keyDown:(NSEvent *)event {
    // Unknown text must never bubble through the terminal content view.
    NSView *sidebar = self.superview;
    while (sidebar && ![sidebar isKindOfClass:[KittyAgentsSidebarView class]]) sidebar = sidebar.superview;
    [(KittyAgentsSidebarView *)sidebar kittySidebarHandleKeyEvent:event];
}
- (void)keyUp:(NSEvent *)event { (void)event; }
- (NSMenu *)menuForEvent:(NSEvent *)event {
    NSInteger row = [self rowAtPoint:[self convertPoint:event.locationInWindow fromView:nil]];
    if (row >= 0) [self selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
    return [(KittyAgentsSidebarView *)self.delegate menuForNode:row >= 0 ? [self itemAtRow:row] : nil];
}
@end

@implementation KittyAgentsScrollView
- (void)cursorUpdate:(NSEvent *)event {
    (void)event;
    [[NSCursor pointingHandCursor] set];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    [self addCursorRect:self.visibleRect cursor:[NSCursor pointingHandCursor]];
}
@end
@implementation KittyAgentsSidebarCell
- (BOOL)isFlipped { return YES; }
- (void)resetCursorRects {
    [super resetCursorRects];
    [self addCursorRect:self.visibleRect cursor:[NSCursor pointingHandCursor]];
}
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    [self.sidebar drawNode:self.node inRect:self.bounds];
}
- (BOOL)isAccessibilityElement { return YES; }
- (NSString *)accessibilityLabel { return [self.sidebar labelForNode:self.node]; }
- (NSString *)accessibilityRole { return NSAccessibilityStaticTextRole; }
- (void)dealloc {
    [_node release];
    [super dealloc];
}
@end

@implementation KittyAgentsSidebarRowView
- (NSRect)decorationRect {
    NSRect rect = self.bounds;
    rect.size.width = MIN(rect.size.width, self.enclosingScrollView.contentSize.width);
    return self.groupHeading ? rect : NSInsetRect(rect, 6, 2);
}
- (void)drawBackgroundInRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    BOOL highlighted = self.pointerHovered || (self.selected && [self.sidebar kittySidebarHasKeyboardFocus]);
    if (self.activeAgent) {
        [[NSColor selectedContentBackgroundColor] setFill];
        [[NSBezierPath bezierPathWithRoundedRect:[self decorationRect] xRadius:6 yRadius:6] fill];
    } else if (self.groupHeading || highlighted) {
        CGFloat alpha = highlighted ? (self.groupHeading ? 0.52 : 0.38) : 0.34;
        [[[NSColor controlBackgroundColor] colorWithAlphaComponent:alpha] setFill];
        if (self.groupHeading) NSRectFill([self decorationRect]);
        else [[NSBezierPath bezierPathWithRoundedRect:[self decorationRect] xRadius:6 yRadius:6] fill];
    }
}
- (void)drawSelectionInRect:(NSRect)dirtyRect {
    // Selection shares the background's hover fill; suppress AppKit's default decoration.
    (void)dirtyRect;
}
- (void)setSelected:(BOOL)selected {
    [super setSelected:selected];
    [self setNeedsDisplay:YES];
}
- (void)viewDidMoveToWindow {
    [super viewDidMoveToWindow];
    [self updateTrackingAreas];
}
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (hover_tracking_area) {
        [self removeTrackingArea:hover_tracking_area];
        [hover_tracking_area release];
    }
    hover_tracking_area = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingCursorUpdate | NSTrackingActiveAlways | NSTrackingInVisibleRect
        owner:self userInfo:nil];
    [self addTrackingArea:hover_tracking_area];
    NSPoint point = [self convertPoint:self.window.mouseLocationOutsideOfEventStream fromView:nil];
    self.pointerHovered = self.window && NSPointInRect(point, self.visibleRect);
    [self setNeedsDisplay:YES];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    [self addCursorRect:self.visibleRect cursor:[NSCursor pointingHandCursor]];
}
- (void)mouseEntered:(NSEvent *)event {
    (void)event;
    self.pointerHovered = YES;
    [self setNeedsDisplay:YES];
    [[NSCursor pointingHandCursor] set];
}
- (void)mouseMoved:(NSEvent *)event { [self mouseEntered:event]; }
- (void)cursorUpdate:(NSEvent *)event { [self mouseEntered:event]; }
- (void)mouseExited:(NSEvent *)event {
    (void)event;
    self.pointerHovered = NO;
    [self setNeedsDisplay:YES];
    [[NSCursor arrowCursor] set];
}
- (void)dealloc {
    if (hover_tracking_area) [self removeTrackingArea:hover_tracking_area];
    [hover_tracking_area release];
    [super dealloc];
}
@end


@implementation KittyAgentsSidebarView
- (instancetype)initWithOSWindowID:(unsigned)window_id width:(CGFloat)width cellWidth:(unsigned)cell_width {
    self = [super initWithFrame:NSMakeRect(0, 0, width, 100)];
    if (self) {
        os_window_id = window_id;
        cell_width_pixels = MAX(1u, cell_width);
        collapsed_groups = [[KittyAgentsSidebarView sharedCollapsedGroups] retain];
        previous_responders = [[NSHashTable weakObjectsHashTable] retain];
        [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(sharedGroupStateChanged:)
                                                   name:@"KittyAgentsSidebarGroupStateChanged" object:nil];
        [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(windowResignedKey:)
                                                   name:NSWindowDidResignKeyNotification object:nil];
        NSVisualEffectView *effect = [[NSVisualEffectView alloc] initWithFrame:self.bounds];
        effect.material = NSVisualEffectMaterialSidebar;
        effect.blendingMode = NSVisualEffectBlendingModeBehindWindow;
        effect.state = NSVisualEffectStateActive;
        background_view = effect;
        summary = [[NSTextField labelWithString:@"No AI agents"] retain];
        summary.font = [NSFont boldSystemFontOfSize:11];
        summary.alignment = NSTextAlignmentCenter;
        summary.textColor = [NSColor secondaryLabelColor];
        summary.lineBreakMode = NSLineBreakByTruncatingTail;
        [self addSubview:summary];
        scroll_view = [[KittyAgentsScrollView alloc] initWithFrame:NSZeroRect];
        scroll_view.drawsBackground = NO;
        scroll_view.hasVerticalScroller = YES;
        scroll_view.autohidesScrollers = YES;
        scroll_view.scrollerStyle = NSScrollerStyleOverlay;
        scroll_view.borderType = NSNoBorder;
        outline = [[KittyAgentsOutlineView alloc] initWithFrame:NSZeroRect];
        NSTableColumn *column = [[[NSTableColumn alloc] initWithIdentifier:@"agent"] autorelease];
        column.minWidth = 0;
        column.resizingMask = NSTableColumnAutoresizingMask;
        [outline addTableColumn:column];
        outline.outlineTableColumn = column;
        outline.headerView = nil;
        outline.backgroundColor = [NSColor clearColor];
        outline.indentationPerLevel = 0;
        outline.autoresizingMask = NSViewWidthSizable;
        if (@available(macOS 11.0, *)) outline.style = NSTableViewStylePlain;
        outline.rowHeight = 48;
        outline.usesAutomaticRowHeights = NO;
        outline.intercellSpacing = NSMakeSize(0, 0);
        outline.columnAutoresizingStyle = NSTableViewLastColumnOnlyAutoresizingStyle;
        outline.allowsEmptySelection = YES;
        outline.allowsMultipleSelection = NO;
        outline.allowsTypeSelect = NO;
        outline.focusRingType = NSFocusRingTypeNone;
        outline.accessibilityLabel = @"AI agents";
        outline.dataSource = self;
        outline.delegate = self;
        outline.target = self;
        outline.action = @selector(outlineClicked:);
        scroll_view.documentView = outline;
        [self addSubview:scroll_view];
        self.identifier = @"kitty-agents-sidebar";
        self.autoresizingMask = NSViewHeightSizable;
        [self setFrameSize:self.frame.size];
    }
    return self;
}

- (BOOL)isFlipped {
    return YES;
}
- (BOOL)isOpaque {
    return NO;
}
- (NSVisualEffectView *)backgroundView {
    return background_view;
}

- (NSView *)hitTest:(NSPoint)point {
    NSPoint local = [self convertPoint:point fromView:self.superview];
    if (!NSPointInRect(local, self.bounds)) return nil;
    if (local.x >= self.bounds.size.width - 8) return self;
    return [super hitTest:point];
}

- (BOOL)acceptsFirstMouse:(NSEvent *)event {
    (void)event;
    return YES;
}

- (void)setFrameSize:(NSSize)size {
    [super setFrameSize:size];
    [background_view setFrame:self.frame];
    const CGFloat title_height = summary.fittingSize.height;
    summary.frame = NSMakeRect(12, (39 - title_height) / 2, MAX(0, size.width - 24), title_height);
    scroll_view.frame = NSMakeRect(0, 40, MAX(0, size.width), MAX(0, size.height - 40));
    outline.tableColumns.firstObject.width = MAX(0, scroll_view.contentSize.width);
    [self.window invalidateCursorRectsForView:self];
    [self setNeedsDisplay:YES];
}

- (void)previewSizeWidth:(CGFloat)width {
    NSRect frame = self.frame;
    frame.size.width = width;
    frame.size.height = self.superview ? self.superview.bounds.size.height : frame.size.height;
    frame.origin.y = 0;
    [self setFrame:frame];
    [background_view setFrame:frame];
    [self resetCursorRects];
    [self setNeedsDisplay:YES];
}

- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (resize_tracking_area) {
        [self removeTrackingArea:resize_tracking_area];
        [resize_tracking_area release];
    }
    NSRect rail = NSMakeRect(MAX(0, self.bounds.size.width - 8), 0, MIN(8, self.bounds.size.width), self.bounds.size.height);
    resize_tracking_area = [[NSTrackingArea alloc] initWithRect:rail
        options:NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingCursorUpdate | NSTrackingActiveAlways | NSTrackingEnabledDuringMouseDrag
        owner:self userInfo:nil];
    [self addTrackingArea:resize_tracking_area];
}
- (void)cursorUpdate:(NSEvent *)event {
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    if (point.x >= self.bounds.size.width - 8) [[NSCursor resizeLeftRightCursor] set];
    else [[NSCursor pointingHandCursor] set];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    const CGFloat edge = 8.0;
    if (self.bounds.size.width > edge) {
        [self addCursorRect:NSMakeRect(self.bounds.size.width - edge, 0, edge, self.bounds.size.height) cursor:[NSCursor resizeLeftRightCursor]];
        [self addCursorRect:NSMakeRect(0, 0, self.bounds.size.width - edge, self.bounds.size.height) cursor:[NSCursor pointingHandCursor]];
    }
}


+ (NSMutableSet<NSString *> *)sharedCollapsedGroups {
    static NSMutableSet<NSString *> *shared;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ shared = [[NSMutableSet alloc] init]; });
    return shared;
}


+ (void)setSharedCollapsedGroups:(const char **)keys count:(size_t)count {
    NSMutableSet<NSString *> *collapsed = [self sharedCollapsedGroups];
    [collapsed removeAllObjects];
    for (size_t i = 0; i < count; i++) {
        if (keys[i] && *keys[i]) [collapsed addObject:[NSString stringWithUTF8String:keys[i]]];
    }
}


- (void)sharedGroupStateChanged:(NSNotification *)notification {
    if (notification.object != self) [self rebuildOutline];
}

- (id)selectedNode {
    return outline.selectedRow >= 0 ? [outline itemAtRow:outline.selectedRow] : nil;
}

- (void)rebuildOutline {
    id selected = [remembered_selection retain];
    NSPoint anchor = scroll_view.contentView.bounds.origin;
    syncing_outline = YES;
    [outline reloadData];
    for (id node in root_nodes) {
        if (![node isKindOfClass:[NSString class]]) continue;
        if ([collapsed_groups containsObject:node]) [outline collapseItem:node];
        else [outline expandItem:node];
    }
    [outline noteHeightOfRowsWithIndexesChanged:[NSIndexSet indexSetWithIndexesInRange:NSMakeRange(0, (NSUInteger)outline.numberOfRows)]];
    NSInteger selected_row = -1;
    for (NSInteger i = 0; i < outline.numberOfRows; i++) {
        if ([[outline itemAtRow:i] isEqual:selected]) { selected_row = i; break; }
    }
    if (selected_row >= 0) [outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)selected_row] byExtendingSelection:NO];
    else [outline deselectAll:nil];
    syncing_outline = NO;
    [scroll_view.contentView scrollToPoint:anchor];
    [scroll_view reflectScrolledClipView:scroll_view.contentView];
    [selected release];
}

- (void)setGroup:(id)group expanded:(BOOL)expanded animated:(BOOL)animated {
    if ([outline isItemExpanded:group] == expanded) return;
    BOOL animate = animated && self.window.isVisible && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    [NSAnimationContext beginGrouping];
    NSAnimationContext.currentContext.duration = animate ? 0.18 : 0;
    NSAnimationContext.currentContext.timingFunction = [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionEaseOut];
    if (expanded) [(animate ? outline.animator : outline) expandItem:group];
    else [(animate ? outline.animator : outline) collapseItem:group];
    [NSAnimationContext endGrouping];
    NSInteger row = [outline rowForItem:group];
    if (row >= 0) [[outline viewAtColumn:0 row:row makeIfNecessary:NO] setNeedsDisplay:YES];
}

- (void)revealRow:(NSInteger)row {
    [outline layoutSubtreeIfNeeded];
    NSRect rect = [outline rectOfRow:row];
    NSRect viewport = scroll_view.contentView.bounds;
    if (NSMinY(rect) < NSMinY(viewport)) viewport.origin.y = NSMinY(rect);
    else if (NSMaxY(rect) > NSMaxY(viewport)) viewport.origin.y = NSMaxY(rect) - viewport.size.height;
    [NSAnimationContext beginGrouping];
    NSAnimationContext.currentContext.duration = 0;
    [scroll_view.contentView scrollToPoint:viewport.origin];
    [scroll_view reflectScrolledClipView:scroll_view.contentView];
    [NSAnimationContext endGrouping];
}
- (NSInteger)outlineView:(NSOutlineView *)view numberOfChildrenOfItem:(id)item {
    (void)view;
    return (NSInteger)(item ? [group_children[item] count] : root_nodes.count);
}
- (id)outlineView:(NSOutlineView *)view child:(NSInteger)index ofItem:(id)item {
    (void)view;
    return item ? group_children[item][(NSUInteger)index] : root_nodes[(NSUInteger)index];
}
- (BOOL)outlineView:(NSOutlineView *)view isItemExpandable:(id)item {
    (void)view;
    return [item isKindOfClass:[NSString class]];
}
- (CGFloat)outlineView:(NSOutlineView *)view heightOfRowByItem:(id)item {
    (void)view;
    return [item isKindOfClass:[NSString class]] ? 24 : 48;
}
- (NSView *)outlineView:(NSOutlineView *)view viewForTableColumn:(NSTableColumn *)column item:(id)item {
    (void)column;
    KittyAgentsSidebarCell *cell = [view makeViewWithIdentifier:@"agent-cell" owner:self];
    if (!cell) {
        cell = [[[KittyAgentsSidebarCell alloc] initWithFrame:NSZeroRect] autorelease];
        cell.identifier = @"agent-cell";
    }
    cell.sidebar = self;
    cell.node = item;
    cell.toolTip = [self labelForNode:item];
    [cell setNeedsDisplay:YES];
    return cell;
}
- (NSTableRowView *)outlineView:(NSOutlineView *)view rowViewForItem:(id)item {
    (void)view;
    KittyAgentsSidebarRowView *row = [[[KittyAgentsSidebarRowView alloc] initWithFrame:NSZeroRect] autorelease];
    row.sidebar = self;
    row.groupHeading = [item isKindOfClass:[NSString class]];
    NSNumber *index = row_indices[item];
    row.activeAgent = index && [active_rows[index.unsignedIntegerValue] boolValue];
    return row;
}
- (void)outlineViewSelectionDidChange:(NSNotification *)notification {
    (void)notification;
    if (syncing_outline) return;
    [remembered_selection release];
    remembered_selection = [[self selectedNode] retain];
}
- (void)groupChanged:(NSNotification *)notification collapsed:(BOOL)collapsed {
    if (syncing_outline) return;
    NSString *key = notification.userInfo[@"NSObject"];
    if (![key isKindOfClass:[NSString class]]) return;
    if (collapsed) [collapsed_groups addObject:key];
    else [collapsed_groups removeObject:key];
    NSString *encoded = [[key dataUsingEncoding:NSUTF8StringEncoding] base64EncodedStringWithOptions:0];
    NSString *payload = [NSString stringWithFormat:@"%u:%d:%@", os_window_id, collapsed ? 1 : 0, encoded];
    set_cocoa_pending_action(AGENTS_SIDEBAR_GROUP_TOGGLE, payload.UTF8String);
    [[NSNotificationCenter defaultCenter] postNotificationName:@"KittyAgentsSidebarGroupStateChanged" object:self];
}
- (void)outlineViewItemDidExpand:(NSNotification *)notification { [self groupChanged:notification collapsed:NO]; }
- (void)outlineViewItemDidCollapse:(NSNotification *)notification { [self groupChanged:notification collapsed:YES]; }

- (BOOL)kittySidebarHasKeyboardFocus {
    NSResponder *responder = self.window.firstResponder;
    return [responder isKindOfClass:[NSView class]] && [(NSView *)responder isDescendantOf:self];
}
- (void)setFocusBorderNeedsDisplay {
    [self setNeedsDisplay:YES];
}
- (void)kittySidebarCancelNavigation {
    if ([self kittySidebarHasKeyboardFocus]) {
        NSResponder *previous = previous_responders.anyObject;
        if (![previous isKindOfClass:[NSView class]] || [(NSView *)previous window] != self.window) previous = self.window.contentView;
        [self.window makeFirstResponder:previous];
    }
    [previous_responders removeAllObjects];
    [self setFocusBorderNeedsDisplay];
}
- (void)windowResignedKey:(NSNotification *)notification {
    if (notification.object == self.window) [self kittySidebarCancelNavigation];
}
- (void)focusSidebar {
    if (![self kittySidebarHasKeyboardFocus]) {
        [previous_responders removeAllObjects];
        if (self.window.firstResponder) [previous_responders addObject:self.window.firstResponder];
    }
    id target = remembered_selection;
    if (!has_entered_navigation || !target) {
        for (NSUInteger i = 0; i < active_rows.count; i++) {
            if ([active_rows[i] boolValue] && [window_ids[i] unsignedLongLongValue]) { target = window_ids[i]; break; }
        }
    }
    has_entered_navigation = YES;
    if ([target isKindOfClass:[NSNumber class]] && row_indices[target]) {
        NSString *key = group_keys[[row_indices[target] unsignedIntegerValue]];
        if (key.length) [outline expandItem:key];
    }
    NSInteger row = -1;
    for (NSInteger i = 0; i < outline.numberOfRows; i++) if ([[outline itemAtRow:i] isEqual:target]) { row = i; break; }
    if (row < 0 && outline.numberOfRows) row = 0;
    if (row >= 0) {
        [outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
        [self revealRow:row];
    }
    outline.keyboardFocusRequested = YES;
    [self.window makeFirstResponder:outline];
    [self setFocusBorderNeedsDisplay];
}
- (void)sendCommand:(NSString *)command windowID:(unsigned long long)window_id {
    if ([command isEqualToString:@"focus"] || [command isEqualToString:@"new_window"]) [self kittySidebarCancelNavigation];
    NSString *payload = [NSString stringWithFormat:@"%u:%llu:%@", os_window_id, window_id, command];
    set_cocoa_pending_action(AGENTS_SIDEBAR_MENU, payload.UTF8String);
}
- (void)activateNode:(id)node {
    if ([node isKindOfClass:[NSString class]]) {
        [self setGroup:node expanded:![outline isItemExpanded:node] animated:YES];
    } else if ([node unsignedLongLongValue]) [self sendCommand:@"focus" windowID:[node unsignedLongLongValue]];
}
- (void)outlineClicked:(id)sender {
    (void)sender;
    if (outline.clickedRow >= 0) [self activateNode:[outline itemAtRow:outline.clickedRow]];
}
- (BOOL)kittySidebarHandleKeyEvent:(NSEvent *)event {
    SEL configured = NSSelectorFromString(@"kittySidebarHandleConfiguredKeyEvent:");
    if ([self.window.contentView respondsToSelector:configured]) {
        BOOL (*invoke)(id, SEL, id) = (BOOL (*)(id, SEL, id))[self.window.contentView methodForSelector:configured];
        if (invoke(self.window.contentView, configured, event)) return YES;
    }
    NSUInteger modifiers = event.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
    if (modifiers & NSEventModifierFlagCommand) {
        NSString *key = event.charactersIgnoringModifiers.lowercaseString;
        return !([@[@"q", @"h", @"m", @"`", @"\t"] containsObject:key]);
    }
    if (modifiers & (NSEventModifierFlagControl | NSEventModifierFlagOption)) return YES;
    NSString *characters = event.charactersIgnoringModifiers;
    if (!characters.length) return YES;
    unichar key = [characters characterAtIndex:0];
    if (key == 0x1b) { [self kittySidebarCancelNavigation]; return YES; }
    if (key == '\t' || key == NSBackTabCharacter) {
        [self kittySidebarCancelNavigation];
        return YES;
    }
    if (key == NSF10FunctionKey && (modifiers & NSEventModifierFlagShift)) { [self showActions:nil]; return YES; }
    NSInteger row = outline.selectedRow;
    id node = [self selectedNode];
    switch (key) {
        case '\r': case NSEnterCharacter: [self activateNode:node]; return YES;
        case NSLeftArrowFunctionKey:
            if ([node isKindOfClass:[NSString class]]) [self setGroup:node expanded:NO animated:YES];
            else if (node) row = [outline rowForItem:[outline parentForItem:node]];
            break;
        case NSRightArrowFunctionKey:
            if ([node isKindOfClass:[NSString class]]) {
                if (![outline isItemExpanded:node]) [self setGroup:node expanded:YES animated:YES];
                else if ([group_children[node] count]) row++;
            }
            break;
        case NSUpArrowFunctionKey: row = row < 0 ? 0 : row - 1; break;
        case NSDownArrowFunctionKey: row++; break;
        case NSHomeFunctionKey: row = 0; break;
        case NSEndFunctionKey: row = outline.numberOfRows - 1; break;
        case NSPageUpFunctionKey: row -= MAX(1, (NSInteger)(scroll_view.contentSize.height / 48)); break;
        case NSPageDownFunctionKey: row += MAX(1, (NSInteger)(scroll_view.contentSize.height / 48)); break;
        default: return YES;
    }
    if (outline.numberOfRows) {
        row = MAX(0, MIN(outline.numberOfRows - 1, row));
        [outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
        [self revealRow:row];
    }
    return YES;
}
- (void)mouseDown:(NSEvent *)event {
    NSPoint p = [self convertPoint:event.locationInWindow fromView:nil];
    if (p.x >= self.bounds.size.width - 8) {
        resizing = YES;
        resize_hovered = YES;
        [self setNeedsDisplay:YES];
    }
}

- (void)addMenuItemTo:(NSMenu *)menu title:(NSString *)title command:(const char *)command windowID:(unsigned long long)window_id {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:@selector(agentsSidebarMenuAction:) keyEquivalent:@""];
    item.target = self;
    item.tag = (NSInteger)window_id;
    item.representedObject = @(command);
    [menu addItem:item];
    [item release];
}

// The row list is rebuilt on a timer, which can happen while the menu is
// tracking, so the menu carries the agent's window id rather than the row
// index it was opened on.
- (NSMenu *)menuForNode:(id)node {
    unsigned long long window_id = [node isKindOfClass:[NSNumber class]] ? [node unsignedLongLongValue] : 0;
    NSMenu *menu = [[[NSMenu alloc] initWithTitle:@"Agent and sidebar actions"] autorelease];
    if (window_id && row_indices[node]) {
        [self addMenuItemTo:menu title:@"Mark as Unread" command:"mark_unread" windowID:window_id];
        [self addMenuItemTo:menu title:@"New Window Here" command:"new_window" windowID:window_id];
        [self addMenuItemTo:menu title:@"Copy Working Directory" command:"copy_cwd" windowID:window_id];
        [menu addItem:[NSMenuItem separatorItem]];
        [self addMenuItemTo:menu title:@"Close Agent Window…" command:"close" windowID:window_id];
        [menu addItem:[NSMenuItem separatorItem]];
    }
    [self addMenuItemTo:menu title:@"Increase Sidebar Width" command:"width_increase" windowID:0];
    [self addMenuItemTo:menu title:@"Decrease Sidebar Width" command:"width_decrease" windowID:0];
    [self addMenuItemTo:menu title:@"Expand All Groups" command:"expand_all" windowID:0];
    [self addMenuItemTo:menu title:@"Collapse All Groups" command:"collapse_all" windowID:0];
    [self addMenuItemTo:menu title:@"Hide Sidebar" command:"hide" windowID:0];
    return menu;
}
- (void)showActions:(id)sender {
    (void)sender;
    NSMenu *menu = [self menuForNode:[self selectedNode]];
    NSRect anchor = outline.selectedRow >= 0 ? [outline rectOfRow:outline.selectedRow] : outline.visibleRect;
    [menu popUpMenuPositioningItem:nil atLocation:NSMakePoint(NSMinX(anchor) + 12, NSMinY(anchor)) inView:outline];
}

- (void)agentsSidebarMenuAction:(id)sender {
    NSMenuItem *item = (NSMenuItem *)sender;
    if (![item.representedObject isKindOfClass:[NSString class]]) return;
    [self sendCommand:item.representedObject windowID:(unsigned long long)item.tag];
}

- (void)updateHoverAtPoint:(NSPoint)p {
    BOOL hovered = resizing || (p.x >= self.bounds.size.width - 8 && p.x <= self.bounds.size.width);
    if (resize_hovered != hovered) { resize_hovered = hovered; [self setNeedsDisplay:YES]; }
}

- (void)mouseMoved:(NSEvent *)event {
    NSPoint p = [self convertPoint:event.locationInWindow fromView:nil];
    [self updateHoverAtPoint:p];
    if (resize_hovered) [[NSCursor resizeLeftRightCursor] set];
    else [[NSCursor pointingHandCursor] set];
}


- (void)mouseEntered:(NSEvent *)event {
    [self mouseMoved:event];
}
- (void)mouseExited:(NSEvent *)event {
    (void)event;
    [self updateHoverAtPoint:NSMakePoint(-1.0, -1.0)];
    if (!resizing) [[NSCursor arrowCursor] set];
}

- (void)mouseDragged:(NSEvent *)event {
    if (!resizing || !self.superview) return;
    CGFloat scale = MAX(1.0, self.window.backingScaleFactor);
    NSPoint p = [self.superview convertPoint:event.locationInWindow fromView:nil];
    CGFloat min_width = 16.0 * cell_width_pixels / scale;
    CGFloat max_width = self.superview.bounds.size.width * 0.75;
    CGFloat width = MIN(max_width, MAX(min_width, p.x));
    unsigned cols = MAX(16u, (unsigned)llround(width * scale / (CGFloat)cell_width_pixels));
    width = cols * cell_width_pixels / scale;
    [self previewSizeWidth:width];
    self.resizePending = YES;
    char data[64];
    snprintf(data, sizeof(data), "%u:%u", os_window_id, cols);
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE, data);
    OSWindow *osw = os_window_for_id(os_window_id);
    if (osw) osw->suppress_pane_animation = true;
}

- (void)mouseUp:(NSEvent *)event {
    resizing = NO;
    NSPoint p = [self convertPoint:event.locationInWindow fromView:nil];
    [self updateHoverAtPoint:p];
    [self resetCursorRects];
}
- (void)setRows:(const unsigned long long *)new_window_ids
         groups:(const char **)new_groups
      groupKeys:(const char **)new_group_keys
         agents:(const char **)new_agents
         labels:(const char **)new_labels
        details:(const char **)new_details
         states:(const unsigned char *)new_states
         active:(const bool *)active
          count:(size_t)count {
    id old_selection = [[self selectedNode] retain];
    NSMutableArray *next_window_ids = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_groups = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_group_keys = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_agents = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_labels = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_details = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_states = [NSMutableArray arrayWithCapacity:count];
    NSMutableArray *next_active = [NSMutableArray arrayWithCapacity:count];
    for (size_t i = 0; i < count; i++) {
        [next_window_ids addObject:@(new_window_ids ? new_window_ids[i] : 0ULL)];
        [next_groups addObject:[NSString stringWithUTF8String:new_groups[i] ? new_groups[i] : ""]];
        [next_group_keys addObject:[NSString stringWithUTF8String:new_group_keys[i] ? new_group_keys[i] : ""]];
        [next_agents addObject:[NSString stringWithUTF8String:new_agents[i] ? new_agents[i] : ""]];
        [next_labels addObject:[NSString stringWithUTF8String:new_labels[i] ? new_labels[i] : ""]];
        [next_details addObject:[NSString stringWithUTF8String:new_details[i] ? new_details[i] : ""]];
        [next_states addObject:@(new_states ? new_states[i] : 0)];
        [next_active addObject:@(active && active[i])];
    }
    [window_ids release];
    [groups release];
    [group_keys release];
    [agents release];
    [labels release];
    [details release];
    [states release];
    [active_rows release];
    window_ids = [next_window_ids copy];
    groups = [next_groups copy];
    group_keys = [next_group_keys copy];
    agents = [next_agents copy];
    labels = [next_labels copy];
    details = [next_details copy];
    states = [next_states copy];
    active_rows = [next_active copy];
    NSMutableArray *roots = [NSMutableArray array];
    NSMutableDictionary *children = [NSMutableDictionary dictionary];
    NSMutableDictionary *indices = [NSMutableDictionary dictionary];
    for (NSUInteger i = 0; i < window_ids.count; i++) {
        if (![window_ids[i] unsignedLongLongValue] || !agents[i].length) continue;
        NSNumber *identity = window_ids[i];
        indices[identity] = @(i);
        NSString *key = group_keys[i];
        if (key.length) {
            if (!children[key]) { children[key] = [NSMutableArray array]; [roots addObject:key]; }
            [children[key] addObject:identity];
        } else [roots addObject:identity];
    }
    BOOL same_structure = [root_nodes isEqualToArray:roots] && [group_children isEqualToDictionary:children];
    if (!same_structure) {
        [root_nodes release];
        [group_children release];
        root_nodes = [roots copy];
        group_children = [children copy];
    }
    [row_indices release];
    row_indices = [indices copy];
    summary.stringValue = indices.count ? [NSString stringWithFormat:@"Grouping: %@ · %lu agents · %lu groups", self.grouping,
                                         (unsigned long)indices.count, (unsigned long)children.count] : @"No AI agents";
    summary.toolTip = summary.stringValue;
    if (!same_structure) [self rebuildOutline];
    else {
        syncing_outline = YES;
        for (id node in root_nodes) {
            if ([node isKindOfClass:[NSString class]]) [self setGroup:node expanded:![collapsed_groups containsObject:node] animated:YES];
        }
        syncing_outline = NO;
        for (NSInteger row = 0; row < outline.numberOfRows; row++) {
            id node = [outline itemAtRow:row];
            KittyAgentsSidebarRowView *view = [outline rowViewAtRow:row makeIfNecessary:NO];
            NSNumber *index = row_indices[node];
            view.activeAgent = index && [active_rows[index.unsignedIntegerValue] boolValue];
            [view setNeedsDisplay:YES];
            NSView *cell = [outline viewAtColumn:0 row:row makeIfNecessary:NO];
            cell.toolTip = [self labelForNode:node];
            [cell setNeedsDisplay:YES];
        }
    }
    if (old_selection && !row_indices[old_selection] && !group_children[old_selection]) {
        [outline deselectAll:nil];
        [remembered_selection release];
        remembered_selection = nil;
    }
    [old_selection release];
    [self updateAnimationState];
    [self setNeedsDisplay:YES];
}

- (NSColor *)stateColor:(NSUInteger)state active:(BOOL)active {
    if (active && state == 1) return [NSColor alternateSelectedControlTextColor];
    switch (state) {
        case 2: return [NSColor systemOrangeColor];
        case 3: return [NSColor systemRedColor];
        case 4: return [NSColor systemGreenColor];
        default: return [NSColor secondaryLabelColor];
    }
}


// The spinner only runs while some row is working. A sidebar full of idle
// agents must not keep waking the CPU.
- (BOOL)hasWorkingRow {
    for (NSUInteger i = 0; i < states.count; i++) {
        if ([states[i] unsignedIntegerValue] == 2) return YES;
    }
    return NO;
}

- (BOOL)isAnimating {
    return spinner_timer != nil;
}

- (void)updateAnimationState {
    BOOL want = [self hasWorkingRow] && self.window != nil;
    if (want && !spinner_timer) {
        spinner_timer = [NSTimer timerWithTimeInterval:(1.0 / 30.0) target:self selector:@selector(advanceSpinner:) userInfo:nil repeats:YES];
        // Common run loop modes, so the spinner keeps turning during a live
        // resize or a menu tracking loop instead of freezing mid-arc.
        [[NSRunLoop currentRunLoop] addTimer:spinner_timer forMode:NSRunLoopCommonModes];
    } else if (!want && spinner_timer) {
        [self stopAnimating];
    }
}

- (void)advanceSpinner:(NSTimer *)timer {
    (void)timer;
    spinner_phase += 1.0 / 30.0;
    if (spinner_phase > 1.0e6) spinner_phase = 0.0;
    for (NSInteger row = 0; row < outline.numberOfRows; row++) {
        if (NSIntersectsRect([outline rectOfRow:row], outline.visibleRect)) [[outline viewAtColumn:0 row:row makeIfNecessary:NO] setNeedsDisplay:YES];
    }
}

- (void)stopAnimating {
    [spinner_timer invalidate];
    spinner_timer = nil;
}

- (void)viewDidMoveToWindow {
    [super viewDidMoveToWindow];
    // Leaving a window orphans the run loop source; re-evaluate either way.
    [self updateAnimationState];
}

// An indeterminate arc, the platform idiom for "running, length unknown".
- (void)drawSpinnerInRect:(NSRect)rect color:(NSColor *)color {
    const CGFloat line_width = 1.8;
    NSPoint center = NSMakePoint(NSMidX(rect), NSMidY(rect));
    CGFloat radius = (MIN(rect.size.width, rect.size.height) - line_width) / 2.0;
    if (radius <= 0) return;
    CGFloat revolutions_per_second = 0.9;
    CGFloat start = -fmod(spinner_phase * revolutions_per_second, 1.0) * 360.0;
    [[color colorWithAlphaComponent:0.25] setStroke];
    NSBezierPath *track = [NSBezierPath bezierPath];
    track.lineWidth = line_width;
    [track appendBezierPathWithArcWithCenter:center radius:radius startAngle:0 endAngle:360];
    [track stroke];
    [color setStroke];
    NSBezierPath *arc = [NSBezierPath bezierPath];
    arc.lineWidth = line_width;
    arc.lineCapStyle = NSLineCapStyleRound;
    [arc appendBezierPathWithArcWithCenter:center radius:radius startAngle:start endAngle:start - 100.0 clockwise:YES];
    [arc stroke];
}

- (NSImage *)iconForAgent:(NSString *)agent textColor:(NSColor *)textColor {
    if (!agent.length || !_iconsDir.length) return nil;
    // Convert to sRGB before reading components (semantic colors crash otherwise)
    NSColor *rgb = [textColor colorUsingColorSpace:[NSColorSpace sRGBColorSpace]];
    if (!rgb) return nil;
    NSString *hexColor =
        [NSString stringWithFormat:@"#%02X%02X%02X", (int)(rgb.redComponent * 255.0), (int)(rgb.greenComponent * 255.0), (int)(rgb.blueComponent * 255.0)];
    NSString *cacheKey = [NSString stringWithFormat:@"%@:%@", agent, hexColor];
    static NSCache<NSString *, NSImage *> *iconCache = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
      iconCache = [[NSCache alloc] init];
      iconCache.countLimit = 100;
    });
    NSImage *cached = [iconCache objectForKey:cacheKey];
    if (cached) return cached;
    NSString *svgPath = [_iconsDir stringByAppendingPathComponent:[agent stringByAppendingString:@".svg"]];
    NSString *pngPath = [_iconsDir stringByAppendingPathComponent:[agent stringByAppendingString:@".png"]];
    NSImage *icon = nil;
    if ([[NSFileManager defaultManager] fileExistsAtPath:svgPath]) {
        NSString *svgText = [NSString stringWithContentsOfFile:svgPath encoding:NSUTF8StringEncoding error:nil];
        if (svgText) {
            NSString *substituted = [svgText stringByReplacingOccurrencesOfString:@"currentColor" withString:hexColor];
            NSData *data = [substituted dataUsingEncoding:NSUTF8StringEncoding];
            icon = [[[NSImage alloc] initWithData:data] autorelease];
        }
        if (!icon) icon = [[[NSImage alloc] initWithContentsOfFile:svgPath] autorelease];
    }
    if (!icon && [[NSFileManager defaultManager] fileExistsAtPath:pngPath]) { icon = [[[NSImage alloc] initWithContentsOfFile:pngPath] autorelease]; }
    if (icon) [iconCache setObject:icon forKey:cacheKey];
    return icon;
}

- (NSString *)labelForNode:(id)node {
    if ([node isKindOfClass:[NSString class]]) {
        NSNumber *first = [group_children[node] firstObject];
        return first ? groups[[row_indices[first] unsignedIntegerValue]] : @"";
    }
    NSNumber *index = row_indices[node];
    if (!index) return @"";
    NSUInteger i = index.unsignedIntegerValue;
    return [NSString stringWithFormat:@"%@, %@%@", labels[i], details[i], [active_rows[i] boolValue] ? @", current terminal" : @""];
}
- (void)drawNode:(id)node inRect:(NSRect)rect {
    NSMutableParagraphStyle *paragraph = [[[NSMutableParagraphStyle alloc] init] autorelease];
    paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
    if ([node isKindOfClass:[NSString class]]) {
        CGFloat center = NSMidY(rect);
        NSBezierPath *chevron = [NSBezierPath bezierPath];
        chevron.lineWidth = 1.8;
        chevron.lineCapStyle = NSLineCapStyleRound;
        chevron.lineJoinStyle = NSLineJoinStyleRound;
        if ([outline isItemExpanded:node]) {
            [chevron moveToPoint:NSMakePoint(8, center - 2)];
            [chevron lineToPoint:NSMakePoint(12, center + 2)];
            [chevron lineToPoint:NSMakePoint(16, center - 2)];
        } else {
            [chevron moveToPoint:NSMakePoint(10, center - 4)];
            [chevron lineToPoint:NSMakePoint(14, center)];
            [chevron lineToPoint:NSMakePoint(10, center + 4)];
        }
        [[NSColor labelColor] setStroke];
        [chevron stroke];
        [[self labelForNode:node] drawInRect:NSMakeRect(26, rect.origin.y + 5, MAX(0, rect.size.width - 30), rect.size.height - 10) withAttributes:@{
            NSFontAttributeName: [NSFont boldSystemFontOfSize:11], NSForegroundColorAttributeName: [NSColor labelColor],
            NSParagraphStyleAttributeName: paragraph}];
        return;
    }
    NSNumber *index = row_indices[node];
    if (!index) return;
    NSUInteger i = index.unsignedIntegerValue;
    BOOL active = [active_rows[i] boolValue];
    NSColor *primary = active ? [NSColor alternateSelectedControlTextColor] : [NSColor controlTextColor];
    NSColor *secondary = active ? [NSColor alternateSelectedControlTextColor] : [NSColor secondaryLabelColor];
    NSColor *state_color = [self stateColor:[states[i] unsignedIntegerValue] active:active];
    CGFloat center = NSMidY(rect), text_left = 20;
    if ([states[i] unsignedIntegerValue] == 2) [self drawSpinnerInRect:NSMakeRect(4, center - 5.5, 11, 11) color:state_color];
    else {
        [state_color setFill];
        [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(5.5, center - 4, 8, 8)] fill];
    }
    NSImage *icon = [self iconForAgent:agents[i] textColor:primary];
    if (icon && icon.size.width > 0 && icon.size.height > 0) {
        CGFloat scale = MIN(16 / icon.size.width, 16 / icon.size.height);
        NSSize size = NSMakeSize(icon.size.width * scale, icon.size.height * scale);
        [icon drawInRect:NSMakeRect(22 + (16 - size.width) / 2, center - size.height / 2, size.width, size.height)
               fromRect:NSZeroRect operation:NSCompositingOperationSourceOver fraction:1 respectFlipped:YES hints:nil];
        text_left = 44;
    }
    NSDictionary *title_attrs = @{NSFontAttributeName: [NSFont boldSystemFontOfSize:12],
                                 NSForegroundColorAttributeName: primary, NSParagraphStyleAttributeName: paragraph};
    NSDictionary *detail_attrs = @{NSFontAttributeName: [NSFont systemFontOfSize:10],
                                  NSForegroundColorAttributeName: secondary, NSParagraphStyleAttributeName: paragraph};
    CGFloat width = MAX(0, rect.size.width - text_left - 6);
    [labels[i] drawInRect:NSMakeRect(text_left, center - 16, width, 17) withAttributes:title_attrs];
    NSMutableAttributedString *detail = [[[NSMutableAttributedString alloc] initWithString:details[i] attributes:detail_attrs] autorelease];
    NSRange separator = [details[i] rangeOfString:@" · " options:NSBackwardsSearch];
    if (separator.location != NSNotFound) {
        NSUInteger start = NSMaxRange(separator);
        [detail addAttributes:@{NSForegroundColorAttributeName:state_color, NSFontAttributeName:[NSFont boldSystemFontOfSize:10.5]}
                        range:NSMakeRange(start, details[i].length - start)];
    }
    [detail drawInRect:NSMakeRect(text_left, center + 2, width, 14)];
}
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    [[NSColor separatorColor] setFill];
    NSRectFill(NSMakeRect(0, 39, self.bounds.size.width, 1));
    if ([self kittySidebarHasKeyboardFocus]) {
        [[NSColor controlAccentColor] setStroke];
        NSBezierPath *focus = [NSBezierPath bezierPathWithRect:NSInsetRect(self.bounds, 1, 1)];
        focus.lineWidth = 2.0;
        [focus stroke];
    }
    const CGFloat rail_width = (resize_hovered || resizing) ? 2.0 : 1.0;
    const CGFloat rail_x = MAX(0.0, self.bounds.size.width - rail_width);
    NSColor *rail_color;
    if (resizing) rail_color = [[NSColor controlAccentColor] colorWithAlphaComponent:0.88];
    else if (resize_hovered) rail_color = [[NSColor labelColor] colorWithAlphaComponent:0.72];
    else rail_color = [[NSColor separatorColor] colorWithAlphaComponent:0.55];
    [rail_color setFill];
    NSRectFill(NSMakeRect(rail_x, 0.0, rail_width, self.bounds.size.height));
}
- (void)dealloc {
    [self stopAnimating];
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    if (resize_tracking_area) [self removeTrackingArea:resize_tracking_area];
    [resize_tracking_area release];
    outline.delegate = nil;
    outline.dataSource = nil;
    [window_ids release];
    [groups release];
    [group_keys release];
    [agents release];
    [labels release];
    [details release];
    [states release];
    [active_rows release];
    [root_nodes release];
    [group_children release];
    [row_indices release];
    [remembered_selection release];
    [previous_responders release];
    [outline release];
    [scroll_view release];
    [summary release];
    [background_view release];
    [collapsed_groups release];
    [_iconsDir release];
    [_grouping release];
    [super dealloc];
}
@end

bool
cocoa_set_agents_sidebar_focus(void *ptr, bool focused, bool toggle) {
    NSWindow *window = (NSWindow *)ptr;
    bool (*dispatch)(void *, const GLFWkeyevent *) = cocoa_dispatch_agents_sidebar_shortcut;
    objc_setAssociatedObject(window, NSSelectorFromString(@"kittySidebarHandleConfiguredKeyEvent:"),
                             [NSValue valueWithBytes:&dispatch objCType:@encode(__typeof__(dispatch))], OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    KittyAgentsSidebarView *sidebar = objc_getAssociatedObject(window, kitty_agents_sidebar_key);
    BOOL was_focused = [sidebar kittySidebarHasKeyboardFocus];
    if (focused && !(toggle && was_focused)) [sidebar focusSidebar];
    else [sidebar kittySidebarCancelNavigation];
    return was_focused;
}

void
cocoa_set_agents_sidebar(
    void *ptr,
    unsigned os_window_id,
    bool enabled,
    unsigned width_pixels,
    unsigned cell_width,
    const char *grouping,
    const char *icons_dir,
    const unsigned long long *window_ids,
    const char **groups,
    const char **group_keys,
    const char **agents,
    const char **labels,
    const char **details,
    const unsigned char *states,
    const bool *active,
    const char **collapsed,
    size_t collapsed_count,
    size_t count) {
    NSWindow *window = (NSWindow *)ptr;
    if (!window || !window.contentView) return;
    [KittyAgentsSidebarView setSharedCollapsedGroups:collapsed count:collapsed_count];
    NSButton *toggle_button = agents_sidebar_toggle_button(window);
    toggle_button.hidden = NO;
    agents_sidebar_update_toggle_state(toggle_button, enabled);
    if (!enabled) {
        KittyAgentsSidebarView *sidebar = objc_getAssociatedObject(window, kitty_agents_sidebar_key);
        [sidebar kittySidebarCancelNavigation];
        [sidebar stopAnimating];
        [[sidebar backgroundView] removeFromSuperview];
        [sidebar removeFromSuperview];
        objc_setAssociatedObject(window, kitty_agents_sidebar_key, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        return;
    }
    CGFloat scale = MAX(1.0, window.backingScaleFactor);
    CGFloat width = width_pixels / scale;
    KittyAgentsSidebarView *sidebar = objc_getAssociatedObject(window, kitty_agents_sidebar_key);
    if (!sidebar) {
        sidebar = [[KittyAgentsSidebarView alloc] initWithOSWindowID:os_window_id width:width cellWidth:cell_width];
        [window.contentView addSubview:sidebar positioned:NSWindowAbove relativeTo:nil];
        [window.contentView addSubview:[sidebar backgroundView] positioned:NSWindowBelow relativeTo:sidebar];
        objc_setAssociatedObject(window, kitty_agents_sidebar_key, sidebar, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        [sidebar release];
    }
    // The sidebar action is deferred until after rendering. Carry suppression
    // across that render so the resulting layout cannot animate after mouse-up.
    if (sidebar.resizePending) {
        OSWindow *osw = os_window_for_id(os_window_id);
        if (osw) osw->suppress_pane_animation = true;
        sidebar.resizePending = NO;
    }
    NSRect frame = NSMakeRect(0, 0, width, window.contentView.bounds.size.height);
    [sidebar setFrame:frame];
    [[sidebar backgroundView] setFrame:frame];
    sidebar.grouping = [NSString stringWithUTF8String:grouping];
    sidebar.iconsDir = icons_dir ? [NSString stringWithUTF8String:icons_dir] : nil;
    [sidebar setRows:window_ids groups:groups groupKeys:group_keys agents:agents labels:labels details:details states:states active:active count:count];
}

static NSMenuItem *secure_input_title_menu = NULL;


static NSString *
find_app_name(void) {
    size_t i;
    NSDictionary *infoDictionary = [[NSBundle mainBundle] infoDictionary];

    // Keys to search for as potential application names
    NSString *name_keys[] = {
        @"CFBundleDisplayName",
        @"CFBundleName",
        @"CFBundleExecutable",
    };

    for (i = 0; i < sizeof(name_keys) / sizeof(name_keys[0]); i++) {
        id name = infoDictionary[name_keys[i]];
        if (name && [name isKindOfClass:[NSString class]] && ![name isEqualToString:@""]) { return name; }
    }

    char **progname = _NSGetProgname();
    if (progname && *progname) return @(*progname);

    // Really shouldn't get here
    return @"kitty";
}

#define debug_key(...)                \
    if (OPT(debug_keyboard)) {        \
        fprintf(stderr, __VA_ARGS__); \
        fflush(stderr);               \
    }

// SecureKeyboardEntryController {{{
@interface SecureKeyboardEntryController : NSObject

@property(nonatomic, readonly) BOOL isDesired;
@property(nonatomic, readonly, getter=isEnabled) BOOL enabled;

+ (instancetype)sharedInstance;

- (void)toggle;
- (void)update;

@end

@implementation SecureKeyboardEntryController {
    int _count;
    BOOL _desired;
}

+ (instancetype)sharedInstance {
    static id instance;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{ instance = [[self alloc] init]; });
    return instance;
}

- (instancetype)init {
    self = [super init];
    if (self) {
        _desired = false;

        [[NSNotificationCenter defaultCenter] addObserver:self
                                                 selector:@selector(applicationDidResignActive:)
                                                     name:NSApplicationDidResignActiveNotification
                                                   object:nil];
        [[NSNotificationCenter defaultCenter] addObserver:self
                                                 selector:@selector(applicationDidBecomeActive:)
                                                     name:NSApplicationDidBecomeActiveNotification
                                                   object:nil];
        if ([NSApp isActive]) { [self update]; }
    }
    return self;
}

#pragma mark - API

- (void)toggle {
    // Set _desired to the opposite of the current state.
    _desired = !_desired;
    debug_key("SecureKeyboardEntry: toggle called. Setting desired to %d ", _desired);

    // Try to set the system's state of secure input to the desired state.
    [self update];
}

- (BOOL)isEnabled {
    return !!IsSecureEventInputEnabled();
}

- (BOOL)isDesired {
    return _desired;
}

#pragma mark - Notifications

- (void)applicationDidResignActive:(NSNotification *)notification {
    (void)notification;
    if (_count > 0) {
        debug_key("SecureKeyboardEntry: Application resigning active.");
        [self update];
    }
}

- (void)applicationDidBecomeActive:(NSNotification *)notification {
    (void)notification;
    if (self.isDesired) {
        debug_key("SecureKeyboardEntry: Application became active.");
        [self update];
    }
}

#pragma mark - Private

- (BOOL)allowed {
    return [NSApp isActive];
}

- (void)update {
    debug_key("Update secure keyboard entry. desired=%d active=%d\n", (int)self.isDesired, (int)[NSApp isActive]);
    const BOOL secure = self.isDesired && [self allowed];

    if (secure && _count > 0) {
        debug_key("Want to turn on secure input but it's already on\n");
        return;
    }

    if (!secure && _count == 0) {
        debug_key("Want to turn off secure input but it's already off\n");
        return;
    }

    debug_key("Before: IsSecureEventInputEnabled returns %d ", (int)self.isEnabled);
    if (secure) {
        OSErr err = EnableSecureEventInput();
        debug_key("EnableSecureEventInput err=%d ", (int)err);
        if (err) {
            debug_key("EnableSecureEventInput failed with error %d ", (int)err);
        } else {
            _count += 1;
        }
    } else {
        OSErr err = DisableSecureEventInput();
        debug_key("DisableSecureEventInput err=%d ", (int)err);
        if (err) {
            debug_key("DisableSecureEventInput failed with error %d ", (int)err);
        } else {
            _count -= 1;
        }
    }
    debug_key("After: IsSecureEventInputEnabled returns %d\n", (int)self.isEnabled);
}

@end
// }}}

static void
update_secure_input_menu_bar_indicator(BOOL enabled) {
    if (enabled) {
        if (secure_input_title_menu == NULL) {
            NSMenu *bar = [NSApp mainMenu];
            secure_input_title_menu = [bar addItemWithTitle:@"" action:NULL keyEquivalent:@""];
            NSMenu *m = [[NSMenu alloc] initWithTitle:@"[Secure input]"];
            [secure_input_title_menu setSubmenu:m];
            [m release];
        }
    } else {
        if (secure_input_title_menu != NULL) {
            NSMenu *bar = [NSApp mainMenu];
            [bar removeItem:secure_input_title_menu];
            secure_input_title_menu = NULL;
        }
    }
}

@interface UserMenuItem : NSMenuItem
@property(nonatomic) size_t action_index;
@end

@implementation UserMenuItem {
}
@end

@interface WindowMenuDelegate : NSObject <NSMenuDelegate>
@end

@implementation WindowMenuDelegate
- (void)menuNeedsUpdate:(NSMenu *)menu {
    // AppKit injects selectPreviousTab:, selectNextTab:, moveTabToNewWindow:,
    // toggleTabOverview: when allowsAutomaticWindowTabbing=YES (we need it YES
    // for mergeAllWindows:/toggleTabBar: to work). Remove those injected
    // duplicates; kitty has its own versions with shortcuts from kitty.conf.
    static const char *injected[] = {"selectPreviousTab:", "selectNextTab:", "moveTabToNewWindow:", "toggleTabOverview:"};
    NSMutableArray<NSMenuItem *> *to_remove = [NSMutableArray array];
    for (NSMenuItem *item in menu.itemArray) {
        if (!item.action) continue;
        const char *sel_name = sel_getName(item.action);
        for (size_t i = 0; i < arraysz(injected); i++) {
            if (strcmp(sel_name, injected[i]) == 0) {
                [to_remove addObject:item];
                break;
            }
        }
    }
    for (NSMenuItem *item in to_remove) [menu removeItem:item];
}
@end


@interface GlobalMenuTarget : NSObject
+ (GlobalMenuTarget *)shared_instance;
@end

#define PENDING(selector, which)               \
    -(void)selector : (id)sender {             \
        (void)sender;                          \
        set_cocoa_pending_action(which, NULL); \
    }

@implementation GlobalMenuTarget

- (void)user_menu_action:(id)sender {
    UserMenuItem *m = sender;
    if (m.action_index < OPT(global_menu).count && OPT(global_menu.entries)) {
        set_cocoa_pending_action(USER_MENU_ACTION, OPT(global_menu).entries[m.action_index].definition);
    }
}

PENDING(edit_config_file, PREFERENCES_WINDOW)
PENDING(new_os_window, NEW_OS_WINDOW)
PENDING(detach_tab, DETACH_TAB)
PENDING(close_os_window, CLOSE_OS_WINDOW)
PENDING(close_tab, CLOSE_TAB)
PENDING(new_tab, NEW_TAB)
PENDING(next_tab, NEXT_TAB)
PENDING(previous_tab, PREVIOUS_TAB)
PENDING(new_window, NEW_WINDOW)
PENDING(close_window, CLOSE_WINDOW)
PENDING(reset_terminal, RESET_TERMINAL)
PENDING(clear_terminal_and_scrollback, CLEAR_TERMINAL_AND_SCROLLBACK)
PENDING(clear_scrollback, CLEAR_SCROLLBACK)
PENDING(clear_screen, CLEAR_SCREEN)
PENDING(clear_last_command, CLEAR_LAST_COMMAND)
PENDING(reload_config, RELOAD_CONFIG)
PENDING(toggle_macos_secure_keyboard_entry, TOGGLE_MACOS_SECURE_KEYBOARD_ENTRY)
PENDING(macos_cycle_through_os_windows, MACOS_CYCLE_THROUGH_OS_WINDOWS)
PENDING(macos_cycle_through_os_windows_backwards, MACOS_CYCLE_THROUGH_OS_WINDOWS_BACKWARDS)
PENDING(toggle_macos_tab_overview, TOGGLE_MACOS_TAB_OVERVIEW)
PENDING(search_scrollback, SEARCH_SCROLLBACK)
PENDING(toggle_fullscreen, TOGGLE_FULLSCREEN)
PENDING(open_kitty_website, OPEN_KITTY_WEBSITE)
PENDING(hide_macos_app, HIDE)
PENDING(hide_macos_other_apps, HIDE_OTHERS)
PENDING(minimize_macos_window, MINIMIZE)
PENDING(quit, QUIT)
PENDING(paste_from_clipboard, PASTE_FROM_CLIPBOARD)
PENDING(copy_or_noop, COPY_OR_NOOP)

- (BOOL)validateMenuItem:(NSMenuItem *)item {
    if (item.action == @selector(toggle_macos_secure_keyboard_entry:)) {
        item.state = [SecureKeyboardEntryController sharedInstance].isDesired ? NSControlStateValueOn : NSControlStateValueOff;
    } else if (item.action == @selector(toggle_fullscreen:)) {
        item.title = ([NSApp currentSystemPresentationOptions] & NSApplicationPresentationFullScreen) ? @"Exit Full Screen" : @"Enter Full Screen";
        if (![NSApp keyWindow]) return NO;
    } else if (item.action == @selector(toggle_macos_tab_overview:)) {
        // The overview only exists with native tabs on and at least two tabs
        // in the key window's tab group.
        NSWindow *window = [NSApp keyWindow];
        if (!OPT(macos_native_tabs) || !window || [window tabbedWindows].count < 2) return NO;
    } else if (item.action == @selector(minimize_macos_window:)) {
        NSWindow *window = [NSApp keyWindow];
        if (!window || window.miniaturized || [NSApp currentSystemPresentationOptions] & NSApplicationPresentationFullScreen) return NO;
    } else if (
        item.action == @selector(close_os_window:) || item.action == @selector(close_tab:) || item.action == @selector(close_window:) ||
        item.action == @selector(reset_terminal:) || item.action == @selector(clear_terminal_and_scrollback:) ||
        item.action == @selector(clear_last_command:) || item.action == @selector(clear_scrollback:) || item.action == @selector(clear_screen:) ||
        item.action == @selector(previous_tab:) || item.action == @selector(next_tab:) || item.action == @selector(detach_tab:)) {
        if (![NSApp keyWindow]) return NO;
    } else if (item.action == @selector(paste_from_clipboard:)) {
        if (![NSApp keyWindow]) return NO;
        NSPasteboard *pb = [NSPasteboard generalPasteboard];
        if (![pb stringForType:NSPasteboardTypeString]) return NO;
    } else if (item.action == @selector(copy_or_noop:)) {
        if (![NSApp keyWindow]) return NO;
        OSWindow *osw = current_os_window();
        if (osw && osw->num_tabs > osw->active_tab) {
            Tab *tab = osw->tabs + osw->active_tab;
            if (tab->num_windows > tab->active_window) {
                Screen *screen = tab->windows[tab->active_window].render_data.screen;
                if (screen && screen_has_selection(screen)) return YES;
            }
        }
        return NO;
    }
    return YES;
}

#undef PENDING

+ (GlobalMenuTarget *)shared_instance {
    static GlobalMenuTarget *sharedGlobalMenuTarget = nil;
    @synchronized(self) {
        if (!sharedGlobalMenuTarget) {
            sharedGlobalMenuTarget = [[GlobalMenuTarget alloc] init];
            SecureKeyboardEntryController *k = [SecureKeyboardEntryController sharedInstance];
            if (!k.isDesired && [[NSUserDefaults standardUserDefaults] boolForKey:@"SecureKeyboardEntry"]) [k toggle];
            update_secure_input_menu_bar_indicator(k.isDesired);
        }
        return sharedGlobalMenuTarget;
    }
}

@end

typedef struct {
    char key[32];
    NSEventModifierFlags mods;
} GlobalShortcut;
typedef struct {
    GlobalShortcut new_os_window, close_os_window, close_tab, edit_config_file, reload_config;
    GlobalShortcut previous_tab, next_tab, new_tab, new_window, close_window, reset_terminal;
    GlobalShortcut clear_terminal_and_scrollback, clear_screen, clear_scrollback, clear_last_command;
    GlobalShortcut toggle_macos_secure_keyboard_entry, toggle_fullscreen, open_kitty_website;
    GlobalShortcut hide_macos_app, hide_macos_other_apps, minimize_macos_window, quit, search_scrollback;
    GlobalShortcut macos_cycle_through_os_windows, macos_cycle_through_os_windows_backwards;
    GlobalShortcut paste_from_clipboard, copy_or_noop, toggle_macos_tab_overview;
} GlobalShortcuts;
static GlobalShortcuts global_shortcuts;

static PyObject *
cocoa_set_global_shortcut(PyObject *self UNUSED, PyObject *args) {
    int mods;
    unsigned int key;
    const char *name;
    if (!PyArg_ParseTuple(args, "siI", &name, &mods, &key)) return NULL;
    GlobalShortcut *gs = NULL;
#define Q(x) \
    if (strcmp(name, #x) == 0) gs = &global_shortcuts.x
    Q(new_os_window);
    else Q(close_os_window); else Q(close_tab);
    else Q(edit_config_file); else Q(new_tab);
    else Q(next_tab); else Q(previous_tab);
    else Q(new_window); else Q(close_window);
    else Q(reset_terminal); else Q(clear_terminal_and_scrollback);
    else Q(clear_scrollback); else Q(clear_screen);
    else Q(clear_last_command); else Q(reload_config);
    else Q(toggle_macos_secure_keyboard_entry); else Q(toggle_fullscreen);
    else Q(open_kitty_website); else Q(hide_macos_app);
    else Q(hide_macos_other_apps); else Q(minimize_macos_window);
    else Q(quit); else Q(search_scrollback);
    else Q(macos_cycle_through_os_windows); else Q(macos_cycle_through_os_windows_backwards);
    else Q(paste_from_clipboard); else Q(copy_or_noop);
    else Q(toggle_macos_tab_overview);
#undef Q
    if (gs == NULL) {
        PyErr_SetString(PyExc_KeyError, "Unknown shortcut name");
        return NULL;
    }
    int cocoa_mods;
    get_cocoa_key_equivalent(key, mods, gs->key, 32, &cocoa_mods);
    gs->mods = cocoa_mods;
    if (gs->key[0]) Py_RETURN_TRUE;
    Py_RETURN_FALSE;
}

// Implementation of applicationDockMenu: for the app delegate
static NSMenu *dockMenu = nil;
static NSMenu *
get_dock_menu(id self UNUSED, SEL _cmd UNUSED, NSApplication *sender UNUSED) {
    if (!dockMenu) {
        GlobalMenuTarget *global_menu_target = [GlobalMenuTarget shared_instance];
        dockMenu = [[NSMenu alloc] init];
        [[dockMenu addItemWithTitle:@"New OS Window" action:@selector(new_os_window:) keyEquivalent:@""] setTarget:global_menu_target];
    }
    return dockMenu;
}

static PyObject *notification_activated_callback = NULL;

static PyObject *
set_notification_activated_callback(PyObject *self UNUSED, PyObject *callback) {
    Py_CLEAR(notification_activated_callback);
    if (callback != Py_None) notification_activated_callback = Py_NewRef(callback);
    Py_RETURN_NONE;
}

static void
do_notification_callback(const char *identifier, const char *event, const char *action_identifer) {
    if (notification_activated_callback) {
        PyObject *ret =
            PyObject_CallFunction(notification_activated_callback, "sss", event, identifier ? identifier : "", action_identifer ? action_identifer : "");
        if (ret) Py_DECREF(ret);
        else PyErr_Print();
    }
}


@interface NotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation NotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    (void)(center);
    (void)notification;
    UNNotificationPresentationOptions options = UNNotificationPresentationOptionSound;
    if (@available(macOS 11.0, *)) options |= UNNotificationPresentationOptionList | UNNotificationPresentationOptionBanner;
    else options |= (1 << 2); // UNNotificationPresentationOptionAlert avoid deprecated warning
    completionHandler(options);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
    didReceiveNotificationResponse:(UNNotificationResponse *)response
             withCompletionHandler:(void (^)(void))completionHandler {
    (void)(center);
    char *identifier = strdup(response.notification.request.identifier.UTF8String);
    char *action_identifier = strdup(response.actionIdentifier.UTF8String);
    const char *event = "button";
    if ([response.actionIdentifier isEqualToString:UNNotificationDefaultActionIdentifier]) {
        event = "activated";
    } else if ([response.actionIdentifier isEqualToString:UNNotificationDismissActionIdentifier]) {
        // Crapple never actually sends this event on macOS
        event = "closed";
    }
    dispatch_async(dispatch_get_main_queue(), ^{
      do_notification_callback(identifier, event, action_identifier);
      free(identifier);
      free(action_identifier);
    });
    completionHandler();
}
@end

static UNUserNotificationCenter *
get_notification_center_safely(void) {
    NSBundle *b = [NSBundle mainBundle];
    // when bundleIdentifier is nil currentNotificationCenter crashes instead
    // of returning nil. Apple...purveyor of shiny TOYS
    if (!b || !b.bundleIdentifier) return nil;
    UNUserNotificationCenter *center = nil;
    @try {
        center = [UNUserNotificationCenter currentNotificationCenter];
    } @catch (NSException *e) {
        log_error("Failed to get current UNUserNotificationCenter object with error: %s (%s)", [[e name] UTF8String], [[e reason] UTF8String]);
    }
    return center;
}

static bool
ident_in_list_of_notifications(NSString *ident, NSArray<UNNotification *> *list) {
    for (UNNotification *n in list) {
        if ([[[n request] identifier] isEqualToString:ident]) return true;
    }
    return false;
}

void
cocoa_report_live_notifications(const char *ident) {
    do_notification_callback(ident, "live", ident ? ident : "");
}

static bool
remove_delivered_notification(const char *identifier) {
    UNUserNotificationCenter *center = get_notification_center_safely();
    if (!center) return false;
    char *ident = strdup(identifier);
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
      if (ident_in_list_of_notifications(@(ident), notifications)) { [center removeDeliveredNotificationsWithIdentifiers:@[ @(ident) ]]; }
      free(ident);
    }];
    return true;
}

static bool
live_delivered_notifications(void) {
    UNUserNotificationCenter *center = get_notification_center_safely();
    if (!center) return false;
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
      @autoreleasepool {
          NSMutableString *buffer = [NSMutableString stringWithCapacity:1024]; // autoreleased
          for (UNNotification *n in notifications) [buffer appendFormat:@"%@,", [[n request] identifier]];
          const char *val = [buffer UTF8String];
          set_cocoa_pending_action(COCOA_NOTIFICATION_UNTRACKED, val ? val : "");
      }
    }];
    return true;
}

static void
schedule_notification(
    const char *appname,
    const char *identifier,
    const char *title,
    const char *body,
    const char *image_path,
    int urgency,
    const char *category_id,
    bool muted) {
    @autoreleasepool {
        UNUserNotificationCenter *center = get_notification_center_safely();
        if (!center) return;
        // Configure the notification's payload.
        UNMutableNotificationContent *content = [[[UNMutableNotificationContent alloc] init] autorelease];
        if (title) content.title = @(title);
        if (body) content.body = @(body);
        if (appname) content.threadIdentifier = @(appname);
        if (category_id) content.categoryIdentifier = @(category_id);
        if (!muted) content.sound = [UNNotificationSound defaultSound];
#if __MAC_OS_X_VERSION_MIN_REQUIRED >= 120000
        switch (urgency) {
            case 0: content.interruptionLevel = UNNotificationInterruptionLevelPassive;
            case 2: content.interruptionLevel = UNNotificationInterruptionLevelCritical;
            default: content.interruptionLevel = UNNotificationInterruptionLevelActive;
        }
#else
        if ([content respondsToSelector:@selector(interruptionLevel)]) {
            NSUInteger level = 1;
            if (urgency == 0) level = 0;
            else if (urgency == 2) level = 3;
            [content setValue:@(level) forKey:@"interruptionLevel"];
        }
#endif
        if (image_path) {
            @try {
                NSError *error;
                NSURL *image_url = [NSURL fileURLWithFileSystemRepresentation:image_path isDirectory:NO relativeToURL:nil]; // autoreleased
                UNNotificationAttachment *attachment = [UNNotificationAttachment attachmentWithIdentifier:@"image"
                                                                                                      URL:image_url
                                                                                                  options:nil
                                                                                                    error:&error]; // autoreleased
                if (attachment) {
                    content.attachments = @[ attachment ];
                } else NSLog(@"Error attaching image %@ to notification: %@", @(image_path), error.localizedDescription);
            } @catch (NSException *exc) { NSLog(@"Creating image attachment %@ for notification failed with error: %@", @(image_path), exc.reason); }
        }

        // Deliver the notification
        static unsigned long counter = 1;
        UNNotificationRequest *request =
            [UNNotificationRequest requestWithIdentifier:(identifier ? @(identifier) : [NSString stringWithFormat:@"Id_%lu", counter++])
                                                 content:content
                                                 trigger:nil];
        char *duped_ident = strdup(identifier ? identifier : "");
        [center addNotificationRequest:request
                 withCompletionHandler:^(NSError *_Nullable error) {
                   if (error != nil) log_error("Failed to show notification: %s", [[error localizedDescription] UTF8String]);
                   bool ok = error == nil;
                   dispatch_async(dispatch_get_main_queue(), ^{
                     do_notification_callback(duped_ident, ok ? "created" : "creation_failed", "");
                     free(duped_ident);
                   });
                 }];
    }
}


typedef struct {
    char *identifier, *title, *body, *appname, *image_path, *category_id;
    int urgency;
    bool muted;
} QueuedNotification;

typedef struct {
    QueuedNotification *notifications;
    size_t count, capacity;
} NotificationQueue;
static NotificationQueue notification_queue = {0};

static void
queue_notification(
    const char *appname,
    const char *identifier,
    const char *title,
    const char *body,
    const char *image_path,
    int urgency,
    const char *category_id,
    bool muted) {
    ensure_space_for((&notification_queue), notifications, QueuedNotification, notification_queue.count + 16, capacity, 16, true);
    QueuedNotification *n = notification_queue.notifications + notification_queue.count++;
#define d(x) n->x = (x && x[0]) ? strdup(x) : NULL;
    d(appname);
    d(identifier);
    d(title);
    d(body);
    d(image_path);
    d(category_id);
#undef d
    n->urgency = urgency;
    n->muted = muted;
}

static void
drain_pending_notifications(BOOL granted) {
    if (granted) {
        for (size_t i = 0; i < notification_queue.count; i++) {
            QueuedNotification *n = notification_queue.notifications + i;
            schedule_notification(n->appname, n->identifier, n->title, n->body, n->image_path, n->urgency, n->category_id, n->muted);
        }
    }
    while (notification_queue.count) {
        QueuedNotification *n = notification_queue.notifications + --notification_queue.count;
        if (!granted) do_notification_callback(n->identifier, "creation_failed", "");
        free(n->identifier);
        free(n->title);
        free(n->body);
        free(n->appname);
        free(n->image_path);
        free(n->category_id);
        memset(n, 0, sizeof(QueuedNotification));
    }
}

static PyObject *
cocoa_remove_delivered_notification(PyObject *self UNUSED, PyObject *x) {
    if (!PyUnicode_Check(x)) {
        PyErr_SetString(PyExc_TypeError, "identifier must be a string");
        return NULL;
    }
    if (remove_delivered_notification(PyUnicode_AsUTF8(x))) { Py_RETURN_TRUE; }
    Py_RETURN_FALSE;
}

static PyObject *
cocoa_live_delivered_notifications(PyObject *self UNUSED, PyObject *x UNUSED) {
    if (live_delivered_notifications()) { Py_RETURN_TRUE; }
    Py_RETURN_FALSE;
}

static UNNotificationCategory *
category_from_python(PyObject *p) {
    RAII_PyObject(button_ids, PyObject_GetAttrString(p, "button_ids"));
    RAII_PyObject(buttons, PyObject_GetAttrString(p, "buttons"));
    RAII_PyObject(id, PyObject_GetAttrString(p, "id"));
    NSMutableArray<UNNotificationAction *> *actions = [NSMutableArray arrayWithCapacity:PyTuple_GET_SIZE(buttons)];
    for (int i = 0; i < PyTuple_GET_SIZE(buttons); i++)
        [actions addObject:[UNNotificationAction actionWithIdentifier:@(PyUnicode_AsUTF8(PyTuple_GET_ITEM(button_ids, i)))
                                                                title:@(PyUnicode_AsUTF8(PyTuple_GET_ITEM(buttons, i)))
                                                              options:UNNotificationActionOptionNone]];

    return [UNNotificationCategory categoryWithIdentifier:@(PyUnicode_AsUTF8(id)) actions:actions intentIdentifiers:@[] options:0];
}

static bool
set_notification_categories(UNUserNotificationCenter *center, PyObject *categories) {
    NSMutableArray<UNNotificationCategory *> *ans = [NSMutableArray arrayWithCapacity:PyTuple_GET_SIZE(categories)];
    for (int i = 0; i < PyTuple_GET_SIZE(categories); i++) {
        UNNotificationCategory *c = category_from_python(PyTuple_GET_ITEM(categories, i));
        if (!c) return false;
        [ans addObject:c];
    }
    [center setNotificationCategories:[NSSet setWithArray:ans]];
    return true;
}

static PyObject *
cocoa_send_notification(PyObject *self UNUSED, PyObject *args, PyObject *kw) {
    const char *identifier = "", *title = "", *body = "", *appname = "", *image_path = "";
    int urgency = 1;
    PyObject *category, *categories;
    int muted = 0;
    static const char *kwlist[] = {"appname", "identifier", "title", "body", "category", "categories", "image_path", "urgency", "muted", NULL};
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kw,
            "ssssOO!|sip",
            (char **)kwlist,
            &appname,
            &identifier,
            &title,
            &body,
            &category,
            &PyTuple_Type,
            &categories,
            &image_path,
            &urgency,
            &muted))
        return NULL;

    UNUserNotificationCenter *center = get_notification_center_safely();
    if (!center) Py_RETURN_NONE;
    if (!center.delegate) center.delegate = [[NotificationDelegate alloc] init];
    if (PyObject_IsTrue(categories))
        if (!set_notification_categories(center, categories)) return NULL;
    RAII_PyObject(category_id, PyObject_GetAttrString(category, "id"));
    queue_notification(appname, identifier, title, body, image_path, urgency, PyUnicode_AsUTF8(category_id), muted);

    // The badge permission needs to be requested as well, even though it is not used,
    // otherwise macOS refuses to show the preference checkbox for enable/disable notification sound.
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge)
                          completionHandler:^(BOOL granted, NSError *_Nullable error) {
                            if (!granted && error != nil) {
                                log_error("Failed to request permission for showing notification: %s", [[error localizedDescription] UTF8String]);
                            }
                            dispatch_async(dispatch_get_main_queue(), ^{ drain_pending_notifications(granted); });
                          }];
    Py_RETURN_NONE;
}

@interface ServiceProvider : NSObject
@end

@implementation ServiceProvider

- (BOOL)openTab:(NSPasteboard *)pasteboard userData:(NSString *)UNUSED userData error:(NSError **)UNUSED error {
    return [self openDirsFromPasteboard:pasteboard type:NEW_TAB_WITH_WD];
}

- (BOOL)openOSWindow:(NSPasteboard *)pasteboard userData:(NSString *)UNUSED userData error:(NSError **)UNUSED error {
    return [self openDirsFromPasteboard:pasteboard type:NEW_OS_WINDOW_WITH_WD];
}

- (BOOL)openDirsFromPasteboard:(NSPasteboard *)pasteboard type:(int)type {
    NSDictionary *options = @{NSPasteboardURLReadingFileURLsOnlyKey : @YES};
    NSArray *filePathArray = [pasteboard readObjectsForClasses:[NSArray arrayWithObject:[NSURL class]] options:options];
    NSMutableArray<NSString *> *dirPathArray = [NSMutableArray arrayWithCapacity:[filePathArray count]];
    for (NSURL *url in filePathArray) {
        NSString *path = [url path];
        BOOL isDirectory = NO;
        if ([[NSFileManager defaultManager] fileExistsAtPath:path isDirectory:&isDirectory]) {
            if (!isDirectory) path = [path stringByDeletingLastPathComponent];
            if (![dirPathArray containsObject:path]) [dirPathArray addObject:path];
        }
    }
    if ([dirPathArray count] > 0) {
        // Colons are not valid in paths under macOS.
        set_cocoa_pending_action(type, [[dirPathArray componentsJoinedByString:@":"] UTF8String]);
    }
    return YES;
}

- (BOOL)openFileURLs:(NSPasteboard *)pasteboard userData:(NSString *)UNUSED userData error:(NSError **)UNUSED error {
    NSDictionary *options = @{NSPasteboardURLReadingFileURLsOnlyKey : @YES};
    NSArray *urlArray = [pasteboard readObjectsForClasses:[NSArray arrayWithObject:[NSURL class]] options:options];
    for (NSURL *url in urlArray) {
        NSString *path = [url path];
        if ([[NSFileManager defaultManager] fileExistsAtPath:path]) {
            set_cocoa_pending_action(LAUNCH_URLS, [[[NSURL fileURLWithPath:path] absoluteString] UTF8String]);
        }
    }
    return YES;
}

- (void)quickAccessTerminal:(NSPasteboard *)pboard userData:(NSString *)userData error:(NSString **)error {
    // we ignore event during application launch as it will cause the window to be shown and hidden
    static bool is_first_event = true;
    if (!is_first_event || monotonic() >= s_double_to_monotonic_t(2.0)) { call_boss(quick_access_terminal_invoked, NULL); }
    is_first_event = false;
}
@end

// global menu {{{

static void
add_user_global_menu_entry(struct MenuItem *e, NSMenu *bar, size_t action_index) {
    NSMenu *parent = bar;
    UserMenuItem *final_item = nil;
    GlobalMenuTarget *global_menu_target = [GlobalMenuTarget shared_instance];
    for (size_t i = 0; i < e->location_count; i++) {
        NSMenuItem *item = [parent itemWithTitle:@(e->location[i])];
        if (!item) {
            final_item = [[UserMenuItem alloc] initWithTitle:@(e->location[i]) action:@selector(user_menu_action:) keyEquivalent:@""];
            final_item.target = global_menu_target;
            [parent addItem:final_item];
            item = final_item;
            [final_item release];
        }
        if (i + 1 < e->location_count) {
            if (![item hasSubmenu]) {
                NSMenu *sub_menu = [[NSMenu alloc] initWithTitle:item.title];
                [item setSubmenu:sub_menu];
                [sub_menu release];
            }
            parent = [item submenu];
            if (!parent) return;
        }
    }
    if (final_item != nil) { final_item.action_index = action_index; }
}

static void
cocoa_create_global_menu(void) {
    NSString *app_name = find_app_name();
    NSMenu *bar = [[NSMenu alloc] init];
    GlobalMenuTarget *global_menu_target = [GlobalMenuTarget shared_instance];
    [NSApp setMainMenu:bar];

#define MENU_ITEM(menu, title, name)                                                                                         \
    {                                                                                                                        \
        NSMenuItem *__mi = [menu addItemWithTitle:title action:@selector(name:) keyEquivalent:@(global_shortcuts.name.key)]; \
        [__mi setKeyEquivalentModifierMask:global_shortcuts.name.mods];                                                      \
        [__mi setTarget:global_menu_target];                                                                                 \
    }

    NSMenuItem *appMenuItem = [bar addItemWithTitle:@"" action:NULL keyEquivalent:@""];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenuItem setSubmenu:appMenu];

    [appMenu addItemWithTitle:[NSString stringWithFormat:@"About %@", app_name] action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(appMenu, @"Preferences…", edit_config_file);
    MENU_ITEM(appMenu, @"Reload Preferences", reload_config);
    [appMenu addItem:[NSMenuItem separatorItem]];

    NSMenu *servicesMenu = [[NSMenu alloc] init];
    [NSApp setServicesMenu:servicesMenu];
    [[appMenu addItemWithTitle:@"Services" action:NULL keyEquivalent:@""] setSubmenu:servicesMenu];
    [servicesMenu release];
    [appMenu addItem:[NSMenuItem separatorItem]];

    MENU_ITEM(appMenu, ([NSString stringWithFormat:@"Hide %@", app_name]), hide_macos_app);
    MENU_ITEM(appMenu, @"Hide Others", hide_macos_other_apps);
    [appMenu addItemWithTitle:@"Show All" action:@selector(unhideAllApplications:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];

    MENU_ITEM(appMenu, @"Secure Keyboard Entry", toggle_macos_secure_keyboard_entry);
    [appMenu addItem:[NSMenuItem separatorItem]];

    MENU_ITEM(appMenu, ([NSString stringWithFormat:@"Quit %@", app_name]), quit);
    [appMenu release];

    NSMenuItem *shellMenuItem = [bar addItemWithTitle:@"Shell" action:NULL keyEquivalent:@""];
    NSMenu *shellMenu = [[NSMenu alloc] initWithTitle:@"Shell"];
    [shellMenuItem setSubmenu:shellMenu];
    MENU_ITEM(shellMenu, @"New OS Window", new_os_window);
    MENU_ITEM(shellMenu, @"New Tab", new_tab);
    MENU_ITEM(shellMenu, @"New Window", new_window);
    [shellMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(shellMenu, @"Close OS Window", close_os_window);
    MENU_ITEM(shellMenu, @"Close Tab", close_tab);
    MENU_ITEM(shellMenu, @"Close Window", close_window);
    [shellMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(shellMenu, @"Reset", reset_terminal);
    [shellMenu release];
    NSMenuItem *editMenuItem = [bar addItemWithTitle:@"Edit" action:NULL keyEquivalent:@""];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [editMenuItem setSubmenu:editMenu];
    MENU_ITEM(editMenu, @"Clear to Start", clear_terminal_and_scrollback);
    MENU_ITEM(editMenu, @"Clear Scrollback", clear_scrollback);
    MENU_ITEM(editMenu, @"Clear Screen", clear_screen);
    MENU_ITEM(editMenu, @"Clear Last Command", clear_last_command);
    MENU_ITEM(editMenu, @"Find", search_scrollback);
    [editMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(editMenu, @"Copy", copy_or_noop);
    MENU_ITEM(editMenu, @"Paste", paste_from_clipboard);
    [editMenu release];

    NSMenuItem *windowMenuItem = [bar addItemWithTitle:@"Window" action:NULL keyEquivalent:@""];
    NSMenu *windowMenu = [[NSMenu alloc] initWithTitle:@"Window"];
    [windowMenuItem setSubmenu:windowMenu];

    MENU_ITEM(windowMenu, @"Minimize", minimize_macos_window);
    [windowMenu addItemWithTitle:@"Zoom" action:@selector(performZoom:) keyEquivalent:@""];
    [windowMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(windowMenu, @"Cycle Through OS Windows", macos_cycle_through_os_windows);
    MENU_ITEM(windowMenu, @"Cycle Through OS Windows backwards", macos_cycle_through_os_windows_backwards);
    [windowMenu addItem:[NSMenuItem separatorItem]];
    [windowMenu addItemWithTitle:@"Bring All to Front" action:@selector(arrangeInFront:) keyEquivalent:@""];

    [windowMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(windowMenu, @"Show Previous Tab", previous_tab);
    MENU_ITEM(windowMenu, @"Show Next Tab", next_tab);
    MENU_ITEM(windowMenu, @"Show All Tabs", toggle_macos_tab_overview);
    [[windowMenu addItemWithTitle:@"Move Tab to New Window" action:@selector(detach_tab:) keyEquivalent:@""] setTarget:global_menu_target];
    // These two have no kitty equivalent, so use AppKit's implementations. They
    // need allowsAutomaticWindowTabbing to stay on, which also makes AppKit
    // inject its own copies of the four items above; WindowMenuDelegate strips
    // those duplicates when the menu opens.
    [windowMenu addItemWithTitle:@"Merge All Windows" action:@selector(mergeAllWindows:) keyEquivalent:@""];
    [windowMenu addItemWithTitle:@"Show Tab Bar" action:@selector(toggleTabBar:) keyEquivalent:@""];

    [windowMenu addItem:[NSMenuItem separatorItem]];
    MENU_ITEM(windowMenu, @"Enter Full Screen", toggle_fullscreen);
    static WindowMenuDelegate *window_menu_delegate = nil;
    if (!window_menu_delegate) window_menu_delegate = [[WindowMenuDelegate alloc] init];
    windowMenu.delegate = window_menu_delegate;
    [NSApp setWindowsMenu:windowMenu];
    [windowMenu release];

    NSMenuItem *helpMenuItem = [bar addItemWithTitle:@"Help" action:NULL keyEquivalent:@""];
    NSMenu *helpMenu = [[NSMenu alloc] initWithTitle:@"Help"];
    [helpMenuItem setSubmenu:helpMenu];

    MENU_ITEM(helpMenu, @"Visit kitty Website", open_kitty_website);
    [NSApp setHelpMenu:helpMenu];
    [helpMenu release];

    if (OPT(global_menu.entries)) {
        for (size_t i = 0; i < OPT(global_menu.count); i++) {
            struct MenuItem *e = OPT(global_menu.entries) + i;
            if (e->definition && e->location && e->location_count > 1) { add_user_global_menu_entry(e, bar, i); }
        }
    }
    [bar release];


    class_addMethod(object_getClass([NSApp delegate]), @selector(applicationDockMenu:), (IMP)get_dock_menu, "@@:@");


    [NSApp setServicesProvider:[[[ServiceProvider alloc] init] autorelease]];

#undef MENU_ITEM
}

void
cocoa_application_lifecycle_event(bool application_launch_finished) {
    if (!application_launch_finished) cocoa_create_global_menu(); // applicationWillFinishLaunching
}

void
cocoa_update_menu_bar_title(PyObject *pytitle) {
    if (!pytitle) return;
    NSString *title = nil;
    if (OPT(macos_menubar_title_max_length) > 0 && PyUnicode_GetLength(pytitle) > OPT(macos_menubar_title_max_length)) {
        static char fmt[64];
        snprintf(fmt, sizeof(fmt), "%%%ld.%ldU%%s", OPT(macos_menubar_title_max_length), OPT(macos_menubar_title_max_length));
        RAII_PyObject(st, PyUnicode_FromFormat(fmt, pytitle, "…"));
        if (st) title = @(PyUnicode_AsUTF8(st));
        else PyErr_Print();
    } else {
        title = @(PyUnicode_AsUTF8(pytitle));
    }
    if (!title) return;
    NSString *menuTitle = [NSString stringWithFormat:@" :: %@", title];
    if (title_menu != NULL) {
        [[title_menu submenu] setTitle:menuTitle];
    } else {
        NSMenu *bar = [NSApp mainMenu];
        title_menu = [bar addItemWithTitle:@"" action:NULL keyEquivalent:@""];
        NSMenu *m = [[NSMenu alloc] initWithTitle:menuTitle];
        [title_menu setSubmenu:m];
        [m release];
    }
}

void
cocoa_clear_global_shortcuts(void) {
    memset(&global_shortcuts, 0, sizeof(global_shortcuts));
}

void
cocoa_recreate_global_menu(void) {
    if (title_menu != NULL) {
        NSMenu *bar = [NSApp mainMenu];
        [bar removeItem:title_menu];
    }
    title_menu = NULL;
    if (secure_input_title_menu != NULL) {
        NSMenu *bar = [NSApp mainMenu];
        [bar removeItem:secure_input_title_menu];
    }
    secure_input_title_menu = NULL;
    cocoa_create_global_menu();
    SecureKeyboardEntryController *k = [SecureKeyboardEntryController sharedInstance];
    update_secure_input_menu_bar_indicator(k.isDesired);
}


// }}}

#define NSLeftAlternateKeyMask (0x000020 | NSEventModifierFlagOption)
#define NSRightAlternateKeyMask (0x000040 | NSEventModifierFlagOption)

bool
cocoa_alt_option_key_pressed(NSUInteger flags) {
    NSUInteger q = (OPT(macos_option_as_alt) == 1) ? NSRightAlternateKeyMask : NSLeftAlternateKeyMask;
    return (q & flags) == q;
}

void
cocoa_toggle_secure_keyboard_entry(void) {
    SecureKeyboardEntryController *k = [SecureKeyboardEntryController sharedInstance];
    [k toggle];
    [[NSUserDefaults standardUserDefaults] setBool:k.isDesired forKey:@"SecureKeyboardEntry"];
    update_secure_input_menu_bar_indicator(k.isDesired);
}

void
cocoa_hide(void) {
    [[NSApplication sharedApplication] performSelectorOnMainThread:@selector(hide:) withObject:nil waitUntilDone:NO];
}

void
cocoa_hide_others(void) {
    [[NSApplication sharedApplication] performSelectorOnMainThread:@selector(hideOtherApplications:) withObject:nil waitUntilDone:NO];
}

void
cocoa_minimize(void *w) {
    NSWindow *window = (NSWindow *)w;
    if (window && !window.miniaturized) [window performSelectorOnMainThread:@selector(performMiniaturize:) withObject:nil waitUntilDone:NO];
}

void
cocoa_focus_window(void *w) {
    NSWindow *window = (NSWindow *)w;
    [window makeKeyWindow];
}

long
cocoa_window_number(void *w) {
    NSWindow *window = (NSWindow *)w;
    return [window windowNumber];
}

void
cocoa_set_native_tab_title(void *w, const char *title) {
    NSWindow *window = (NSWindow *)w;
    // Keep tab labels independent of explicit OS window title overrides.
    if (@available(macOS 10.13, *)) window.tab.title = @(title);
}

size_t
cocoa_get_workspace_ids(void *w, size_t *workspace_ids, size_t array_sz) {
    NSWindow *window = (NSWindow *)w;
    if (!window) return 0;
    NSArray *window_array = @[ @([window windowNumber]) ];
    CFArrayRef spaces = CGSCopySpacesForWindows(_CGSDefaultConnection(), kCGSSpaceAll, (__bridge CFArrayRef)window_array);
    CFIndex ans = CFArrayGetCount(spaces);
    if (ans > 0) {
        for (CFIndex i = 0; i < MIN(ans, (CFIndex)array_sz); i++) {
            NSNumber *s = (NSNumber *)CFArrayGetValueAtIndex(spaces, i);
            workspace_ids[i] = [s intValue];
        }
    } else ans = 0;
    CFRelease(spaces);
    return ans;
}

static PyObject *
cocoa_get_lang(PyObject UNUSED *self, PyObject *args UNUSED) {
    @autoreleasepool {
        NSString *lang_code = [[NSLocale currentLocale] languageCode];
        NSString *country_code = [[NSLocale currentLocale] objectForKey:NSLocaleCountryCode];
        NSString *identifier = [[NSLocale currentLocale] localeIdentifier];
        return Py_BuildValue(
            "sss", lang_code ? [lang_code UTF8String] : "", country_code ? [country_code UTF8String] : "", identifier ? [identifier UTF8String] : "");
    } // autoreleasepool
}

monotonic_t
cocoa_cursor_blink_interval(void) {
    @autoreleasepool {
        NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
        double on_period_ms = [defaults doubleForKey:@"NSTextInsertionPointBlinkPeriodOn"];
        double off_period_ms = [defaults doubleForKey:@"NSTextInsertionPointBlinkPeriodOff"];
        double period_ms = [defaults doubleForKey:@"NSTextInsertionPointBlinkPeriod"];
        double max_value = 60 * 1000.0, ans = -1.0;
        if (on_period_ms != 0. || off_period_ms != 0.) {
            ans = on_period_ms + off_period_ms;
        } else if (period_ms != 0.) {
            ans = period_ms;
        }
        return ans > max_value ? 0ll : ms_double_to_monotonic_t(ans);

    } // autoreleasepool
}

void
cocoa_set_activation_policy(bool hide_from_tasks) {
    [NSApp setActivationPolicy:(hide_from_tasks ? NSApplicationActivationPolicyAccessory : NSApplicationActivationPolicyRegular)];
}

static PyObject *
cocoa_set_url_handler(PyObject UNUSED *self, PyObject *args) {
    @autoreleasepool {
        const char *url_scheme = NULL, *bundle_id = NULL;
        if (!PyArg_ParseTuple(args, "s|z", &url_scheme, &bundle_id)) return NULL;
        if (!url_scheme || url_scheme[0] == '\0') {
            PyErr_SetString(PyExc_TypeError, "Empty url scheme");
            return NULL;
        }

        NSString *scheme = [NSString stringWithUTF8String:url_scheme];
        NSString *identifier = @"";
        if (!bundle_id) {
            identifier = [[NSBundle mainBundle] bundleIdentifier];
            if (!identifier || identifier.length == 0) identifier = @"net.kovidgoyal.kitty";
        } else if (bundle_id[0] != '\0') {
            identifier = [NSString stringWithUTF8String:bundle_id];
        }
        // This API has been marked as deprecated. It will need to be replaced when a new approach is available.
        OSStatus err = LSSetDefaultHandlerForURLScheme((CFStringRef)scheme, (CFStringRef)identifier);
        if (err == noErr) Py_RETURN_NONE;
        PyErr_Format(PyExc_OSError, "Failed to set default handler with error code: %d", err);
        return NULL;
    } // autoreleasepool
}

static PyObject *
cocoa_set_app_icon(PyObject UNUSED *self, PyObject *args) {
    @autoreleasepool {
        const char *icon_path = NULL, *app_path = NULL;
        if (!PyArg_ParseTuple(args, "s|z", &icon_path, &app_path)) return NULL;
        if (!icon_path || icon_path[0] == '\0') {
            PyErr_SetString(PyExc_TypeError, "Empty icon file path");
            return NULL;
        }
        NSString *custom_icon_path = [NSString stringWithUTF8String:icon_path];
        if (![[NSFileManager defaultManager] fileExistsAtPath:custom_icon_path]) {
            PyErr_Format(PyExc_FileNotFoundError, "Icon file not found: %s", [custom_icon_path UTF8String]);
            return NULL;
        }

        NSString *bundle_path = @"";
        if (!app_path) {
            bundle_path = [[NSBundle mainBundle] bundlePath];
            if (!bundle_path || bundle_path.length == 0) bundle_path = @"/Applications/kitty.app";
            // When compiled from source and run from the launcher folder the bundle path should be `kitty.app` in it
            if (![bundle_path hasSuffix:@".app"]) {
                NSString *launcher_app_path = [bundle_path stringByAppendingPathComponent:@"kitty.app"];
                bundle_path = @"";
                BOOL is_dir;
                if ([[NSFileManager defaultManager] fileExistsAtPath:launcher_app_path isDirectory:&is_dir] && is_dir &&
                    [[NSWorkspace sharedWorkspace] isFilePackageAtPath:launcher_app_path]) {
                    bundle_path = launcher_app_path;
                }
            }
        } else if (app_path[0] != '\0') {
            bundle_path = [NSString stringWithUTF8String:app_path];
        }
        if (!bundle_path || bundle_path.length == 0 || ![[NSFileManager defaultManager] fileExistsAtPath:bundle_path]) {
            PyErr_Format(PyExc_FileNotFoundError, "Application bundle not found: %s", [bundle_path UTF8String]);
            return NULL;
        }

        NSImage *icon_image = [[NSImage alloc] initWithContentsOfFile:custom_icon_path];
        BOOL result = [[NSWorkspace sharedWorkspace] setIcon:icon_image forFile:bundle_path options:NSExcludeQuickDrawElementsIconCreationOption];
        [icon_image release];
        if (result) Py_RETURN_NONE;
        PyErr_Format(PyExc_OSError, "Failed to set custom icon %s for %s", [custom_icon_path UTF8String], [bundle_path UTF8String]);
        return NULL;

    } // autoreleasepool
}

static NSImage *custom_dock_icon = nil;

static PyObject *
cocoa_set_dock_icon(PyObject UNUSED *self, PyObject *args) {
    @autoreleasepool {
        const char *icon_path = NULL;
        if (!PyArg_ParseTuple(args, "s", &icon_path)) return NULL;
        if (!icon_path || icon_path[0] == '\0') {
            PyErr_SetString(PyExc_TypeError, "Empty icon file path");
            return NULL;
        }
        NSString *custom_icon_path = [NSString stringWithUTF8String:icon_path];
        if ([[NSFileManager defaultManager] fileExistsAtPath:custom_icon_path]) {
            NSImage *icon_image = [[[NSImage alloc] initWithContentsOfFile:custom_icon_path] autorelease];
            [NSApplication sharedApplication].applicationIconImage = icon_image;
            if (custom_dock_icon) [custom_dock_icon release];
            custom_dock_icon = [icon_image retain];
            Py_RETURN_NONE;
        }
        return NULL;

    } // autoreleasepool
}

static NSSound *beep_sound = nil;

static void
cleanup(void) {
    @autoreleasepool {
        if (dockMenu) [dockMenu release];
        dockMenu = nil;
        if (beep_sound) [beep_sound release];
        beep_sound = nil;

        drain_pending_notifications(NO);
        free(notification_queue.notifications);
        notification_queue.notifications = NULL;
        notification_queue.capacity = 0;

    } // autoreleasepool
}

void
cocoa_system_beep(const char *path) {
    if (!path) {
        NSBeep();
        return;
    }
    static const char *beep_path = NULL;
    if (beep_path != path) {
        if (beep_sound) [beep_sound release];
        beep_sound = [[NSSound alloc] initWithContentsOfFile:@(path) byReference:YES];
    }
    if (beep_sound) [beep_sound play];
    else NSBeep();
}

static void
uncaughtExceptionHandler(NSException *exception) {
    log_error("Unhandled exception in Cocoa: %s", [[exception description] UTF8String]);
    log_error("Stack trace:\n%s", [[exception.callStackSymbols description] UTF8String]);
}

void
cocoa_set_uncaught_exception_handler(void) {
    NSSetUncaughtExceptionHandler(&uncaughtExceptionHandler);
}

static PyObject *
convert_imagerep_to_png(NSBitmapImageRep *rep, const char *output_path) {
    NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{NSImageCompressionFactor : @1.0}]; // autoreleased

    if (output_path) {
        if (![png writeToFile:@(output_path) atomically:YES]) {
            PyErr_Format(PyExc_OSError, "Failed to write PNG data to %s", output_path);
            return NULL;
        }
        return PyBytes_FromStringAndSize(NULL, 0);
    }
    return PyBytes_FromStringAndSize(png.bytes, png.length);
}

static PyObject *
convert_image_to_png(NSImage *icon, unsigned image_size, const char *output_path) {
    NSRect r = NSMakeRect(0, 0, image_size, image_size);
    RAII_CoreFoundation(CGColorSpaceRef, colorSpace, CGColorSpaceCreateWithName(kCGColorSpaceGenericRGB));
    RAII_CoreFoundation(
        CGContextRef,
        cgContext,
        CGBitmapContextCreate(NULL, image_size, image_size, 8, 4 * image_size, colorSpace, kCGBitmapByteOrderDefault | kCGImageAlphaPremultipliedLast));
    NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithCGContext:cgContext flipped:NO]; // autoreleased
    CGImageRef cg = [icon CGImageForProposedRect:&r context:context hints:nil];
    NSBitmapImageRep *rep = [[[NSBitmapImageRep alloc] initWithCGImage:cg] autorelease];
    return convert_imagerep_to_png(rep, output_path);
}

static PyObject *
render_emoji(NSString *text, unsigned image_size, const char *output_path) {
    NSFont *font = [NSFont fontWithName:@"AppleColorEmoji" size:12];
    CTFontRef ctfont = (__bridge CTFontRef)(font);
    CGFloat line_height = MAX(1, floor(CTFontGetAscent(ctfont) + CTFontGetDescent(ctfont) + MAX(0, CTFontGetLeading(ctfont)) + 0.5));
    CGFloat pts_per_px = CTFontGetSize(ctfont) / line_height;
    CGFloat desired_size = image_size * pts_per_px;
    NSFont *final_font = [NSFont fontWithName:@"AppleColorEmoji" size:desired_size];
    NSAttributedString *attr_string = [[[NSAttributedString alloc] initWithString:text attributes:@{NSFontAttributeName : final_font}] autorelease];
    NSBitmapImageRep *bmp = [[[NSBitmapImageRep alloc] initWithBitmapDataPlanes:nil
                                                                     pixelsWide:image_size
                                                                     pixelsHigh:image_size
                                                                  bitsPerSample:8
                                                                samplesPerPixel:4
                                                                       hasAlpha:YES
                                                                       isPlanar:NO
                                                                 colorSpaceName:NSDeviceRGBColorSpace
                                                                    bytesPerRow:0
                                                                   bitsPerPixel:0] autorelease];
    [NSGraphicsContext saveGraphicsState];
    NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithBitmapImageRep:bmp];
    [NSGraphicsContext setCurrentContext:context];
    [attr_string drawInRect:NSMakeRect(0, 0, image_size, image_size)];
    [NSGraphicsContext restoreGraphicsState];
    return convert_imagerep_to_png(bmp, output_path);
}


static PyObject *
bundle_image_as_png(PyObject *self UNUSED, PyObject *args, PyObject *kw) {
    @autoreleasepool {
        const char *b, *output_path = NULL;
        int image_type = 1;
        unsigned image_size = 256;
        static const char *kwlist[] = {"path_or_identifier", "output_path", "image_size", "image_type", NULL};
        if (!PyArg_ParseTupleAndKeywords(args, kw, "s|sIi", (char **)kwlist, &b, &output_path, &image_size, &image_type)) return NULL;
        NSImage *icon = nil;
        switch (image_type) {
            case 0:
            case 1: {
                NSWorkspace *workspace = [NSWorkspace sharedWorkspace]; // autoreleased
                if (image_type == 1) {
                    NSURL *url = [workspace URLForApplicationWithBundleIdentifier:@(b)]; // autoreleased
                    if (!url) {
                        PyErr_Format(PyExc_KeyError, "Failed to find bundle path for identifier: %s", b);
                        return NULL;
                    }
                    icon = [workspace iconForFile:@(url.fileSystemRepresentation)];
                } else icon = [workspace iconForFile:@(b)];
            } break;
            case 2: return render_emoji(@(b), image_size, output_path);
            default:
                if (@available(macOS 11.0, *)) {
                    icon = [NSImage imageWithSystemSymbolName:@(b) accessibilityDescription:@""]; // autoreleased
                } else {
                    PyErr_SetString(PyExc_ValueError, "Your version of macOS is too old to use symbol images, need >= 11.0");
                    return NULL;
                }
                break;
        }
        if (!icon) {
            PyErr_Format(PyExc_ValueError, "Failed to load icon for bundle: %s", b);
            return NULL;
        }
        return convert_image_to_png(icon, image_size, output_path);
    }
}

static PyObject *
play_system_sound_by_id_async(PyObject *self UNUSED, PyObject *which) {
    if (!PyLong_Check(which)) {
        PyErr_SetString(PyExc_TypeError, "system sound id must be an integer");
        return NULL;
    }
    AudioServicesPlaySystemSound(PyLong_AsUnsignedLong(which));
    Py_RETURN_NONE;
}

// Dock Progress bar {{{
@interface RoundedRectangleView : NSView {
    unsigned intermediate_step;
    CGFloat fill_fraction;
    BOOL is_indeterminate;
}
- (void)animate;
- (BOOL)isIndeterminate;
- (void)setIndeterminate:(BOOL)val;
- (void)setFraction:(CGFloat)fraction;
@end

@implementation RoundedRectangleView

- (void)animate {
    intermediate_step++;
}
- (BOOL)isIndeterminate {
    return is_indeterminate;
}
- (void)setIndeterminate:(BOOL)val {
    if (val != is_indeterminate) {
        is_indeterminate = val;
        intermediate_step = 0;
    }
}
- (void)setFraction:(CGFloat)fraction {
    fill_fraction = fraction;
}


- (void)drawRect:(NSRect)dirtyRect {
    [super drawRect:dirtyRect];

    NSRect bar = NSInsetRect(self.bounds, 4, 4);
    CGFloat cornerRadius = self.bounds.size.height / 4.0;

#define fill(bar) [[NSBezierPath bezierPathWithRoundedRect:bar xRadius:cornerRadius yRadius:cornerRadius] fill]
    // Create the border
    [[[NSColor whiteColor] colorWithAlphaComponent:0.8] setFill];
    fill(bar);
    // Create the background
    [[[NSColor blackColor] colorWithAlphaComponent:0.8] setFill];
    fill(NSInsetRect(bar, 0.5, 0.5));
    // Create the progress
    NSRect bar_progress = NSInsetRect(bar, 1, 1);
    if (intermediate_step) {
        unsigned num_of_steps = 80;
        intermediate_step = intermediate_step % num_of_steps;
        bar_progress.size.width = self.bounds.size.width / 8;
        float frac = intermediate_step / (float)num_of_steps;
        bar_progress.origin.x += (self.bounds.size.width - bar_progress.size.width) * frac;
    } else bar_progress.size.width *= fill_fraction;
    [[NSColor whiteColor] setFill];
    fill(bar_progress);
#undef fill
}

@end
static NSView *dock_content_view = nil;
static NSImageView *dock_image_view = nil;
static RoundedRectangleView *dock_pbar = nil;

static void animate_dock_progress_bar(id_type timer_id UNUSED, void *data UNUSED);

static void
tick_dock_pbar(void) {
    add_main_loop_timer(ms_to_monotonic_t(20), false, animate_dock_progress_bar, NULL, NULL);
}

static void
animate_dock_progress_bar(id_type timer_id UNUSED, void *data UNUSED) {
    if (dock_pbar != nil && [dock_pbar isIndeterminate]) {
        [dock_pbar animate];
        NSDockTile *dockTile = [NSApp dockTile];
        [dockTile display];
        tick_dock_pbar();
    }
}

static PyObject *
cocoa_show_progress_bar_on_dock_icon(PyObject *self UNUSED, PyObject *args) {
    float percent = -100;
    if (!PyArg_ParseTuple(args, "|f", &percent)) return NULL;
    NSDockTile *dockTile = [NSApp dockTile];
    const bool remove_pbar = percent < 0;
    if (!dock_content_view) {
        if (remove_pbar) Py_RETURN_NONE; // nothing to remove, leave the dock tile alone
        dock_content_view = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, dockTile.size.width, dockTile.size.height)];
        dock_content_view.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        dock_image_view = [NSImageView.alloc initWithFrame:dock_content_view.frame];
        dock_image_view.imageScaling = NSImageScaleProportionallyDown;
        dock_image_view.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        [dock_content_view addSubview:dock_image_view];
        dock_pbar = [[RoundedRectangleView alloc] initWithFrame:NSMakeRect(0, 0, dockTile.size.width, dockTile.size.height / 4)];
        dock_pbar.autoresizingMask = NSViewWidthSizable | NSViewMaxYMargin;
        [dock_content_view addSubview:dock_pbar];
    }
    NSImage *icon_image = custom_dock_icon ? custom_dock_icon : NSApp.applicationIconImage;
    if (dock_image_view.image != icon_image) dock_image_view.image = icon_image;
    [dock_content_view setFrameSize:dockTile.size];
    [dock_image_view setFrameSize:dockTile.size];
    if (percent >= 0 && percent <= 100) {
        [dock_pbar setFraction:percent / 100.];
        [dock_pbar setIndeterminate:NO];
    } else if (percent > 100) {
        if (![dock_pbar isIndeterminate]) {
            [dock_pbar setIndeterminate:YES];
            tick_dock_pbar();
        }
    } else [dock_pbar setIndeterminate:NO]; // stops the animation timer, if running
    [dock_pbar setFrameSize:NSMakeSize(dockTile.size.width - 20, 20)];
    [dock_pbar setFrameOrigin:NSMakePoint(10, -2)];
    dock_pbar.hidden = remove_pbar;
    // Re-apply the custom icon in case macOS has reverted it, so that the
    // icon used outside the Dock (cmd+tab, notifications, etc.) is correct
    if (custom_dock_icon != nil && NSApp.applicationIconImage != custom_dock_icon) NSApp.applicationIconImage = custom_dock_icon;
    // On some macOS versions setting the content view to nil causes the Dock
    // to redraw the tile with the bundle icon before the custom icon is
    // restored, causing a flicker. So when there is a custom icon, keep the
    // content view, which draws the custom icon itself, just without the
    // progress bar.
    [dockTile setContentView:(remove_pbar && custom_dock_icon == nil) ? nil : dock_content_view];
    [dockTile display];
    Py_RETURN_NONE;
}
// }}}

// Dock badge {{{

static bool dock_badge_is_set = false;

void
cocoa_set_dock_badge(const char *label) {
    @autoreleasepool {
        NSDockTile *dockTile = [NSApp dockTile];
        [dockTile setBadgeLabel:label ? @(label) : nil];
        [dockTile display];
        dock_badge_is_set = (label != NULL);
    }
}

void
cocoa_clear_dock_badge_if_set(void) {
    if (dock_badge_is_set) cocoa_set_dock_badge(NULL);
}

// }}}

static PyObject *
cocoa_is_secure_input_enabled(PyObject *self UNUSED, PyObject *args UNUSED) {
    SecureKeyboardEntryController *k = [SecureKeyboardEntryController sharedInstance];
    return Py_NewRef(k.isDesired ? Py_True : Py_False);
}

static PyObject *
cocoa_get_machine_id(PyObject *self UNUSED, PyObject *args UNUSED) {
    static char ans[1024] = {0};
    static bool done = false;
    if (!done) {
        done = true;
        CFMutableDictionaryRef matching = IOServiceMatching("IOPlatformExpertDevice");
        // Get the matching service
        io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, matching);
        if (service) {
            CFTypeRef uuid = IORegistryEntryCreateCFProperty(service, CFSTR("IOPlatformUUID"), kCFAllocatorDefault, 0);
            if (uuid) {
                // Transfer ownership to NSString using ARC __bridge_transfer
                NSString *s = (NSString *)uuid;
                [s getCString:ans maxLength:sizeof(ans) encoding:NSUTF8StringEncoding];
            }
            // Release the I/O object
            IOObjectRelease(service);
        }
    }
    return PyUnicode_FromString(ans);
}

// Keychain {{{
// These never show any UI, such as a prompt to unlock the keychain or to allow kitty to access an item, failing instead.

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
// The legacy file based keychain, which is the one used by default, shows its prompts unless interaction is disallowed with this
// deprecated, process wide, API, which has no replacement.
typedef struct {
    Boolean previous;
    bool is_set;
} KeychainInteraction;

static KeychainInteraction
disallow_keychain_interaction(void) {
    KeychainInteraction ans = {0};
    if (SecKeychainGetUserInteractionAllowed(&ans.previous) == errSecSuccess) ans.is_set = SecKeychainSetUserInteractionAllowed(false) == errSecSuccess;
    return ans;
}

static void
restore_keychain_interaction(KeychainInteraction x) {
    if (x.is_set) SecKeychainSetUserInteractionAllowed(x.previous);
}

static void
set_keychain_query_ui_fail(CFMutableDictionaryRef q) {
    CFDictionarySetValue(q, kSecUseAuthenticationUI, kSecUseAuthenticationUIFail);
}
#pragma clang diagnostic pop

static CFMutableDictionaryRef
keychain_query(const char *service, const char *account) {
    // account can be NULL to match all items of service
    CFMutableDictionaryRef q = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFStringRef s = CFStringCreateWithCString(NULL, service, kCFStringEncodingUTF8);
    CFStringRef a = account ? CFStringCreateWithCString(NULL, account, kCFStringEncodingUTF8) : NULL;
    if (!q || !s || (account && !a)) {
        if (q) CFRelease(q);
        if (s) CFRelease(s);
        if (a) CFRelease(a);
        PyErr_NoMemory();
        return NULL;
    }
    CFDictionarySetValue(q, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(q, kSecAttrService, s);
    CFRelease(s);
    if (a) {
        CFDictionarySetValue(q, kSecAttrAccount, a);
        CFRelease(a);
    }
    return q;
}

static PyObject *
set_keychain_error(OSStatus status, const char *action) {
    char msg[512] = {0};
    CFStringRef m = SecCopyErrorMessageString(status, NULL);
    if (m) {
        CFStringGetCString(m, msg, sizeof(msg), kCFStringEncodingUTF8);
        CFRelease(m);
    }
    PyErr_Format(PyExc_OSError, "Failed to %s the keychain: %s (error code: %d)", action, msg, (int)status);
    return NULL;
}

static PyObject *
cocoa_keychain_store_secret(PyObject *self UNUSED, PyObject *args) {
    const char *service, *account, *label;
    PyObject *secret;
    if (!PyArg_ParseTuple(args, "sssO", &service, &account, &label, &secret)) return NULL;
    const void *data;
    size_t len;
    if (!secret_data(secret, &data, &len)) return NULL;
    CFMutableDictionaryRef q = keychain_query(service, account);
    if (!q) return NULL;
    // Do not copy the secret into memory that is not cleared when freed
    CFDataRef d = CFDataCreateWithBytesNoCopy(NULL, data, (CFIndex)len, kCFAllocatorNull);
    CFStringRef l = CFStringCreateWithCString(NULL, label, kCFStringEncodingUTF8);
    if (!d || !l) {
        CFRelease(q);
        if (d) CFRelease(d);
        if (l) CFRelease(l);
        return PyErr_NoMemory();
    }
    CFDictionarySetValue(q, kSecValueData, d);
    CFDictionarySetValue(q, kSecAttrLabel, l);
    CFRelease(d);
    CFRelease(l);
    KeychainInteraction ki = disallow_keychain_interaction();
    OSStatus status = SecItemAdd(q, NULL);
    restore_keychain_interaction(ki);
    CFRelease(q);
    if (status != errSecSuccess) return set_keychain_error(status, "store an item in");
    Py_RETURN_NONE;
}

static PyObject *
cocoa_keychain_read_secret(PyObject *self UNUSED, PyObject *args) {
    const char *service, *account;
    if (!PyArg_ParseTuple(args, "ss", &service, &account)) return NULL;
    CFMutableDictionaryRef q = keychain_query(service, account);
    if (!q) return NULL;
    CFDictionarySetValue(q, kSecReturnData, kCFBooleanTrue);
    CFDictionarySetValue(q, kSecMatchLimit, kSecMatchLimitOne);
    set_keychain_query_ui_fail(q);
    CFTypeRef result = NULL;
    KeychainInteraction ki = disallow_keychain_interaction();
    OSStatus status = SecItemCopyMatching(q, &result);
    restore_keychain_interaction(ki);
    CFRelease(q);
    if (status == errSecItemNotFound) Py_RETURN_NONE;
    if (status != errSecSuccess) return set_keychain_error(status, "read an item from");
    if (!result || CFGetTypeID(result) != CFDataGetTypeID()) {
        if (result) CFRelease(result);
        PyErr_SetString(PyExc_OSError, "The item read from the keychain has no data");
        return NULL;
    }
    PyObject *ans = secret_from_data(CFDataGetBytePtr((CFDataRef)result), (size_t)CFDataGetLength((CFDataRef)result));
    CFRelease(result);
    return ans;
}

static PyObject *
cocoa_keychain_delete(PyObject *self UNUSED, PyObject *args) {
    const char *service, *account;
    if (!PyArg_ParseTuple(args, "ss", &service, &account)) return NULL;
    CFMutableDictionaryRef q = keychain_query(service, account);
    if (!q) return NULL;
    set_keychain_query_ui_fail(q);
    KeychainInteraction ki = disallow_keychain_interaction();
    OSStatus status = SecItemDelete(q);
    restore_keychain_interaction(ki);
    CFRelease(q);
    if (status == errSecItemNotFound) Py_RETURN_FALSE;
    if (status != errSecSuccess) return set_keychain_error(status, "delete an item from");
    Py_RETURN_TRUE;
}
static PyObject *
cocoa_keychain_list_accounts(PyObject *self UNUSED, PyObject *args) {
    const char *service;
    if (!PyArg_ParseTuple(args, "s", &service)) return NULL;
    CFMutableDictionaryRef q = keychain_query(service, NULL);
    if (!q) return NULL;
    // Only the attributes are read, which does not need access to the secret data of the items
    CFDictionarySetValue(q, kSecReturnAttributes, kCFBooleanTrue);
    CFDictionarySetValue(q, kSecMatchLimit, kSecMatchLimitAll);
    set_keychain_query_ui_fail(q);
    CFTypeRef result = NULL;
    KeychainInteraction ki = disallow_keychain_interaction();
    OSStatus status = SecItemCopyMatching(q, &result);
    restore_keychain_interaction(ki);
    CFRelease(q);
    if (status == errSecItemNotFound) return PyList_New(0);
    if (status != errSecSuccess) return set_keychain_error(status, "list the items in");
    if (!result || CFGetTypeID(result) != CFArrayGetTypeID()) {
        if (result) CFRelease(result);
        PyErr_SetString(PyExc_OSError, "Listing the items in the keychain did not return a list");
        return NULL;
    }
    PyObject *ans = PyList_New(0);
    CFArrayRef items = (CFArrayRef)result;
    for (CFIndex i = 0; ans && i < CFArrayGetCount(items); i++) {
        CFTypeRef item = CFArrayGetValueAtIndex(items, i);
        if (!item || CFGetTypeID(item) != CFDictionaryGetTypeID()) continue;
        CFTypeRef account = CFDictionaryGetValue((CFDictionaryRef)item, kSecAttrAccount);
        if (!account || CFGetTypeID(account) != CFStringGetTypeID()) continue;
        char buf[1024];
        if (!CFStringGetCString((CFStringRef)account, buf, sizeof(buf), kCFStringEncodingUTF8)) continue;
        RAII_PyObject(a, PyUnicode_FromString(buf));
        if (!a || PyList_Append(ans, a) != 0) Py_CLEAR(ans);
    }
    CFRelease(result);
    return ans;
}
// }}}

static PyMethodDef module_methods[] = {
    {"cocoa_keychain_list_accounts", (PyCFunction)cocoa_keychain_list_accounts, METH_VARARGS, ""},
    {"cocoa_keychain_store_secret", (PyCFunction)cocoa_keychain_store_secret, METH_VARARGS, ""},
    {"cocoa_keychain_read_secret", (PyCFunction)cocoa_keychain_read_secret, METH_VARARGS, ""},
    {"cocoa_keychain_delete", (PyCFunction)cocoa_keychain_delete, METH_VARARGS, ""},
    {"cocoa_play_system_sound_by_id_async", play_system_sound_by_id_async, METH_O, ""},
    {"cocoa_get_lang", (PyCFunction)cocoa_get_lang, METH_NOARGS, ""},
    {"cocoa_get_machine_id", (PyCFunction)cocoa_get_machine_id, METH_NOARGS, ""},
    {"cocoa_is_secure_input_enabled", (PyCFunction)cocoa_is_secure_input_enabled, METH_NOARGS, ""},
    {"cocoa_set_global_shortcut", (PyCFunction)cocoa_set_global_shortcut, METH_VARARGS, ""},
    {"cocoa_send_notification", (PyCFunction)(void (*)(void))cocoa_send_notification, METH_VARARGS | METH_KEYWORDS, ""},
    {"cocoa_remove_delivered_notification", (PyCFunction)cocoa_remove_delivered_notification, METH_O, ""},
    {"cocoa_live_delivered_notifications", (PyCFunction)cocoa_live_delivered_notifications, METH_NOARGS, ""},
    {"cocoa_set_notification_activated_callback", (PyCFunction)set_notification_activated_callback, METH_O, ""},
    {"cocoa_set_url_handler", (PyCFunction)cocoa_set_url_handler, METH_VARARGS, ""},
    {"cocoa_set_app_icon", (PyCFunction)cocoa_set_app_icon, METH_VARARGS, ""},
    {"cocoa_set_dock_icon", (PyCFunction)cocoa_set_dock_icon, METH_VARARGS, ""},
    {"cocoa_show_progress_bar_on_dock_icon", (PyCFunction)cocoa_show_progress_bar_on_dock_icon, METH_VARARGS, ""},
    {"cocoa_bundle_image_as_png", (PyCFunction)(void (*)(void))bundle_image_as_png, METH_VARARGS | METH_KEYWORDS, ""},
    {NULL, NULL, 0, NULL} /* Sentinel */
};

bool
init_cocoa(PyObject *module) {
    cocoa_clear_global_shortcuts();
    if (PyModule_AddFunctions(module, module_methods) != 0) return false;
    register_at_exit_cleanup_func(COCOA_CLEANUP_FUNC, cleanup);
    [[NSNotificationCenter defaultCenter] addObserverForName:NSApplicationDidBecomeActiveNotification
                                                      object:nil
                                                       queue:[NSOperationQueue mainQueue]
                                                  usingBlock:^(NSNotification *note UNUSED) { cocoa_set_dock_badge(NULL); }];
    return true;
}