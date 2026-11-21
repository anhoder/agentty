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
#include <libproc.h>
#include <objc/runtime.h>
#include <limits.h>

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

static const void *kitty_search_overlay_key = &kitty_search_overlay_key;

@interface KittySearchButton : NSButton {
    NSTrackingArea *hover_tracking_area;
    BOOL hovered;
}
@property(nonatomic, readonly) BOOL hovered;
@end

@interface KittySearchButtonCell : NSButtonCell
@end

@implementation KittySearchButtonCell
- (void)drawBezelWithFrame:(NSRect)frame inView:(NSView *)view {
    KittySearchButton *button = (KittySearchButton *)view;
    if (!self.enabled) return;
    BOOL selected = button.tag == 1 && self.state == NSControlStateValueOn;
    BOOL active = button.hovered || self.highlighted;
    if (!selected && !active) return;
    NSColor *color = selected ? NSColor.selectedControlColor : NSColor.labelColor;
    [[color colorWithAlphaComponent:selected ? (active ? 0.28 : 0.18) : (self.highlighted ? 0.14 : 0.08)] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSInsetRect(frame, 1, 2) xRadius:5 yRadius:5] fill];
}
@end

@implementation KittySearchButton
+ (Class)cellClass { return [KittySearchButtonCell class]; }
- (BOOL)hovered { return hovered; }
- (void)dealloc {
    if (hover_tracking_area) [self removeTrackingArea:hover_tracking_area];
    [hover_tracking_area release];
    [super dealloc];
}
- (void)updateTrackingAreas {
    if (hover_tracking_area) [self removeTrackingArea:hover_tracking_area];
    [hover_tracking_area release];
    hover_tracking_area = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingCursorUpdate | NSTrackingActiveInKeyWindow | NSTrackingInVisibleRect
        owner:self userInfo:nil];
    [self addTrackingArea:hover_tracking_area];
    [super updateTrackingAreas];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    if (self.enabled) [self addCursorRect:self.visibleRect cursor:[NSCursor pointingHandCursor]];
}
- (void)mouseEntered:(NSEvent *)event {
    (void)event;
    if (self.enabled) { hovered = YES; self.needsDisplay = YES; }
    [(self.enabled ? NSCursor.pointingHandCursor : NSCursor.arrowCursor) set];
}
- (void)mouseMoved:(NSEvent *)event { [self mouseEntered:event]; }
- (void)cursorUpdate:(NSEvent *)event { [self mouseEntered:event]; }
- (void)mouseExited:(NSEvent *)event {
    (void)event;
    hovered = NO; self.needsDisplay = YES;
    [[NSCursor arrowCursor] set];
}
- (void)setEnabled:(BOOL)enabled {
    [super setEnabled:enabled];
    if (!enabled && hovered) [[NSCursor arrowCursor] set];
    if (!enabled) hovered = NO;
    self.needsDisplay = YES;
    [self.window invalidateCursorRectsForView:self];
}
@end

@interface KittySearchInput : NSView
@end

@implementation KittySearchInput
- (BOOL)isFlipped { return YES; }
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    [[[NSColor labelColor] colorWithAlphaComponent:0.06] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:self.bounds xRadius:7 yRadius:7] fill];
}
@end

@interface KittySearchOverlay : NSView <NSSearchFieldDelegate> {
    id_type pane_id;
    NSSearchField *search_field;
    NSTextField *count_label;
    KittySearchInput *input_background;
    NSButton *case_button, *whole_button, *regex_button, *previous_button, *next_button, *close_button;
    id local_event_monitor;
}
@property(nonatomic) id_type paneID;
@property(nonatomic, retain) NSSearchField *searchField;
@property(nonatomic, retain) NSTextField *countLabel;
@property(nonatomic, retain) KittySearchInput *inputBackground;
@property(nonatomic, retain) NSButton *caseButton;
@property(nonatomic, retain) NSButton *wholeButton;
@property(nonatomic, retain) NSButton *regexButton;
@property(nonatomic, retain) NSButton *previousButton;
@property(nonatomic, retain) NSButton *nextButton;
@property(nonatomic, retain) NSButton *closeButton;
@property(nonatomic, assign) id localEventMonitor;
- (void)updateStatus:(NSString *)status focus:(BOOL)focus;
- (void)sendCommand:(NSString *)command;
- (void)toggleCase:(id)sender;
- (void)toggleWholeWord:(id)sender;
- (void)toggleRegex:(id)sender;
- (BOOL)handleSearchKey:(NSEvent *)event;
@end

static void
search_event(id_type pane_id, NSString *command, NSString *query) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:@[@(pane_id), command, query ? query : @""] options:0 error:nil];
    if (data) {
        NSString *payload = [[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] autorelease];
        set_cocoa_pending_action(SEARCH_EVENT, payload.UTF8String);
    }
}

@implementation KittySearchOverlay
@synthesize paneID = pane_id, searchField = search_field, countLabel = count_label, inputBackground = input_background;
@synthesize caseButton = case_button, wholeButton = whole_button, regexButton = regex_button;
@synthesize previousButton = previous_button, nextButton = next_button, closeButton = close_button;
@synthesize localEventMonitor = local_event_monitor;
- (BOOL)isFlipped { return YES; }
// Stop unhandled child events here instead of forwarding them to the terminal.
- (void)mouseDown:(NSEvent *)event { (void)event; }
- (void)mouseUp:(NSEvent *)event { (void)event; }
- (void)mouseDragged:(NSEvent *)event { (void)event; }
- (void)rightMouseDown:(NSEvent *)event { (void)event; }
- (void)rightMouseUp:(NSEvent *)event { (void)event; }
- (void)rightMouseDragged:(NSEvent *)event { (void)event; }
- (void)otherMouseDown:(NSEvent *)event { (void)event; }
- (void)otherMouseUp:(NSEvent *)event { (void)event; }
- (void)otherMouseDragged:(NSEvent *)event { (void)event; }
- (void)mouseMoved:(NSEvent *)event { (void)event; }
- (void)scrollWheel:(NSEvent *)event { (void)event; }
- (void)dealloc {
    if (local_event_monitor) [NSEvent removeMonitor:local_event_monitor];
    [search_field release]; [count_label release]; [input_background release];
    [case_button release]; [whole_button release]; [regex_button release];
    [previous_button release]; [next_button release]; [close_button release];
    [super dealloc];
}
- (void)layout {
    [super layout];
    CGFloat w = NSWidth(self.bounds), h = NSHeight(self.bounds);
    CGFloat inset = MIN(8, w / 8), available = MAX(0, w - 2 * inset);
    CGFloat button = MIN(24, available / 5), gap = MIN(4, available / 20);
    CGFloat row_height = MIN(32, MAX(0, h - 2 * inset));
    CGFloat x = w - inset, y = inset;
    if (w < 400) y = MAX(inset, h - inset - row_height);
    close_button.frame = NSMakeRect(x - button, y, button, row_height); x -= button + gap;
    next_button.frame = NSMakeRect(x - button, y, button, row_height); x -= button + gap;
    previous_button.frame = NSMakeRect(x - button, y, button, row_height); x -= button + gap;
    CGFloat count_width = MIN(MAX(24, count_label.fittingSize.width), MIN(64, MAX(0, x - inset)));
    count_label.frame = NSMakeRect(x - count_width, y + MAX(0, (row_height - 16) / 2), count_width, MIN(16, row_height));
    x -= count_width + 4;
    CGFloat input_width = w < 400 ? available : MAX(0, x - inset);
    CGFloat input_height = MIN(w < 220 ? 56 : 32, MAX(0, h - 2 * inset));
    input_background.frame = NSMakeRect(inset, inset, input_width, input_height);
    CGFloat inner_inset = MIN(8, input_width / 8), option_gap = MIN(2, input_width / 30);
    CGFloat option = MIN(26, MAX(0, input_width - 2 * inner_inset - 2 * option_gap) / 3);
    CGFloat option_y = w < 220 ? MAX(0, input_height - 28) : MAX(0, (input_height - 28) / 2);
    CGFloat option_height = MIN(28, input_height), option_x = input_width - inner_inset;
    regex_button.frame = NSMakeRect(option_x - option, option_y, option, option_height); option_x -= option + option_gap;
    whole_button.frame = NSMakeRect(option_x - option, option_y, option, option_height); option_x -= option + option_gap;
    case_button.frame = NSMakeRect(option_x - option, option_y, option, option_height); option_x -= option + option_gap;
    // A borderless search cell top-aligns its text and field editor. Give both only
    // the font's line height, then center that line inside the input background.
    CGFloat text_width = w < 220 ? input_width - 2 * inner_inset : option_x - inner_inset - 4;
    NSFont *font = search_field.font;
    CGFloat text_height = MIN(ceil(font.ascender - font.descender + font.leading), input_height);
    CGFloat text_area_height = w < 220 ? MIN(28, input_height) : input_height;
    search_field.frame = NSMakeRect(inner_inset, MAX(0, (text_area_height - text_height) / 2), MAX(0, text_width), text_height);
}
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:self.bounds xRadius:10 yRadius:10] fill];
}
- (void)sendCommand:(NSString *)command {
    search_event(pane_id, command, search_field.stringValue);
}
- (BOOL)hasMarkedText {
    NSText *editor = search_field.currentEditor;
    return [editor isKindOfClass:[NSTextView class]] && [(NSTextView *)editor hasMarkedText];
}
- (BOOL)control:(NSControl *)control textView:(NSTextView *)textView doCommandBySelector:(SEL)selector {
    (void)control; (void)textView;
    if ([self hasMarkedText]) return NO;
    if (selector == @selector(cancelOperation:)) { [self sendCommand:@"close"]; return YES; }
    if (selector == @selector(insertNewline:) || selector == @selector(insertNewlineIgnoringFieldEditor:)) {
        [self sendCommand:([NSApp.currentEvent modifierFlags] & NSEventModifierFlagShift) ? @"submit_previous" : @"submit"];
        return YES;
    }
    return NO;
}
- (void)controlTextDidChange:(NSNotification *)notification {
    if (notification.object != search_field || [self hasMarkedText]) return;
    NSPasteboard *pasteboard = [NSPasteboard pasteboardWithName:NSPasteboardNameFind];
    [pasteboard clearContents];
    [pasteboard setString:search_field.stringValue forType:NSPasteboardTypeString];
    search_event(pane_id, @"query", search_field.stringValue);
    previous_button.enabled = next_button.enabled = search_field.stringValue.length > 0;
}
- (void)previous:(id)sender { (void)sender; previous_button.state = NSControlStateValueOff; [self sendCommand:@"previous"]; }
- (void)next:(id)sender { (void)sender; next_button.state = NSControlStateValueOff; [self sendCommand:@"next"]; }
- (void)close:(id)sender { (void)sender; [self sendCommand:@"close"]; }
- (void)toggleCase:(id)sender {
    if (sender != case_button) case_button.state = case_button.state == NSControlStateValueOn ? NSControlStateValueOff : NSControlStateValueOn;
    search_event(pane_id, @"case_sensitive", case_button.state == NSControlStateValueOn ? @"1" : @"0");
}
- (void)toggleWholeWord:(id)sender {
    if (sender != whole_button) whole_button.state = whole_button.state == NSControlStateValueOn ? NSControlStateValueOff : NSControlStateValueOn;
    search_event(pane_id, @"whole_word", whole_button.state == NSControlStateValueOn ? @"1" : @"0");
}
- (void)toggleRegex:(id)sender {
    if (sender != regex_button) regex_button.state = regex_button.state == NSControlStateValueOn ? NSControlStateValueOff : NSControlStateValueOn;
    search_event(pane_id, @"regex", regex_button.state == NSControlStateValueOn ? @"1" : @"0");
}
- (void)updateStatus:(NSString *)status focus:(BOOL)focus {
    // The field owns the draft, including while unfocused. Status updates must
    // never replace its text, marked range, selection, or undo history.
    NSString *message = status ? status : @"";
    BOOL invalid = [message hasPrefix:@"Invalid regex:"];
    BOOL timed_out = [message isEqualToString:@"Search timed out"];
    count_label.stringValue = invalid || timed_out ? @"Error" : message;
    count_label.textColor = invalid || timed_out ? NSColor.systemRedColor : NSColor.secondaryLabelColor;
    count_label.toolTip = message;
    count_label.accessibilityValue = message;
    search_field.toolTip = invalid || timed_out ? message : @"Search terminal scrollback";
    // Navigation also submits an unsearched draft or refreshes stale results.
    previous_button.enabled = next_button.enabled = search_field.stringValue.length > 0;
    if (focus && ![self hasMarkedText]) {
        [self.window makeFirstResponder:search_field];
        [search_field selectText:nil];
    }
    [self setNeedsLayout:YES];
}
- (BOOL)handleSearchKey:(NSEvent *)event {
    if ([self hasMarkedText]) return NO;
    NSUInteger flags = event.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
    NSString *chars = event.charactersIgnoringModifiers.lowercaseString;
    if ((flags & (NSEventModifierFlagCommand | NSEventModifierFlagOption)) == (NSEventModifierFlagCommand | NSEventModifierFlagOption)) {
        if ([chars isEqualToString:@"c"]) { [self toggleCase:nil]; return YES; }
        if ([chars isEqualToString:@"w"]) { [self toggleWholeWord:nil]; return YES; }
        if ([chars isEqualToString:@"r"]) { [self toggleRegex:nil]; return YES; }
    }
    if ((flags & NSEventModifierFlagCommand) && [chars isEqualToString:@"g"]) {
        [self sendCommand:(flags & NSEventModifierFlagShift) ? @"previous" : @"next"]; return YES;
    }
    if (event.keyCode == 53) { [self sendCommand:@"close"]; return YES; }
    if ((flags & NSEventModifierFlagCommand) && [chars isEqualToString:@"f"]) {
        [self.window makeFirstResponder:search_field]; [search_field selectText:nil]; return YES;
    }
    return NO;
}
- (void)keyDown:(NSEvent *)event {
    if (![self handleSearchKey:event]) [super keyDown:event];
}
@end

static NSMutableDictionary<NSNumber *, KittySearchOverlay *> *
search_overlays_for_window(NSWindow *window) {
    return objc_getAssociatedObject(window, kitty_search_overlay_key);
}

static void
remove_search_overlay(NSWindow *window, id_type pane_id) {
    NSMutableDictionary *overlays = search_overlays_for_window(window);
    KittySearchOverlay *overlay = overlays[@(pane_id)];
    if (overlay) {
        NSResponder *first = window.firstResponder;
        BOOL fieldFocused = first == overlay.searchField.currentEditor ||
            ([first isKindOfClass:[NSView class]] && [(NSView *)first isDescendantOf:overlay]);
        if (overlay.localEventMonitor) {
            [NSEvent removeMonitor:overlay.localEventMonitor];
            overlay.localEventMonitor = nil;
        }
        if (fieldFocused) [window makeFirstResponder:window.contentView];
        [overlay removeFromSuperview];
        [overlays removeObjectForKey:@(pane_id)];
    }
}

static void
position_search_overlay(OSWindow *osw) {
    NSWindow *window = glfwGetCocoaWindow(osw->handle);
    NSDictionary<NSNumber *, KittySearchOverlay *> *overlays = search_overlays_for_window(window);
    for (KittySearchOverlay *overlay in overlays.objectEnumerator) {
        Window *pane = NULL;
        if (osw->active_tab < osw->num_tabs) {
            Tab *tab = osw->tabs + osw->active_tab;
            for (unsigned i = 0; i < tab->num_windows; i++) {
                Window *candidate = tab->windows + i;
                if (candidate->id == overlay.paneID && candidate->visible && candidate->render_data.screen) {
                    pane = candidate;
                    break;
                }
            }
        }
        CGFloat scale = window.backingScaleFactor > 0 ? window.backingScaleFactor : 1;
        NSRect bounds = window.contentView.bounds;
        CGFloat left = NSMinX(bounds), right = NSMaxX(bounds), top = 0, bottom = NSHeight(bounds);
        if (pane) {
            const WindowGeometry *g = &pane->render_data.geometry;
            left = MAX(left, NSMinX(bounds) + g->left / scale);
            right = MIN(right, NSMinX(bounds) + g->right / scale);
            top = MAX(0, g->top / scale);
            bottom = MIN(bottom, g->bottom / scale);
        }
        // Leave room for the soft shadow without changing the terminal's geometry.
        CGFloat width = MIN(440, MAX(0, right - left - 32));
        CGFloat desired_height = width < 220 ? 104 : width < 400 ? 80 : 48;
        CGFloat height = MIN(desired_height, MAX(0, bottom - top - 32));
        if (!pane || width <= 0 || height <= 0) {
            if (window.firstResponder == overlay.searchField.currentEditor ||
                ([window.firstResponder isKindOfClass:[NSView class]] && [(NSView *)window.firstResponder isDescendantOf:overlay]))
                [window makeFirstResponder:window.contentView];
            overlay.hidden = YES;
            continue;
        }
        overlay.hidden = NO;
        CGFloat y = window.contentView.isFlipped ? NSMinY(bounds) + top + 16 : NSMaxY(bounds) - top - height - 16;
        overlay.frame = NSMakeRect(right - width - 16, y, width, height);
        [overlay layoutSubtreeIfNeeded];
    }
}

void
set_scrollback_search(id_type os_window_id, id_type window_id, const char *query, const char *status, bool visible, bool focus) {
    OSWindow *osw = os_window_for_id(os_window_id);
    if (!osw || !osw->handle) return;
    NSWindow *window = glfwGetCocoaWindow(osw->handle);
    NSMutableDictionary<NSNumber *, KittySearchOverlay *> *overlays = search_overlays_for_window(window);
    KittySearchOverlay *overlay = overlays[@(window_id)];
    if (!visible) {
        remove_search_overlay(window, window_id);
        return;
    }
    if (!overlay) {
        if (!overlays) {
            overlays = [NSMutableDictionary dictionary];
            objc_setAssociatedObject(window, kitty_search_overlay_key, overlays, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        }
        if (!query || !*query) {
            NSString *saved = [[NSPasteboard pasteboardWithName:NSPasteboardNameFind] stringForType:NSPasteboardTypeString];
            if (saved.length) {
                query = saved.UTF8String;
                search_event(window_id, @"query", saved);
            }
        }
        overlay = [[[KittySearchOverlay alloc] initWithFrame:NSMakeRect(0, 0, 440, 48)] autorelease];
        overlay.paneID = window_id;
        overlay.inputBackground = [[[KittySearchInput alloc] initWithFrame:NSZeroRect] autorelease];
        [overlay addSubview:overlay.inputBackground];
        overlay.searchField = [[[NSSearchField alloc] initWithFrame:NSZeroRect] autorelease];
        overlay.searchField.delegate = overlay;
        overlay.searchField.placeholderString = @"Search...";
        overlay.searchField.font = [NSFont systemFontOfSize:13];
        overlay.searchField.accessibilityLabel = @"Search terminal scrollback";
        overlay.searchField.bezeled = NO;
        overlay.searchField.bordered = NO;
        overlay.searchField.drawsBackground = NO;
        overlay.searchField.focusRingType = NSFocusRingTypeNone;
        NSSearchFieldCell *search_cell = (NSSearchFieldCell *)overlay.searchField.cell;
        search_cell.searchButtonCell = nil;
        search_cell.cancelButtonCell = nil;
        overlay.searchField.stringValue = @(query ? query : "");
        overlay.countLabel = [[[NSTextField alloc] initWithFrame:NSZeroRect] autorelease];
        overlay.countLabel.editable = NO; overlay.countLabel.selectable = NO;
        overlay.countLabel.bezeled = NO; overlay.countLabel.drawsBackground = NO;
        overlay.countLabel.font = [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightRegular];
        overlay.countLabel.alignment = NSTextAlignmentCenter;
        overlay.countLabel.lineBreakMode = NSLineBreakByTruncatingTail;
        overlay.countLabel.accessibilityLabel = @"Search results";
        overlay.caseButton = [KittySearchButton buttonWithTitle:@"Aa" target:overlay action:@selector(toggleCase:)];
        overlay.wholeButton = [KittySearchButton buttonWithTitle:@"ab" target:overlay action:@selector(toggleWholeWord:)];
        overlay.regexButton = [KittySearchButton buttonWithTitle:@".*" target:overlay action:@selector(toggleRegex:)];
        overlay.previousButton = [KittySearchButton buttonWithTitle:@"" target:overlay action:@selector(previous:)];
        overlay.nextButton = [KittySearchButton buttonWithTitle:@"" target:overlay action:@selector(next:)];
        overlay.closeButton = [KittySearchButton buttonWithTitle:@"" target:overlay action:@selector(close:)];
        NSArray<NSButton *> *buttons = @[overlay.caseButton, overlay.wholeButton, overlay.regexButton, overlay.previousButton, overlay.nextButton, overlay.closeButton];
        NSArray<NSString *> *labels = @[@"Match case", @"Match whole word", @"Use regular expression", @"Previous match", @"Next match", @"Close search"];
        NSArray<NSString *> *tips = @[@"Match case (Option-Command-C)", @"Match whole word (Option-Command-W)", @"Use regular expression (Option-Command-R)", @"Previous match (Shift-Command-G)", @"Next match (Command-G)", @"Close search (Escape)"];
        NSArray<NSString *> *symbols = @[@"chevron.up", @"chevron.down", @"xmark"];
        for (NSUInteger i = 0; i < buttons.count; i++) {
            NSButton *button = buttons[i];
            [button setButtonType:i < 3 ? NSButtonTypeToggle : NSButtonTypeMomentaryPushIn];
            button.tag = i < 3 ? 1 : 0;
            button.bordered = YES;
            button.focusRingType = NSFocusRingTypeExterior;
            button.font = [NSFont systemFontOfSize:12 weight:NSFontWeightMedium];
            button.toolTip = tips[i];
            button.accessibilityLabel = labels[i];
            if (i >= 3) {
            if (@available(macOS 11.0, *)) {
                button.image = [NSImage imageWithSystemSymbolName:symbols[i - 3] accessibilityDescription:labels[i]];
                button.imagePosition = NSImageOnly;
            } else {
                // SF Symbols require macOS 11; keep the same stroked controls on older systems.
                NSImage *image = [[[NSImage alloc] initWithSize:NSMakeSize(14, 14)] autorelease];
                [image lockFocus];
                NSBezierPath *path = [NSBezierPath bezierPath];
                path.lineWidth = 1.5;
                path.lineCapStyle = NSLineCapStyleRound;
                if (i == 5) {
                    [path moveToPoint:NSMakePoint(3, 3)]; [path lineToPoint:NSMakePoint(11, 11)];
                    [path moveToPoint:NSMakePoint(3, 11)]; [path lineToPoint:NSMakePoint(11, 3)];
                } else {
                    CGFloat end = i == 3 ? 5 : 9, middle = i == 3 ? 9 : 5;
                    [path moveToPoint:NSMakePoint(2, end)]; [path lineToPoint:NSMakePoint(7, middle)]; [path lineToPoint:NSMakePoint(12, end)];
                }
                [[NSColor blackColor] setStroke]; [path stroke];
                [image unlockFocus];
                image.template = YES;
                button.image = image;
                button.imagePosition = NSImageOnly;
            }
            }
            // Only our bezel supplies hover/selection; native content and focus stay intact.
            [(NSButtonCell *)button.cell setHighlightsBy:NSNoCellMask];
            [(NSButtonCell *)button.cell setShowsStateBy:NSNoCellMask];
            if (i < 3) [overlay.inputBackground addSubview:button];
            else [overlay addSubview:button];
        }
        NSMutableParagraphStyle *whole_word_style = [[[NSMutableParagraphStyle alloc] init] autorelease];
        whole_word_style.alignment = NSTextAlignmentCenter;
        overlay.wholeButton.attributedTitle = [[[NSAttributedString alloc] initWithString:@"ab" attributes:@{
            NSFontAttributeName: overlay.wholeButton.font, NSForegroundColorAttributeName: NSColor.controlTextColor,
            NSUnderlineStyleAttributeName: @(NSUnderlineStyleSingle), NSParagraphStyleAttributeName: whole_word_style
        }] autorelease];
        overlay.wholeButton.attributedAlternateTitle = overlay.wholeButton.attributedTitle;
        [overlay.inputBackground addSubview:overlay.searchField]; [overlay addSubview:overlay.countLabel];
        overlay.searchField.nextKeyView = overlay.caseButton;
        overlay.caseButton.nextKeyView = overlay.wholeButton;
        overlay.wholeButton.nextKeyView = overlay.regexButton;
        overlay.regexButton.nextKeyView = overlay.previousButton;
        overlay.previousButton.nextKeyView = overlay.nextButton;
        overlay.nextButton.nextKeyView = overlay.closeButton;
        overlay.closeButton.nextKeyView = overlay.searchField;
        overlay.wantsLayer = YES;
        overlay.layer.masksToBounds = NO;
        NSShadow *shadow = [[[NSShadow alloc] init] autorelease];
        shadow.shadowColor = [[NSColor blackColor] colorWithAlphaComponent:0.20];
        shadow.shadowOffset = NSMakeSize(0, -2);
        shadow.shadowBlurRadius = 10;
        overlay.shadow = shadow;
        [window.contentView addSubview:overlay positioned:NSWindowAbove relativeTo:nil];
        overlays[@(window_id)] = overlay;
        // __block is non-retaining under MRC; remove the monitor before releasing the overlay.
        __block KittySearchOverlay *monitored_overlay = overlay;
        __block NSWindow *monitored_window = window;
        overlay.localEventMonitor = [NSEvent addLocalMonitorForEventsMatchingMask:NSEventMaskKeyDown handler:^NSEvent *(NSEvent *event) {
            // A command may synchronously remove the overlay and its monitor.
            KittySearchOverlay *current_overlay = [[monitored_overlay retain] autorelease];
            NSWindow *current_window = [[monitored_window retain] autorelease];
            if (event.window != current_window || current_overlay.hidden) return event;
            NSResponder *first = current_window.firstResponder;
            BOOL editing = first == current_overlay.searchField.currentEditor;
            BOOL in_overlay = [first isKindOfClass:[NSView class]] && [(NSView *)first isDescendantOf:current_overlay];
            if (!editing && !in_overlay) return event;
            if (editing && [(NSTextView *)first hasMarkedText]) return event;
            if ([current_overlay handleSearchKey:event]) return nil;
            if (!editing && (event.keyCode == 36 || event.keyCode == 76)) {
                [current_overlay sendCommand:(event.modifierFlags & NSEventModifierFlagShift) ? @"submit_previous" : @"submit"];
                return nil;
            }
            NSUInteger flags = event.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
            NSString *chars = event.charactersIgnoringModifiers.lowercaseString;
            if (editing && (flags & NSEventModifierFlagCommand) && !(flags & (NSEventModifierFlagOption | NSEventModifierFlagControl))) {
                NSTextView *editor = (NSTextView *)first;
                if ([chars isEqualToString:@"a"]) { [editor selectAll:nil]; return nil; }
                if ([chars isEqualToString:@"c"]) { [editor copy:nil]; return nil; }
                if ([chars isEqualToString:@"v"]) { [editor paste:nil]; return nil; }
                if ([chars isEqualToString:@"x"]) { [editor cut:nil]; return nil; }
            }
            return event;
        }];
    }
    [overlay updateStatus:@(status ? status : "") focus:focus];
    position_search_overlay(osw);
}


static PaneAnimationRect
pane_animation_rect(const Window *w) {
    const WindowGeometry *g = &w->render_data.geometry;
    const WindowRenderData *title = &w->window_title_render_data;
    double top = g->top, bottom = g->bottom;
    if (title->screen && title->geometry.bottom > title->geometry.top) {
        top = MIN(top, title->geometry.top);
        bottom = MAX(bottom, title->geometry.bottom);
    }
    double left = (double)g->left - g->spaces.left;
    top -= g->spaces.top;
    return (PaneAnimationRect){left, top, g->right + g->spaces.right - left, bottom + g->spaces.bottom - top};
}

bool
cocoa_prepare_pane_animation(void *ptr, monotonic_t now) {
    OSWindow *osw = ptr;
    PaneAnimation *a = &osw->pane_animation;
    Tab *tab = osw->tabs + osw->active_tab;
    double at = monotonic_t_to_s_double(now);
    bool dragging = global_state.active_drag_resize && os_window_for_kitty_window(global_state.active_drag_resize) == osw;
    bool enabled = OPT(window_jelly_enabled) && OPT(window_jelly_duration) > 0 && !dragging && !osw->suppress_pane_animation &&
                   !osw->live_resize.in_progress && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    osw->suppress_pane_animation = false;
    bool compatible = a->tab_id == tab->id && a->width == osw->viewport_width && a->height == osw->viewport_height;
    bool was_active = a->active;
    if (!enabled || !compatible || at - a->started_at >= a->duration) a->active = false;
    if (!a->active) a->content_valid = false;
    size_t visible = 0;
    bool changed = false;
    for (unsigned i = 0; i < tab->num_windows; i++) {
        Window *w = tab->windows + i;
        if (!w->visible || !w->render_data.screen) continue;
        PaneAnimationRect rect = pane_animation_rect(w);
        if (visible >= a->count || a->frames[visible].id != w->id || !pane_animation_rect_equal(a->frames[visible].target, rect)) changed = true;
        visible++;
    }
    changed |= visible != a->count;
    if (changed || !compatible) {
        a->content_valid = false;
        // Reuse two geometry arrays so retargeting never overwrites an old frame
        // before another pane has sampled its current presentation position.
        if (visible > a->capacity) {
            a->capacity = MAX(visible, a->capacity * 2);
            a->next_frames = realloc(a->next_frames, a->capacity * sizeof(*a->next_frames));
            a->frames = realloc(a->frames, a->capacity * sizeof(*a->frames));
            if (!a->frames || !a->next_frames) fatal("Out of memory storing pane animation geometry");
        }
        size_t n = 0;
        bool animate = enabled && compatible && a->count && visible;
        double scale = MAX(1., [(NSWindow *)glfwGetCocoaWindow(osw->handle) backingScaleFactor]);
        for (unsigned i = 0; i < tab->num_windows; i++) {
            Window *w = tab->windows + i;
            if (!w->visible || !w->render_data.screen) continue;
            PaneAnimationRect rect = pane_animation_rect(w), start = rect;
            bool existed = false;
            if (animate) {
                for (size_t j = 0; j < a->count; j++) {
                    if (a->frames[j].id == w->id) {
                        start = a->active ? pane_animation_sample(a->frames + j, at - a->started_at, a->duration) : a->frames[j].target;
                        existed = true;
                        break;
                    }
                }
                if (!existed) {
                    start.x += rect.width * 0.04;
                    start.y += rect.height * 0.04;
                    start.width *= 0.92;
                    start.height *= 0.92;
                }
            }
            double bounce = OPT(window_jelly_bounce), maximum = OPT(window_jelly_bounce_max) * scale;
            double dx = fabs(rect.x + rect.width / 2. - start.x - start.width / 2.);
            double dy = fabs(rect.y + rect.height / 2. - start.y - start.height / 2.);
            a->next_frames[n++] = (PaneAnimationFrame){
                .id = w->id, .from = start, .target = rect,
                .position_spring = pane_animation_spring(MAX(pane_animation_damping(dx, bounce, maximum), pane_animation_damping(dy, bounce, maximum))),
                .size_spring = pane_animation_spring(MAX(pane_animation_damping(fabs(rect.width - start.width), bounce, maximum),
                                                       pane_animation_damping(fabs(rect.height - start.height), bounce, maximum))),
            };
        }
        PaneAnimationFrame *previous = a->frames;
        a->frames = a->next_frames;
        a->next_frames = previous;
        a->count = visible;
        a->active = animate;
        a->started_at = at;
        a->duration = OPT(window_jelly_duration);
        a->tab_id = tab->id;
        a->width = osw->viewport_width;
        a->height = osw->viewport_height;
    }
    // Shrink only after all old presentation frames have been sampled.
    if (a->capacity > MAX((size_t)64, visible * 4)) {
        a->capacity = MAX((size_t)1, visible * 2);
        a->next_frames = realloc(a->next_frames, a->capacity * sizeof(*a->next_frames));
        a->frames = realloc(a->frames, a->capacity * sizeof(*a->frames));
        if (!a->frames || !a->next_frames) fatal("Out of memory storing pane animation geometry");
    }
    return was_active || a->active;
}

void
cocoa_update_search_overlay(void *ptr) {
    position_search_overlay(ptr);
}

static const void *kitty_pane_switcher_key = &kitty_pane_switcher_key;
extern bool cocoa_dispatch_pane_switcher_shortcut(void *, const GLFWkeyevent *);

// Implemented by GLFW's Cocoa content view so keymap remapping and alternate
// keys exactly match normal shortcut dispatch, without delivering terminal text.
@interface NSView (KittyPaneSwitcherConfiguredKey)
- (BOOL)kittyPaneSwitcherHandleConfiguredKeyEvent:(NSEvent *)event;
@end

@interface KittyPaneSwitcherEntry : NSObject {
@public
    id_type pane_id;
    NSString *title;
    NSImage *image;
    BOOL pending;
}
@end

@implementation KittyPaneSwitcherEntry
- (void)dealloc { [title release]; [image release]; [super dealloc]; }
@end

@class KittyPaneSwitcher;
@interface KittyPaneSwitcherGrid : NSView {
@public
    KittyPaneSwitcher *owner; // Non-owning: the switcher owns its scroll view.
}
@end

@interface KittyPaneSwitcher : NSView {
@public
    id_type os_window_id, tab_id;
    NSMutableArray<KittyPaneSwitcherEntry *> *entries;
    NSUInteger selected, current_index, columns;
    CGFloat tile_width, tile_height;
    NSRect panel_rect;
    NSScrollView *scroll_view;
    KittyPaneSwitcherGrid *grid;
    NSWindow *hosting_window; // Non-owning; close notification tears us down.
    id event_monitor;
    NSEventModifierFlags initiating_modifiers, release_modifiers;
    unsigned short initiating_key;
    int initiating_delta;
    BOOL finished;
}
- (BOOL)isValid;
- (void)finish:(BOOL)confirm;
- (void)cycle:(NSInteger)delta;
- (void)revealSelection;
- (NSRect)tileRect:(NSUInteger)index;
- (NSEvent *)handleEvent:(NSEvent *)event;
- (void)windowInvalidated:(NSNotification *)notification;
@end

@implementation KittyPaneSwitcherGrid
- (BOOL)isFlipped { return YES; }
- (void)drawRect:(NSRect)dirtyRect {
    NSMutableParagraphStyle *style = [[[NSMutableParagraphStyle alloc] init] autorelease];
    style.lineBreakMode = NSLineBreakByTruncatingTail;
    NSDictionary *title_attributes = @{NSFontAttributeName: [NSFont systemFontOfSize:13 weight:NSFontWeightMedium],
        NSForegroundColorAttributeName: NSColor.labelColor, NSParagraphStyleAttributeName: style};
    style = [[style mutableCopy] autorelease];
    style.alignment = NSTextAlignmentCenter;
    NSDictionary *loading_attributes = @{NSFontAttributeName: [NSFont systemFontOfSize:12],
        NSForegroundColorAttributeName: NSColor.secondaryLabelColor, NSParagraphStyleAttributeName: style};
    for (NSUInteger i = 0; i < owner->entries.count; i++) {
        NSRect tile = [owner tileRect:i];
        if (!NSIntersectsRect(tile, dirtyRect)) continue;
        KittyPaneSwitcherEntry *entry = owner->entries[i];
        BOOL selected = i == owner->selected;
        BOOL current = i == owner->current_index;
        NSBezierPath *outline = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(tile, 2, 2) xRadius:8 yRadius:8];
        NSColor *tile_fill = current ? [[NSColor controlAccentColor] colorWithAlphaComponent:0.26] :
            (selected ? [[NSColor selectedControlColor] colorWithAlphaComponent:0.14] : NSColor.controlBackgroundColor);
        [tile_fill setFill];
        [outline fill];
        if (selected) { [NSColor.selectedControlColor setStroke]; outline.lineWidth = 2; [outline stroke]; }
        NSRect preview = NSMakeRect(tile.origin.x + 10, tile.origin.y + 10, MAX(0, tile.size.width - 20), MAX(0, tile.size.height - 44));
        NSBezierPath *preview_path = [NSBezierPath bezierPathWithRoundedRect:preview xRadius:5 yRadius:5];
        NSShadow *preview_shadow = [[[NSShadow alloc] init] autorelease];
        preview_shadow.shadowColor = [[NSColor blackColor] colorWithAlphaComponent:0.36];
        preview_shadow.shadowBlurRadius = 7;
        preview_shadow.shadowOffset = NSMakeSize(0, 2);
        [NSGraphicsContext saveGraphicsState];
        [preview_shadow set];
        [NSColor.textBackgroundColor setFill];
        [preview_path fill];
        [NSGraphicsContext restoreGraphicsState];
        if (entry->image && preview.size.width > 0 && preview.size.height > 0) {
            NSSize size = entry->image.size;
            CGFloat scale = MAX(preview.size.width / size.width, preview.size.height / size.height);
            NSRect target = NSMakeRect(NSMidX(preview) - size.width * scale / 2, NSMidY(preview) - size.height * scale / 2,
                size.width * scale, size.height * scale);
            [NSGraphicsContext saveGraphicsState];
            [preview_path addClip];
            [entry->image drawInRect:target fromRect:NSZeroRect operation:NSCompositingOperationSourceOver fraction:1
                respectFlipped:YES hints:@{NSImageHintInterpolation: @(NSImageInterpolationHigh)}];
            [NSGraphicsContext restoreGraphicsState];
        } else {
            [(entry->pending ? @"Loading preview…" : @"Preview unavailable") drawInRect:
                NSMakeRect(preview.origin.x + 4, NSMidY(preview) - 8, MAX(0, preview.size.width - 8), 18) withAttributes:loading_attributes];
        }
        CGFloat title_right = NSMaxX(tile) - 12;
        if (current) {
            NSString *badge_text = @"CURRENT";
            NSDictionary *badge_attributes = @{NSFontAttributeName: [NSFont systemFontOfSize:9 weight:NSFontWeightBold],
                NSForegroundColorAttributeName: [NSColor alternateSelectedControlTextColor]};
            NSSize badge_text_size = [badge_text sizeWithAttributes:badge_attributes];
            CGFloat badge_height = 16;
            CGFloat badge_width = badge_text_size.width + 10;
            NSRect badge = NSMakeRect(title_right - badge_width, NSMaxY(tile) - 29, badge_width, badge_height);
            NSBezierPath *badge_path = [NSBezierPath bezierPathWithRoundedRect:badge xRadius:badge_height / 2 yRadius:badge_height / 2];
            [[NSColor controlAccentColor] setFill];
            [badge_path fill];
            [badge_text drawInRect:NSMakeRect(NSMinX(badge) + 5, NSMidY(badge) - badge_text_size.height / 2,
                badge_text_size.width, badge_text_size.height) withAttributes:badge_attributes];
            title_right = NSMinX(badge) - 8;
        }
        [entry->title drawInRect:NSMakeRect(tile.origin.x + 12, NSMaxY(tile) - 28, MAX(0, title_right - tile.origin.x - 12), 18)
            withAttributes:title_attributes];
    }
}
@end

@implementation KittyPaneSwitcher
- (BOOL)isFlipped { return YES; }
- (BOOL)acceptsFirstResponder { return NO; }
// The overlay protects the terminal beneath it without changing first responder.
- (void)mouseDown:(NSEvent *)event { (void)event; }
- (void)mouseUp:(NSEvent *)event { (void)event; }
- (void)mouseDragged:(NSEvent *)event { (void)event; }
- (void)rightMouseDown:(NSEvent *)event { (void)event; }
- (void)rightMouseUp:(NSEvent *)event { (void)event; }
- (void)otherMouseDown:(NSEvent *)event { (void)event; }
- (void)otherMouseUp:(NSEvent *)event { (void)event; }
- (void)scrollWheel:(NSEvent *)event { (void)event; }
- (void)dealloc {
    if (event_monitor) [NSEvent removeMonitor:event_monitor];
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    [entries release]; [scroll_view release]; [grid release];
    [super dealloc];
}
- (BOOL)isValid {
    OSWindow *osw = os_window_for_id(os_window_id);
    if (finished || !osw || !osw->handle || !hosting_window.isKeyWindow || osw->active_tab >= osw->num_tabs) return NO;
    Tab *tab = osw->tabs + osw->active_tab;
    if (tab->id != tab_id || strcmp(tab->layout_name, "stack")) return NO;
    for (KittyPaneSwitcherEntry *entry in entries) {
        BOOL found = NO;
        for (unsigned i = 0; i < tab->num_windows; i++) if (tab->windows[i].id == entry->pane_id) { found = YES; break; }
        if (!found) return NO;
    }
    return YES;
}
- (void)finish:(BOOL)confirm {
    if (finished) return;
    id_type result = confirm && [self isValid] ? entries[selected]->pane_id : 0;
    finished = YES;
    // Removing the association can drop the last owner, including during a callback.
    [self retain];
    if (event_monitor) { [NSEvent removeMonitor:event_monitor]; event_monitor = nil; }
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    objc_setAssociatedObject(hosting_window, @selector(kittyPaneSwitcherHandleConfiguredKeyEvent:), nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    objc_setAssociatedObject(hosting_window, NSSelectorFromString(@"kittyPaneSwitcherModifiersChanged:"), nil, OBJC_ASSOCIATION_COPY_NONATOMIC);
    [self removeFromSuperview];
    if (objc_getAssociatedObject(hosting_window, kitty_pane_switcher_key) == self)
        objc_setAssociatedObject(hosting_window, kitty_pane_switcher_key, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [entries removeAllObjects]; // Release the copied titles and preview images now.
    PyGILState_STATE gil = PyGILState_Ensure();
    call_boss(pane_switcher_done, "KK", (unsigned long long)tab_id, (unsigned long long)result);
    PyGILState_Release(gil);
    [self release];
}
- (void)windowInvalidated:(NSNotification *)notification { (void)notification; [self finish:NO]; }
- (NSRect)tileRect:(NSUInteger)index {
    return NSMakeRect((index % columns) * (tile_width + 10), (index / columns) * (tile_height + 10), tile_width, tile_height);
}
- (void)revealSelection {
    [grid scrollRectToVisible:[self tileRect:selected]];
    grid.needsDisplay = YES;
}
- (void)cycle:(NSInteger)delta {
    if (![self isValid]) { [self finish:NO]; return; }
    NSInteger count = (NSInteger)entries.count;
    selected = (NSUInteger)(((NSInteger)selected + delta % count + count) % count);
    [self revealSelection];
    self.needsDisplay = YES;
}
- (void)layout {
    if (finished || !entries.count) return;
    [super layout];
    CGFloat width = MAX(0, NSWidth(self.bounds) - 32), height = MAX(0, NSHeight(self.bounds) - 32);
    CGFloat panel_width = MIN(960, width), content_width = MAX(1, panel_width - 40);
    columns = MIN(entries.count, MAX((NSUInteger)1, (NSUInteger)((content_width + 10) / 210)));
    tile_width = MAX(1, (content_width - 10 * (columns - 1)) / columns);
    CGFloat aspect = 1.6;
    OSWindow *osw = os_window_for_id(os_window_id);
    if (osw && osw->active_tab < osw->num_tabs) {
        Tab *tab = osw->tabs + osw->active_tab;
        for (unsigned i = 0; i < tab->num_windows; i++) {
            Window *pane = tab->windows + i;
            if (pane->id != entries[0]->pane_id) continue;
            const WindowGeometry *g = &pane->render_data.geometry;
            if (g->right > g->left && g->bottom > g->top)
                aspect = (CGFloat)(g->right - g->left) / (CGFloat)(g->bottom - g->top);
            break;
        }
    }
    if (entries[0]->image) aspect = entries[0]->image.size.width / entries[0]->image.size.height;
    tile_height = MIN(264, MAX(1, tile_width - 20) / aspect + 44);
    NSUInteger rows = (entries.count + columns - 1) / columns;
    CGFloat grid_height = rows * (tile_height + 10) - 10;
    CGFloat panel_height = MIN(height, grid_height + 68);
    panel_rect = NSMakeRect((NSWidth(self.bounds) - panel_width) / 2, (NSHeight(self.bounds) - panel_height) / 2, panel_width, panel_height);
    scroll_view.frame = NSMakeRect(panel_rect.origin.x + 20, panel_rect.origin.y + 48, MAX(0, panel_width - 40), MAX(0, panel_height - 68));
    grid.frame = NSMakeRect(0, 0, content_width, MAX(grid_height, NSHeight(scroll_view.contentView.bounds)));
    [self revealSelection];
}
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    if (finished) return;
    [[[NSColor blackColor] colorWithAlphaComponent:0.22] setFill]; NSRectFill(self.bounds);
    [NSColor.windowBackgroundColor setFill];
    [[NSBezierPath bezierPathWithRoundedRect:panel_rect xRadius:12 yRadius:12] fill];
    if (panel_rect.size.height < 60) return;
    NSMutableParagraphStyle *style = [[[NSMutableParagraphStyle alloc] init] autorelease];
    style.lineBreakMode = NSLineBreakByTruncatingTail;
    NSString *heading = [NSString stringWithFormat:@"Switch pane  ·  %lu of %lu", (unsigned long)selected + 1, (unsigned long)entries.count];
    [heading drawInRect:NSMakeRect(panel_rect.origin.x + 22, panel_rect.origin.y + 17, MAX(0, panel_rect.size.width - 44), 22)
        withAttributes:@{NSFontAttributeName: [NSFont systemFontOfSize:14 weight:NSFontWeightSemibold],
            NSForegroundColorAttributeName: NSColor.labelColor, NSParagraphStyleAttributeName: style}];
}
- (NSEvent *)handleEvent:(NSEvent *)event {
    if (![self isValid]) { [self finish:NO]; return event; }
    // Modifier state is application-wide; flagsChanged may have no window or a
    // different window number even though this switcher still owns keyboard focus.
    if (event.type == NSEventTypeFlagsChanged) {
        if (release_modifiers && !(event.modifierFlags & release_modifiers)) [self finish:YES];
        return nil;
    }
    if (event.window && event.window != hosting_window) return event;
    if (event.type == NSEventTypeKeyUp &&
        ((release_modifiers && !(event.modifierFlags & release_modifiers)) ||
         (!release_modifiers && event.keyCode == initiating_key))) {
        [self finish:YES];
        return nil;
    }
    if (event.type == NSEventTypeKeyUp || event.type == NSEventTypeLeftMouseUp) return nil;
    if (event.type == NSEventTypeLeftMouseDown) {
        NSPoint point = [grid convertPoint:event.locationInWindow fromView:nil];
        if (NSPointInRect([scroll_view convertPoint:event.locationInWindow fromView:nil], scroll_view.bounds)) {
            for (NSUInteger i = 0; i < entries.count; i++) if (NSPointInRect(point, [self tileRect:i])) {
                selected = i; [self finish:YES]; return nil;
            }
        }
        if (!NSPointInRect([self convertPoint:event.locationInWindow fromView:nil], panel_rect)) [self finish:YES];
        return nil;
    }
    if (event.type == NSEventTypeKeyDown) {
        unsigned short key = event.keyCode;
        BOOL shift = (event.modifierFlags & NSEventModifierFlagShift) != 0;
        if (key == 53 || key == 36 || key == 76) return nil;
        if ([hosting_window.contentView respondsToSelector:@selector(kittyPaneSwitcherHandleConfiguredKeyEvent:)] &&
            [hosting_window.contentView kittyPaneSwitcherHandleConfiguredKeyEvent:event]) return nil;
        else if (key == initiating_key) {
            BOOL reversed = shift != ((initiating_modifiers & NSEventModifierFlagShift) != 0);
            [self cycle:reversed ? -(NSInteger)initiating_delta : initiating_delta];
        } else if (key == 48) [self cycle:shift ? -1 : 1];
        else if (key == 123) [self cycle:-1];
        else if (key == 124) [self cycle:1];
        else if (key == 125) [self cycle:(NSInteger)columns];
        else if (key == 126) [self cycle:-(NSInteger)columns];
        // All other typing is deliberately consumed, never sent to the terminal.
        return nil;
    }
    return event;
}
@end

bool
cocoa_show_pane_switcher(void *ptr, id_type tab_id, PyObject *rows, id_type active_id, int delta) {
    OSWindow *osw = ptr;
    if (!osw || !osw->handle || osw->active_tab >= osw->num_tabs) return false;
    NSWindow *window = glfwGetCocoaWindow(osw->handle);
    KittyPaneSwitcher *existing = objc_getAssociatedObject(window, kitty_pane_switcher_key);
    if (existing) {
        if (existing->tab_id == tab_id && [existing isValid]) { [existing cycle:delta]; return true; }
        [existing finish:NO];
    }
    Tab *tab = osw->tabs + osw->active_tab;
    if (tab->id != tab_id || strcmp(tab->layout_name, "stack") || !window.isKeyWindow) return false;
    PyObject *sequence = PySequence_Fast(rows, "Pane switcher rows must be a sequence");
    if (!sequence) return false;
    NSMutableArray *entries = [NSMutableArray array];
    NSMutableSet *seen = [NSMutableSet set];
    BOOL valid = YES;
    NSUInteger active_index = NSNotFound;
    for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(sequence); i++) {
        PyObject *row = PySequence_Fast(PySequence_Fast_GET_ITEM(sequence, i), "Pane switcher row must contain an ID and title");
        if (!row) { valid = NO; break; }
        if (PySequence_Fast_GET_SIZE(row) != 2) { Py_DECREF(row); valid = NO; break; }
        unsigned long long value = PyLong_AsUnsignedLongLong(PySequence_Fast_GET_ITEM(row, 0));
        Py_ssize_t title_size = 0;
        const char *text = PyUnicode_AsUTF8AndSize(PySequence_Fast_GET_ITEM(row, 1), &title_size);
        BOOL found = NO;
        for (unsigned j = 0; j < tab->num_windows; j++) if (tab->windows[j].id == value) { found = YES; break; }
        if (PyErr_Occurred() || !value || !found || !text || [seen containsObject:@(value)]) {
            Py_DECREF(row); valid = NO; break;
        }
        KittyPaneSwitcherEntry *entry = [[[KittyPaneSwitcherEntry alloc] init] autorelease];
        entry->pane_id = (id_type)value;
        entry->title = [[NSString alloc] initWithBytes:text length:(NSUInteger)title_size encoding:NSUTF8StringEncoding];
        entry->pending = YES;
        if (value == active_id) active_index = entries.count;
        [entries addObject:entry]; [seen addObject:@(value)];
        Py_DECREF(row);
    }
    Py_DECREF(sequence);
    if (!valid || !entries.count || active_index == NSNotFound) return false;
    KittyPaneSwitcher *switcher = [[[KittyPaneSwitcher alloc] initWithFrame:window.contentView.bounds] autorelease];
    switcher->os_window_id = osw->id; switcher->tab_id = tab_id; switcher->hosting_window = window;
    switcher->entries = [entries mutableCopy]; switcher->selected = active_index; switcher->current_index = active_index; switcher->columns = 1;
    switcher->initiating_delta = delta ? delta : 1;
    NSEvent *event = objc_getAssociatedObject(window, NSSelectorFromString(@"kittyPaneSwitcherInitiatingEvent"));
    if (!event) event = NSApp.currentEvent;
    switcher->initiating_key = event.type == NSEventTypeKeyDown ? event.keyCode : USHRT_MAX;
    const NSEventModifierFlags shortcut_modifiers = NSEventModifierFlagCommand | NSEventModifierFlagControl |
        NSEventModifierFlagOption | NSEventModifierFlagShift;
    switcher->initiating_modifiers = (event.type == NSEventTypeKeyDown ? event.modifierFlags : NSEvent.modifierFlags) & shortcut_modifiers;
    switcher->release_modifiers = switcher->initiating_modifiers & ~NSEventModifierFlagShift;
    if (!switcher->release_modifiers) switcher->release_modifiers = switcher->initiating_modifiers & NSEventModifierFlagShift;
    switcher.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    bool (*dispatch)(void *, const GLFWkeyevent *) = cocoa_dispatch_pane_switcher_shortcut;
    objc_setAssociatedObject(window, @selector(kittyPaneSwitcherHandleConfiguredKeyEvent:),
        [NSValue valueWithBytes:&dispatch objCType:@encode(__typeof__(dispatch))], OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    switcher->scroll_view = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    switcher->scroll_view.drawsBackground = NO;
    switcher->scroll_view.hasVerticalScroller = YES;
    switcher->scroll_view.autohidesScrollers = YES;
    switcher->scroll_view.scrollerStyle = NSScrollerStyleOverlay;
    switcher->grid = [[KittyPaneSwitcherGrid alloc] initWithFrame:NSZeroRect];
    switcher->grid->owner = switcher;
    switcher->scroll_view.documentView = switcher->grid;
    [switcher addSubview:switcher->scroll_view];
    [window.contentView addSubview:switcher positioned:NSWindowAbove relativeTo:nil];
    objc_setAssociatedObject(window, kitty_pane_switcher_key, switcher, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [switcher layout]; [switcher cycle:delta];
    // MRC __block captures do not retain: remove the monitor before dropping owners.
    __block KittyPaneSwitcher *monitored_switcher = switcher;
    void (^modifiers_changed)(NSEvent *) = ^(NSEvent *incoming) {
        KittyPaneSwitcher *current = [[monitored_switcher retain] autorelease];
        [current handleEvent:incoming];
    };
    objc_setAssociatedObject(window, NSSelectorFromString(@"kittyPaneSwitcherModifiersChanged:"), modifiers_changed, OBJC_ASSOCIATION_COPY_NONATOMIC);
    switcher->event_monitor = [NSEvent addLocalMonitorForEventsMatchingMask:
        NSEventMaskKeyDown | NSEventMaskKeyUp | NSEventMaskFlagsChanged | NSEventMaskLeftMouseDown | NSEventMaskLeftMouseUp
        handler:^NSEvent *(NSEvent *incoming) {
            KittyPaneSwitcher *current = [[monitored_switcher retain] autorelease];
            return [current handleEvent:incoming];
        }];
    NSNotificationCenter *notifications = [NSNotificationCenter defaultCenter];
    [notifications addObserver:switcher selector:@selector(windowInvalidated:) name:NSWindowDidResignKeyNotification object:window];
    [notifications addObserver:switcher selector:@selector(windowInvalidated:) name:NSWindowWillCloseNotification object:window];
    // A fast release can precede monitor installation. Check the live state once
    // after returning to AppKit, without keeping an idle polling timer alive.
    if (switcher->release_modifiers) dispatch_async(dispatch_get_main_queue(), ^{
        if (!switcher->finished && !(NSEvent.modifierFlags & switcher->release_modifiers))
            [switcher finish:YES];
    });
    return true;
}

id_type
cocoa_pane_switcher_next_preview(void *ptr) {
    OSWindow *osw = ptr;
    if (!osw || !osw->handle) return 0;
    KittyPaneSwitcher *switcher = objc_getAssociatedObject(glfwGetCocoaWindow(osw->handle), kitty_pane_switcher_key);
    if (!switcher) return 0;
    if (![switcher isValid]) { [switcher finish:NO]; return 0; }
    for (KittyPaneSwitcherEntry *entry in switcher->entries) if (entry->pending) return entry->pane_id;
    return 0;
}

void
cocoa_pane_switcher_set_preview(void *ptr, id_type window_id, const unsigned char *pixels, unsigned width, unsigned height) {
    OSWindow *osw = ptr;
    if (!osw || !osw->handle) return;
    KittyPaneSwitcher *switcher = objc_getAssociatedObject(glfwGetCocoaWindow(osw->handle), kitty_pane_switcher_key);
    if (!switcher) return;
    if (![switcher isValid]) { [switcher finish:NO]; return; }
    for (KittyPaneSwitcherEntry *entry in switcher->entries) if (entry->pane_id == window_id && entry->pending) {
        entry->pending = NO;
        if (pixels && width && height && width <= SIZE_MAX / 4 / height) {
            NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:width pixelsHigh:height
                bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
                bitmapFormat:NSBitmapFormatAlphaNonpremultiplied bytesPerRow:(NSInteger)width * 4 bitsPerPixel:32];
            if (bitmap.bitmapData) {
                memcpy(bitmap.bitmapData, pixels, (size_t)width * height * 4);
                entry->image = [[NSImage alloc] initWithSize:NSMakeSize(width, height)];
                // Tag the renderer's sRGB bytes without converting their values.
                NSBitmapImageRep *srgb = [bitmap bitmapImageRepByRetaggingWithColorSpace:NSColorSpace.sRGBColorSpace];
                [entry->image addRepresentation:srgb];
            }
            [bitmap release];
        }
        [switcher layout];
        switcher->grid.needsDisplay = YES;
        return;
    }
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
// These records survive content refreshes and outline reordering. Only changed
// UTF-8 fields become NSStrings; AppKit continues to use IDs and group keys.
static BOOL
agents_sidebar_string_matches(NSString *string, const char *value) {
    return string && strcmp(string.UTF8String, value ? value : "") == 0;
}

static BOOL
agents_sidebar_update_string(NSString **string, const char *value) {
    if (agents_sidebar_string_matches(*string, value)) return NO;
    NSString *next = [[NSString alloc] initWithUTF8String:value ? value : ""];
    [*string release];
    *string = next;
    return YES;
}

@interface KittyAgentsSidebarEntry : NSObject {
@public
    NSNumber *identity;
    NSString *group, *group_key, *agent, *label, *detail, *project_key;
    unsigned char state;
    BOOL editor;
    BOOL active;
    BOOL related, linked;
    unsigned changes;
}
@end
@implementation KittyAgentsSidebarEntry
- (void)dealloc {
    [identity release];
    [group release];
    [group_key release];
    [agent release];
    [label release];
    [detail release];
    [project_key release];
    [super dealloc];
}
@end

@class KittyAgentsSidebarView;

@interface KittyAgentsOutlineView : NSOutlineView
@property BOOL keyboardFocusRequested;
@property(copy) NSString *emptyStateText;
@end

@interface KittyAgentsScrollView : NSScrollView
@end
// Only this small view is layer-hosted; labels and icons keep native drawing.
@interface KittyAgentsSidebarSpinnerView : NSView
@end
@implementation KittyAgentsSidebarSpinnerView
- (BOOL)isFlipped { return YES; }
- (NSView *)hitTest:(NSPoint)point { (void)point; return nil; }
- (BOOL)isAccessibilityElement { return NO; }
@end

@interface KittyAgentsSidebarCell : NSTableCellView {
    KittyAgentsSidebarSpinnerView *spinner_view;
    CAShapeLayer *spinner_track, *spinner_arc;
    BOOL spinner_rotating, window_closing;
}
@property(assign) KittyAgentsSidebarView *sidebar;
@property(retain) id node;
- (void)updateSpinner;
- (void)stopSpinner;
@end

@interface KittyAgentsSidebarRowView : NSTableRowView {
    NSTrackingArea *hover_tracking_area;
}
@property(assign) KittyAgentsSidebarView *sidebar;
@property BOOL activeAgent;
@property BOOL groupHeading;
@property BOOL pointerHovered;
@property BOOL related;
@end


@interface KittyAgentsSidebarView : NSView <NSOutlineViewDataSource, NSOutlineViewDelegate> {
    unsigned os_window_id;
    NSArray<KittyAgentsSidebarEntry *> *entries;
    // NSOutlineView owns selection; IDs survive tracker refreshes and reordering.
    NSDictionary<NSNumber *, KittyAgentsSidebarEntry *> *entries_by_id;
    NSDictionary<NSNumber *, KittyAgentsSidebarEntry *> *editors_by_id;
    NSArray *root_nodes;
    NSDictionary *group_children;
    NSArray *editor_entries;
    NSView *agents_section, *editors_section, *section_divider, *divider_hit_view;
    NSButton *agents_header, *editors_header;
    NSScrollView *editor_scroll_view;
    KittyAgentsOutlineView *editor_outline;
    CGFloat agents_fraction;
    BOOL agents_collapsed, editors_collapsed, dragging_divider;
    CGFloat divider_drag_origin, divider_start_fraction;
    BOOL summary_dirty, row_style_changed;
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
    NSTrackingArea *resize_tracking_area;
    unsigned long long link_source_window_id;
    BOOL link_source_is_editor;
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
    projectKeys:(const char **)new_project_keys
         states:(const unsigned char *)new_states
          active:(const bool *)active
           count:(size_t)count
           kinds:(const char **)new_kinds;
- (NSVisualEffectView *)backgroundView;
- (BOOL)kittySidebarHasKeyboardFocus;
- (void)kittySidebarCancelNavigation;
- (BOOL)kittySidebarHandleKeyEvent:(NSEvent *)event;
- (void)focusSidebar;
- (void)drawNode:(id)node inRect:(NSRect)rect;
- (void)updateRelatedLinkState;
- (NSColor *)workingSpinnerColorForNode:(id)node;
- (NSString *)labelForNode:(id)node;
- (NSMenu *)menuForNode:(id)node;
- (void)rebuildOutline;
- (void)syncGroupExpansion;
- (void)setGroup:(id)group expanded:(BOOL)expanded animated:(BOOL)animated;
+ (NSMutableSet<NSString *> *)sharedCollapsedGroups;
+ (void)setSharedCollapsedGroups:(const char **)keys count:(size_t)count;
@end


@implementation KittyAgentsOutlineView
- (void)drawRect:(NSRect)dirtyRect {
    [super drawRect:dirtyRect];
    if (self.numberOfRows || !self.emptyStateText.length || self.bounds.size.height <= 20) return;
    NSMutableParagraphStyle *style = [[[NSMutableParagraphStyle alloc] init] autorelease];
    style.alignment = NSTextAlignmentCenter;
    NSDictionary *attributes = @{NSFontAttributeName:[NSFont systemFontOfSize:11],
        NSForegroundColorAttributeName:[NSColor tertiaryLabelColor], NSParagraphStyleAttributeName:style};
    [self.emptyStateText drawInRect:NSMakeRect(8, 10, MAX(0, self.bounds.size.width - 24), 16) withAttributes:attributes];
}
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
    // Keep the cell inset for its text/icon content; row backgrounds are drawn
    // independently across the full viewport by KittyAgentsSidebarRowView.
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
    id node = row >= 0 ? [self itemAtRow:row] : nil;
    return [(KittyAgentsSidebarView *)self.delegate menuForNode:node];
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

@interface KittySidebarSectionButton : NSButton
@property(assign) KittyAgentsSidebarView *sidebar;
@property BOOL agentsSection;
@property BOOL expanded;
@property BOOL pointerHovered;
@property(retain) NSTrackingArea *headerTrackingArea;
@end

static NSColor *KittySidebarSectionTint(BOOL agents_section) {
    return agents_section ? [NSColor systemPurpleColor] : [NSColor systemTealColor];
}

static NSColor *KittySidebarSectionColor(BOOL agents_section, BOOL header) {
    // Purple is darker than teal at the same blend fraction. Use per-hue
    // fractions so both sections have matching content/header lightness.
    CGFloat fraction = agents_section ? (header ? 0.07 : 0.04) : (header ? 0.12 : 0.07);
    NSColor *base = [NSColor controlBackgroundColor];
    NSColor *tint = KittySidebarSectionTint(agents_section);
    return [base blendedColorWithFraction:fraction ofColor:tint];
}

static NSColor *KittySidebarSectionBackgroundColor(BOOL agents_section) {
    return KittySidebarSectionColor(agents_section, NO);
}

static NSColor *KittySidebarSectionHeaderColor(BOOL agents_section) {
    return KittySidebarSectionColor(agents_section, YES);
}

@implementation KittySidebarSectionButton
- (void)mouseDown:(NSEvent *)event { [super mouseDown:event]; }
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (self.headerTrackingArea) { [self removeTrackingArea:self.headerTrackingArea]; self.headerTrackingArea = nil; }
    self.headerTrackingArea = [[[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingCursorUpdate | NSTrackingActiveAlways | NSTrackingInVisibleRect
        owner:self userInfo:nil] autorelease];
    [self addTrackingArea:self.headerTrackingArea];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    NSRect visible = NSIntersectionRect(self.bounds, self.visibleRect);
    if (!NSIsEmptyRect(visible)) [self addCursorRect:visible cursor:[NSCursor pointingHandCursor]];
}
- (void)cursorUpdate:(NSEvent *)event {
    (void)event;
    [[NSCursor pointingHandCursor] set];
}
- (void)mouseEntered:(NSEvent *)event { (void)event; self.pointerHovered = YES; [self setNeedsDisplay:YES]; [[NSCursor pointingHandCursor] set]; }
- (void)mouseMoved:(NSEvent *)event { [self mouseEntered:event]; }
- (void)mouseExited:(NSEvent *)event { (void)event; self.pointerHovered = NO; [self setNeedsDisplay:YES]; [[NSCursor arrowCursor] set]; }
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    NSColor *tint = KittySidebarSectionTint(self.agentsSection);
    NSColor *band = KittySidebarSectionHeaderColor(self.agentsSection);
    if (self.pointerHovered) {
        NSColor *hover_tint = self.agentsSection ? tint : [NSColor controlAccentColor];
        band = [band blendedColorWithFraction:0.12 ofColor:hover_tint];
    }
    [band setFill]; NSRectFill(self.bounds);
    [[NSColor separatorColor] setFill]; NSRectFill(NSMakeRect(0, NSMaxY(self.bounds) - 1, self.bounds.size.width, 1));
    [tint setFill]; NSRectFill(NSMakeRect(0, 0, 2, self.bounds.size.height));
    NSFont *titleFont = self.font ? self.font : [NSFont systemFontOfSize:11];
    NSColor *titleColor = self.pointerHovered ? [NSColor labelColor] : [NSColor secondaryLabelColor];
    NSMutableParagraphStyle *titleStyle = [[[NSMutableParagraphStyle alloc] init] autorelease];
    titleStyle.lineBreakMode = NSLineBreakByTruncatingTail;
    NSRect titleRect = NSMakeRect(12, 0, MAX(0, NSWidth(self.bounds) - 38), NSHeight(self.bounds));
    NSSize titleSize = [self.title sizeWithAttributes:@{NSFontAttributeName:titleFont}];
    titleRect.origin.y = NSMidY(self.bounds) - titleSize.height / 2;
    titleRect.size.height = titleSize.height;
    [self.title drawInRect:titleRect withAttributes:@{NSFontAttributeName:titleFont,
        NSForegroundColorAttributeName:titleColor, NSParagraphStyleAttributeName:titleStyle}];
    NSBezierPath *chevron = [NSBezierPath bezierPath];
    chevron.lineWidth = 1.5; chevron.lineCapStyle = NSLineCapStyleRound; chevron.lineJoinStyle = NSLineJoinStyleRound;
    CGFloat cx = NSWidth(self.bounds) - 15, cy = NSMidY(self.bounds);
    if (self.expanded) {
        [chevron moveToPoint:NSMakePoint(cx - 3, cy - 1)]; [chevron lineToPoint:NSMakePoint(cx, cy + 2)];
        [chevron lineToPoint:NSMakePoint(cx + 3, cy - 1)];
    } else {
        [chevron moveToPoint:NSMakePoint(cx - 1, cy - 3)]; [chevron lineToPoint:NSMakePoint(cx + 2, cy)];
        [chevron lineToPoint:NSMakePoint(cx - 1, cy + 3)];
    }
    [titleColor setStroke]; [chevron stroke];
}
- (void)viewDidMoveToWindow { [super viewDidMoveToWindow]; [self updateTrackingAreas]; }
- (void)dealloc {
    if (self.headerTrackingArea) [self removeTrackingArea:self.headerTrackingArea];
    [_headerTrackingArea release];
    [super dealloc];
}
@end
@implementation KittyAgentsSidebarCell
- (instancetype)initWithFrame:(NSRect)frame {
    self = [super initWithFrame:frame];
    if (self) {
        [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(spinnerPreferencesChanged:)
            name:NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification object:nil];
    }
    return self;
}
- (void)stopSpinner {
    [spinner_arc removeAllAnimations];
    spinner_rotating = NO;
    spinner_view.hidden = YES;
}
- (void)updateSpinner {
    if (!self.window || window_closing || !self.superview || self.hiddenOrHasHiddenAncestor || NSIsEmptyRect(self.visibleRect)) {
        [self stopSpinner];
        return;
    }
    NSColor *color = [self.sidebar workingSpinnerColorForNode:self.node];
    if (!color) {
        [self stopSpinner];
        return;
    }
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    if (!spinner_view) {
        spinner_view = [[KittyAgentsSidebarSpinnerView alloc] initWithFrame:NSZeroRect];
        spinner_track = [CAShapeLayer layer];
        spinner_arc = [CAShapeLayer layer];
        spinner_track.geometryFlipped = YES;
        spinner_track.fillColor = spinner_arc.fillColor = nil;
        spinner_track.lineWidth = spinner_arc.lineWidth = 1.8;
        spinner_arc.lineCap = kCALineCapRound;
        // Preserve the existing 5.5pt outer radius, including half the stroke.
        const CGFloat radius = 5.5 - 1.8 / 2;
        CGMutablePathRef track = CGPathCreateMutable();
        CGPathAddEllipseInRect(track, NULL, CGRectMake(5.5 - radius, 5.5 - radius, 2 * radius, 2 * radius));
        spinner_track.path = track;
        CGPathRelease(track);
        CGMutablePathRef arc = CGPathCreateMutable();
        CGPathAddArc(arc, NULL, 5.5, 5.5, radius, 0, -100.0 * M_PI / 180.0, true);
        spinner_arc.path = arc;
        CGPathRelease(arc);
        spinner_arc.frame = CGRectMake(0, 0, 11, 11);
        [spinner_track addSublayer:spinner_arc];
        spinner_view.layer = spinner_track;
        spinner_view.wantsLayer = YES;
        [self addSubview:spinner_view];
    }
    spinner_view.frame = NSMakeRect(4, NSMidY(self.bounds) - 5.5, 11, 11);
    spinner_track.contentsScale = spinner_arc.contentsScale = self.window.backingScaleFactor;
    // Semantic NSColors must be resolved using this cell's current appearance.
    if (@available(macOS 11.0, *)) {
        [self.effectiveAppearance performAsCurrentDrawingAppearance:^{
            spinner_track.strokeColor = [color colorWithAlphaComponent:0.25].CGColor;
            spinner_arc.strokeColor = color.CGColor;
        }];
    } else {
        spinner_track.strokeColor = [color colorWithAlphaComponent:0.25].CGColor;
        spinner_arc.strokeColor = color.CGColor;
    }
    spinner_view.hidden = NO;
    BOOL rotate = self.window.isVisible && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    if (rotate && !spinner_rotating) {
        CABasicAnimation *rotation = [CABasicAnimation animationWithKeyPath:@"transform.rotation.z"];
        rotation.fromValue = @0;
        rotation.toValue = @(-2 * M_PI);
        rotation.duration = 1.0 / 0.9;
        rotation.repeatCount = HUGE_VALF;
        rotation.timingFunction = [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionLinear];
        [spinner_arc addAnimation:rotation forKey:@"working-rotation"];
        spinner_rotating = YES;
    } else if (!rotate && spinner_rotating) {
        [spinner_arc removeAllAnimations];
        spinner_rotating = NO;
    }
    [CATransaction commit];
}
- (void)spinnerPreferencesChanged:(NSNotification *)notification { (void)notification; [self updateSpinner]; }
- (void)spinnerWindowVisibilityChanged:(NSNotification *)notification { (void)notification; [self updateSpinner]; }
- (void)spinnerWindowWillClose:(NSNotification *)notification {
    (void)notification;
    window_closing = YES;
    [self stopSpinner];
}
- (void)viewDidMoveToWindow {
    [super viewDidMoveToWindow];
    NSNotificationCenter *center = NSNotificationCenter.defaultCenter;
    [center removeObserver:self name:NSWindowWillCloseNotification object:nil];
    [center removeObserver:self name:NSWindowDidChangeOcclusionStateNotification object:nil];
    window_closing = NO;
    if (self.window) {
        [center addObserver:self selector:@selector(spinnerWindowWillClose:) name:NSWindowWillCloseNotification object:self.window];
        [center addObserver:self selector:@selector(spinnerWindowVisibilityChanged:) name:NSWindowDidChangeOcclusionStateNotification object:self.window];
    }
    [self updateSpinner];
}
- (void)viewDidMoveToSuperview { [super viewDidMoveToSuperview]; [self updateSpinner]; }
- (void)viewDidHide { [super viewDidHide]; [self stopSpinner]; }
- (void)viewDidUnhide { [super viewDidUnhide]; [self updateSpinner]; }
- (void)viewDidChangeEffectiveAppearance { [super viewDidChangeEffectiveAppearance]; [self updateSpinner]; }
- (void)viewDidChangeBackingProperties { [super viewDidChangeBackingProperties]; [self updateSpinner]; }
- (void)setFrameSize:(NSSize)size { [super setFrameSize:size]; [self updateSpinner]; }
- (void)prepareForReuse {
    [self stopSpinner];
    self.node = nil;
    self.sidebar = nil;
    [super prepareForReuse];
}
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
    [self stopSpinner];
    [NSNotificationCenter.defaultCenter removeObserver:self];
    [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self];
    [spinner_view release];
    [_node release];
    [super dealloc];
}
@end

@implementation KittyAgentsSidebarRowView
- (NSRect)decorationRect {
    NSRect rect = self.bounds;
    rect.size.width = MIN(rect.size.width, self.enclosingScrollView.contentSize.width);
    // Keep the 24pt group band intact. Agent/editor fills span the row width and
    // touch the row edges vertically, avoiding the apparent gap below headers.
    return rect;
}
- (void)drawBackgroundInRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    BOOL highlighted = self.pointerHovered || (self.selected && [self.sidebar kittySidebarHasKeyboardFocus]);
    NSRect rect = [self decorationRect];
    if (self.groupHeading) {
        NSColor *background = [[NSColor windowBackgroundColor] blendedColorWithFraction:highlighted ? 0.14 : 0.08
            ofColor:[NSColor labelColor]];
        [background setFill];
        NSRectFill(rect);
        [[[NSColor labelColor] colorWithAlphaComponent:0.14] setFill];
        NSRectFill(NSMakeRect(NSMinX(rect), NSMaxY(rect) - 1, rect.size.width, 1));
    } else {
        if (self.related) {
            [[[NSColor controlAccentColor] colorWithAlphaComponent:0.10] setFill];
            NSRectFill(rect);
        }
        if (self.activeAgent || highlighted) {
            NSColor *background = self.activeAgent ? [NSColor selectedContentBackgroundColor] :
                [[NSColor controlBackgroundColor] colorWithAlphaComponent:0.38];
            [background setFill];
            NSRectFill(rect);
        }
        if (self.related) {
            [[[NSColor controlAccentColor] colorWithAlphaComponent:0.78] setFill];
            NSRectFill(NSMakeRect(NSMinX(rect), NSMinY(rect), 2, rect.size.height));
        }
    }
}
- (void)drawSelectionInRect:(NSRect)dirtyRect {
    // Selection shares the background's hover fill; suppress AppKit's default decoration.
    (void)dirtyRect;
}
- (void)viewDidChangeEffectiveAppearance {
    [super viewDidChangeEffectiveAppearance];
    [self setNeedsDisplay:YES];
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


@interface KittySidebarDividerHitView : NSView
@property(assign) KittyAgentsSidebarView *sidebar;
@end

@implementation KittySidebarDividerHitView
- (BOOL)acceptsFirstMouse:(NSEvent *)event { (void)event; return YES; }
- (void)resetCursorRects {
    [super resetCursorRects];
    NSRect visible = NSIntersectionRect(self.bounds, self.visibleRect);
    if (!NSIsEmptyRect(visible)) [self addCursorRect:visible cursor:[NSCursor resizeUpDownCursor]];
}
- (void)cursorUpdate:(NSEvent *)event { (void)event; [[NSCursor resizeUpDownCursor] set]; }
- (void)mouseDown:(NSEvent *)event { [self.sidebar mouseDown:event]; }
- (void)mouseDragged:(NSEvent *)event { [self.sidebar mouseDragged:event]; }
- (void)mouseUp:(NSEvent *)event { [self.sidebar mouseUp:event]; }
@end

@implementation KittyAgentsSidebarView
- (instancetype)initWithOSWindowID:(unsigned)window_id width:(CGFloat)width cellWidth:(unsigned)cell_width {
    self = [super initWithFrame:NSMakeRect(0, 0, width, 100)];
    if (self) {
        os_window_id = window_id;
        summary_dirty = YES;
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
        NSUserDefaults *prefs = NSUserDefaults.standardUserDefaults;
        NSString *scope = @"kitty.agents-sidebar.sections.v1";
        agents_collapsed = [prefs boolForKey:[scope stringByAppendingString:@".agents-collapsed"]];
        editors_collapsed = [prefs boolForKey:[scope stringByAppendingString:@".editors-collapsed"]];
        CGFloat saved_fraction = [prefs doubleForKey:[scope stringByAppendingString:@".agents-fraction"]];
        agents_fraction = isfinite(saved_fraction) && saved_fraction >= 0.12 && saved_fraction <= 0.88 ? saved_fraction : 0.62;
        agents_header = [[KittySidebarSectionButton alloc] initWithFrame:NSZeroRect];
        editors_header = [[KittySidebarSectionButton alloc] initWithFrame:NSZeroRect];
        ((KittySidebarSectionButton *)agents_header).sidebar = self;
        ((KittySidebarSectionButton *)editors_header).sidebar = self;
        ((KittySidebarSectionButton *)agents_header).agentsSection = YES;
        ((KittySidebarSectionButton *)editors_header).agentsSection = NO;
        for (NSButton *header in @[agents_header, editors_header]) {
            header.bordered = NO; header.alignment = NSTextAlignmentLeft; header.font = [NSFont systemFontOfSize:11 weight:NSFontWeightSemibold];
            header.contentTintColor = NSColor.secondaryLabelColor; header.imagePosition = NSNoImage;
            header.cell.wraps = NO;
            header.cell.usesSingleLineMode = YES;
            header.cell.truncatesLastVisibleLine = YES;
            header.cell.lineBreakMode = NSLineBreakByTruncatingTail;
        }
        agents_header.title = @"Grouping: tab · 0 agents · 0 groups"; agents_header.target = self; agents_header.action = @selector(toggleAgentsSection:);
        agents_header.allowsExpansionToolTips = NO;
        agents_header.image = nil;
        editors_header.title = @"Editors"; editors_header.target = self; editors_header.action = @selector(toggleEditorsSection:);
        editors_header.image = nil;
        [self updateSectionHeaders];
        [self addSubview:agents_header]; [self addSubview:editors_header];
        scroll_view = [[KittyAgentsScrollView alloc] initWithFrame:NSZeroRect];
        scroll_view.drawsBackground = NO;
        scroll_view.hasVerticalScroller = YES;
        scroll_view.autohidesScrollers = YES;
        scroll_view.scrollerStyle = NSScrollerStyleOverlay;
        scroll_view.borderType = NSNoBorder;
        scroll_view.contentInsets = NSEdgeInsetsZero;
        outline = [[KittyAgentsOutlineView alloc] initWithFrame:NSZeroRect];
        NSTableColumn *column = [[[NSTableColumn alloc] initWithIdentifier:@"agent"] autorelease];
        column.minWidth = 0;
        column.resizingMask = NSTableColumnAutoresizingMask;
        [outline addTableColumn:column];
        outline.outlineTableColumn = column;
        outline.headerView = nil;
        outline.backgroundColor = [NSColor clearColor];
        // Source-list/automatic tables add platform-dependent row insets. Use a
        // plain table so the first row starts directly at the scroll content edge.
        if (@available(macOS 11.0, *)) outline.style = NSTableViewStylePlain;
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
        outline.emptyStateText = @"No Agents";
        outline.dataSource = self;
        outline.delegate = self;
        outline.target = self;
        outline.action = @selector(outlineClicked:);
        scroll_view.documentView = outline;
        scroll_view.contentView.postsBoundsChangedNotifications = YES;
        [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(spinnerViewportChanged:)
            name:NSViewBoundsDidChangeNotification object:scroll_view.contentView];
        [self addSubview:scroll_view];
        editor_scroll_view = [[KittyAgentsScrollView alloc] initWithFrame:NSZeroRect];
        editor_scroll_view.drawsBackground = NO; editor_scroll_view.hasVerticalScroller = YES;
        editor_scroll_view.autohidesScrollers = YES; editor_scroll_view.scrollerStyle = NSScrollerStyleOverlay;
        editor_scroll_view.borderType = NSNoBorder;
        editor_scroll_view.contentInsets = NSEdgeInsetsZero;
        editor_outline = [[KittyAgentsOutlineView alloc] initWithFrame:NSZeroRect];
        NSTableColumn *editor_column = [[[NSTableColumn alloc] initWithIdentifier:@"editor"] autorelease];
        editor_column.minWidth = 0; editor_column.resizingMask = NSTableColumnAutoresizingMask;
        [editor_outline addTableColumn:editor_column]; editor_outline.outlineTableColumn = editor_column;
        editor_outline.headerView = nil; editor_outline.backgroundColor = NSColor.clearColor;
        if (@available(macOS 11.0, *)) editor_outline.style = NSTableViewStylePlain;
        editor_outline.rowHeight = 48;
        editor_outline.usesAutomaticRowHeights = NO; editor_outline.intercellSpacing = NSMakeSize(0, 0);
        editor_outline.dataSource = self; editor_outline.delegate = self; editor_outline.allowsTypeSelect = NO;
        editor_outline.target = self; editor_outline.action = @selector(outlineClicked:);
        editor_outline.accessibilityLabel = @"Editors"; editor_outline.emptyStateText = @"No Editors"; editor_scroll_view.documentView = editor_outline;
        [self addSubview:editor_scroll_view];
        section_divider = [[NSView alloc] initWithFrame:NSZeroRect];
        section_divider.wantsLayer = YES; section_divider.layer.backgroundColor = [NSColor separatorColor].CGColor;
        [self addSubview:section_divider];
        divider_hit_view = [[KittySidebarDividerHitView alloc] initWithFrame:NSZeroRect];
        ((KittySidebarDividerHitView *)divider_hit_view).sidebar = self;
        divider_hit_view.hidden = YES;
        [self addSubview:divider_hit_view];
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

@synthesize grouping = _grouping, iconsDir = _iconsDir;
- (void)persistSectionState {
    NSString *scope = @"kitty.agents-sidebar.sections.v1";
    [NSUserDefaults.standardUserDefaults setBool:agents_collapsed forKey:[scope stringByAppendingString:@".agents-collapsed"]];
    [NSUserDefaults.standardUserDefaults setBool:editors_collapsed forKey:[scope stringByAppendingString:@".editors-collapsed"]];
    [NSUserDefaults.standardUserDefaults setDouble:agents_fraction forKey:[scope stringByAppendingString:@".agents-fraction"]];
}
- (void)updateSectionHeaders {
    agents_header.image = editors_header.image = nil;
    agents_header.imagePosition = editors_header.imagePosition = NSNoImage;
    ((KittySidebarSectionButton *)agents_header).expanded = !agents_collapsed;
    ((KittySidebarSectionButton *)editors_header).expanded = !editors_collapsed;
    [agents_header setNeedsDisplay:YES]; [editors_header setNeedsDisplay:YES];
}
- (void)toggleAgentsSection:(id)sender { (void)sender; agents_collapsed = !agents_collapsed; [self persistSectionState]; [self updateSectionHeaders]; [self setFrameSize:self.frame.size]; }
- (void)toggleEditorsSection:(id)sender { (void)sender; editors_collapsed = !editors_collapsed; [self persistSectionState]; [self updateSectionHeaders]; [self setFrameSize:self.frame.size]; }
- (NSRect)dividerHitRect {
    if (agents_collapsed || editors_collapsed || NSIsEmptyRect(section_divider.frame)) return NSZeroRect;
    CGFloat top = NSMaxY(agents_header.frame);
    CGFloat bottom = NSMinY(editors_header.frame);
    CGFloat center = NSMidY(section_divider.frame);
    CGFloat hit_height = MIN(8, MAX(0, bottom - top));
    CGFloat y = MIN(MAX(center - hit_height / 2, top), bottom - hit_height);
    return NSMakeRect(NSMinX(section_divider.frame), y, section_divider.frame.size.width, hit_height);
}
- (CGFloat)dividerDragAvailableHeight {
    return MAX(1, self.bounds.size.height - 60 - 1);
}
- (void)mouseDown:(NSEvent *)event {
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    if (NSPointInRect(point, [self dividerHitRect])) {
        dragging_divider = YES; divider_drag_origin = point.y; divider_start_fraction = agents_fraction;
        [[NSCursor resizeUpDownCursor] set]; return;
    }
    if (point.x >= self.bounds.size.width - 8) { resizing = YES; resize_hovered = YES; [self setNeedsDisplay:YES]; return; }
    [super mouseDown:event];
}
- (void)mouseDragged:(NSEvent *)event {
    if (dragging_divider) {
        NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
        CGFloat available = [self dividerDragAvailableHeight];
        CGFloat next = divider_start_fraction + (point.y - divider_drag_origin) / available;
        agents_fraction = MIN(0.88, MAX(0.12, next)); [self setFrameSize:self.frame.size]; return;
    }
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
    if (dragging_divider) { dragging_divider = NO; [self persistSectionState]; return; }
    resizing = NO;
    NSPoint p = [self convertPoint:event.locationInWindow fromView:nil];
    [self updateHoverAtPoint:p]; [self resetCursorRects];
}
- (NSString *)grouping { return _grouping; }
- (NSString *)iconsDir { return _iconsDir; }
- (void)setGrouping:(NSString *)value {
    if ([_grouping isEqualToString:value]) return;
    [_grouping release];
    _grouping = [value copy];
    summary_dirty = YES;
}
- (void)setIconsDir:(NSString *)value {
    if ([_iconsDir isEqualToString:value]) return;
    [_iconsDir release];
    _iconsDir = [value copy];
    row_style_changed = YES;
}

- (NSView *)hitTest:(NSPoint)point {
    NSPoint local = [self convertPoint:point fromView:self.superview];
    if (!NSPointInRect(local, self.bounds)) return nil;
    if (local.x >= self.bounds.size.width - 8) return self;
    if (NSPointInRect(local, [self dividerHitRect])) return divider_hit_view;
    return [super hitTest:point];
}

- (BOOL)acceptsFirstMouse:(NSEvent *)event {
    (void)event;
    return YES;
}

- (void)setFrameSize:(NSSize)size {
    [super setFrameSize:size];
    [background_view setFrame:self.frame];
    CGFloat width = MAX(0, size.width - 2), height = MAX(0, size.height);
    CGFloat agents_header_y = 0;
    CGFloat header_height = MIN(30, MAX(0, height) / 2);
    CGFloat divider_height = (!agents_collapsed && !editors_collapsed) ? MIN(1, MAX(0, height - agents_header_y - 2 * header_height)) : 0;
    CGFloat available = MAX(0, height - agents_header_y - 2 * header_height - divider_height);
    CGFloat agent_height = 0, editor_height = 0;
    BOOL ac = agents_collapsed, ec = editors_collapsed;
    if (ac && ec) { agent_height = editor_height = 0; }
    else if (ac) editor_height = available;
    else if (ec) agent_height = available;
    else { agent_height = available * agents_fraction; editor_height = available - agent_height; }
    agents_header.frame = NSMakeRect(0, agents_header_y, width, header_height);
    agents_header.toolTip = agents_header.title;
    summary.frame = NSZeroRect; // Retained for the existing summary/cache path; section titles stay anchored.
    scroll_view.hidden = ac;
    editor_scroll_view.hidden = ec;
    scroll_view.frame = NSMakeRect(0, agents_header_y + header_height, width, agent_height);
    CGFloat divider_y = NSMaxY(scroll_view.frame);
    section_divider.frame = NSMakeRect(0, divider_y, width, divider_height);
    CGFloat editor_header_y = divider_y + divider_height;
    editors_header.frame = NSMakeRect(0, editor_header_y, width, header_height);
    editor_scroll_view.frame = NSMakeRect(0, editor_header_y + header_height, width, editor_height);
    outline.tableColumns.firstObject.width = MAX(0, scroll_view.contentSize.width);
    editor_outline.tableColumns.firstObject.width = MAX(0, editor_scroll_view.contentSize.width);
    NSRect divider_hit_rect = [self dividerHitRect];
    divider_hit_view.frame = divider_hit_rect;
    divider_hit_view.hidden = NSIsEmptyRect(divider_hit_rect);
    [self.window invalidateCursorRectsForView:divider_hit_view];
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
    if (NSPointInRect(point, [self dividerHitRect])) [[NSCursor resizeUpDownCursor] set];
    else if (point.x >= self.bounds.size.width - 8) [[NSCursor resizeLeftRightCursor] set];
    else [[NSCursor pointingHandCursor] set];
}
- (void)resetCursorRects {
    [super resetCursorRects];
    const CGFloat edge = 8.0;
    if (self.bounds.size.width > edge) {
        [self addCursorRect:NSMakeRect(self.bounds.size.width - edge, 0, edge, self.bounds.size.height) cursor:[NSCursor resizeLeftRightCursor]];
        [self addCursorRect:NSMakeRect(0, 0, self.bounds.size.width - edge, self.bounds.size.height) cursor:[NSCursor pointingHandCursor]];
    }
    NSRect divider_hit_rect = [self dividerHitRect];
    if (!NSIsEmptyRect(divider_hit_rect)) [self addCursorRect:divider_hit_rect cursor:[NSCursor resizeUpDownCursor]];
}


+ (NSMutableSet<NSString *> *)sharedCollapsedGroups {
    static NSMutableSet<NSString *> *shared;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ shared = [[NSMutableSet alloc] init]; });
    return shared;
}


+ (void)setSharedCollapsedGroups:(const char **)keys count:(size_t)count {
    NSMutableSet<NSString *> *collapsed = [self sharedCollapsedGroups];
    if (collapsed.count == count) {
        BOOL unchanged = YES;
        for (size_t i = 0; i < count && unchanged; i++) {
            BOOL found = NO;
            for (NSString *key in collapsed) {
                if (agents_sidebar_string_matches(key, keys[i])) { found = YES; break; }
            }
            unchanged = found;
        }
        if (unchanged) return;
    }
    [collapsed removeAllObjects];
    for (size_t i = 0; i < count; i++) {
        if (keys[i] && *keys[i]) [collapsed addObject:[NSString stringWithUTF8String:keys[i]]];
    }
}


- (void)sharedGroupStateChanged:(NSNotification *)notification {
    if (notification.object != self) [self syncGroupExpansion];
}

- (void)syncGroupExpansion {
    syncing_outline = YES;
    for (id node in root_nodes) {
        if ([node isKindOfClass:[NSString class]]) [self setGroup:node expanded:![collapsed_groups containsObject:node] animated:YES];
    }
    syncing_outline = NO;
}

- (KittyAgentsOutlineView *)navigationOutline {
    NSResponder *responder = self.window.firstResponder;
    return [responder isKindOfClass:[NSView class]] && [(NSView *)responder isDescendantOf:editor_outline] ? editor_outline : outline;
}
- (id)selectedNode {
    NSOutlineView *view = [self navigationOutline];
    return view.selectedRow >= 0 ? [view itemAtRow:view.selectedRow] : nil;
}

- (KittyAgentsSidebarEntry *)linkEntryForWindowID:(unsigned long long)window_id editor:(BOOL)editor {
    return (editor ? editors_by_id : entries_by_id)[@(window_id)];
}

- (void)clearRelatedLinkState {
    for (KittyAgentsSidebarEntry *entry in entries) {
        entry->related = NO;
        entry->linked = NO;
    }
}

- (void)refreshRelatedLinkStateForOutline:(NSOutlineView *)view {
    [view enumerateAvailableRowViewsUsingBlock:^(NSTableRowView *rowView, NSInteger row) {
        id node = [view itemAtRow:row];
        KittyAgentsSidebarEntry *entry = [node isKindOfClass:[KittyAgentsSidebarEntry class]] ? (KittyAgentsSidebarEntry *)node : entries_by_id[node];
        ((KittyAgentsSidebarRowView *)rowView).related = entry && entry->related;
        [rowView setNeedsDisplay:YES];
        KittyAgentsSidebarCell *cell = (KittyAgentsSidebarCell *)[rowView viewAtColumn:0];
        if (entry) cell.toolTip = [self labelForNode:entry];
        [cell setNeedsDisplay:YES];
    }];
}

- (void)refreshRelatedLinkRowViews {
    [self refreshRelatedLinkStateForOutline:outline];
    [self refreshRelatedLinkStateForOutline:editor_outline];
}

- (BOOL)applyLinkStateFromEntry:(KittyAgentsSidebarEntry *)source {
    [self clearRelatedLinkState];
    if (!source || !source->identity.unsignedLongLongValue || !source->project_key.length) {
        [self refreshRelatedLinkRowViews];
        return NO;
    }
    BOOL found = NO;
    for (KittyAgentsSidebarEntry *entry in entries) {
        if (entry == source || !entry->identity.unsignedLongLongValue || entry->editor == source->editor) continue;
        if (!entry->project_key.length || ![entry->project_key isEqualToString:source->project_key]) continue;
        entry->related = YES;
        found = YES;
    }
    if (found) source->linked = YES;
    [self refreshRelatedLinkRowViews];
    return found;
}

- (void)updateRelatedLinkState {
    KittyAgentsSidebarEntry *source = nil;
    if (link_source_window_id) source = [self linkEntryForWindowID:link_source_window_id editor:link_source_is_editor];
    if (source) [self applyLinkStateFromEntry:source];
    else {
        [self clearRelatedLinkState];
        [self refreshRelatedLinkRowViews];
    }
}

- (void)clearOtherOutlineSelection:(NSOutlineView *)view {
    NSOutlineView *other = view == outline ? editor_outline : outline;
    if (other.selectedRow < 0) return;
    BOOL was_syncing = syncing_outline;
    syncing_outline = YES;
    [other deselectAll:nil];
    syncing_outline = was_syncing;
}

- (void)setLinkSourceFromOutline:(NSOutlineView *)view {
    id node = view.selectedRow >= 0 ? [view itemAtRow:view.selectedRow] : nil;
    KittyAgentsSidebarEntry *entry = nil;
    if ([node isKindOfClass:[KittyAgentsSidebarEntry class]]) entry = node;
    else if ([node isKindOfClass:[NSNumber class]]) entry = entries_by_id[node];
    if (entry) {
        link_source_window_id = entry->identity.unsignedLongLongValue;
        link_source_is_editor = entry->editor;
    } else {
        link_source_window_id = 0;
        link_source_is_editor = NO;
    }
    [self updateRelatedLinkState];
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
    if (view == editor_outline) return item ? 0 : (NSInteger)editor_entries.count;
    return (NSInteger)(item ? [group_children[item] count] : root_nodes.count);
}
- (id)outlineView:(NSOutlineView *)view child:(NSInteger)index ofItem:(id)item {
    (void)view;
    if (view == editor_outline) return editor_entries[(NSUInteger)index];
    return item ? group_children[item][(NSUInteger)index] : root_nodes[(NSUInteger)index];
}
- (BOOL)outlineView:(NSOutlineView *)view isItemExpandable:(id)item {
    (void)view;
    if (view == editor_outline) return NO;
    return [item isKindOfClass:[NSString class]];
}
- (CGFloat)outlineView:(NSOutlineView *)view heightOfRowByItem:(id)item {
    (void)view;
    if (view == editor_outline) return 48;
    return [item isKindOfClass:[NSString class]] ? 24 : 48;
}
- (NSView *)outlineView:(NSOutlineView *)view viewForTableColumn:(NSTableColumn *)column item:(id)item {
    (void)column;
    if (view == editor_outline) {
        KittyAgentsSidebarCell *cell = [view makeViewWithIdentifier:@"agent-cell" owner:self];
        if (!cell) { cell = [[[KittyAgentsSidebarCell alloc] initWithFrame:NSZeroRect] autorelease]; cell.identifier = @"agent-cell"; }
        cell.sidebar = self; cell.node = item; cell.toolTip = [self labelForNode:item]; [cell updateSpinner]; [cell setNeedsDisplay:YES]; return cell;
    }
    KittyAgentsSidebarCell *cell = [view makeViewWithIdentifier:@"agent-cell" owner:self];
    if (!cell) {
        cell = [[[KittyAgentsSidebarCell alloc] initWithFrame:NSZeroRect] autorelease];
        cell.identifier = @"agent-cell";
    }
    cell.sidebar = self;
    cell.node = item;
    cell.toolTip = [self labelForNode:item];
    [cell updateSpinner];
    [cell setNeedsDisplay:YES];
    return cell;
}
- (NSTableRowView *)outlineView:(NSOutlineView *)view rowViewForItem:(id)item {
    (void)view;
    KittyAgentsSidebarRowView *row = [[[KittyAgentsSidebarRowView alloc] initWithFrame:NSZeroRect] autorelease];
    row.sidebar = self;
    row.groupHeading = view != editor_outline && [item isKindOfClass:[NSString class]];
    KittyAgentsSidebarEntry *entry = view == editor_outline ? (KittyAgentsSidebarEntry *)item : entries_by_id[item];
    row.activeAgent = entry && entry->active;
    row.related = entry && entry->related;
    return row;
}
- (void)outlineView:(NSOutlineView *)view didAddRowView:(NSTableRowView *)rowView forRow:(NSInteger)row {
    (void)view; (void)row;
    [(KittyAgentsSidebarCell *)[rowView viewAtColumn:0] updateSpinner];
}
- (void)outlineView:(NSOutlineView *)view didRemoveRowView:(NSTableRowView *)rowView forRow:(NSInteger)row {
    (void)view; (void)row;
    [(KittyAgentsSidebarCell *)[rowView viewAtColumn:0] stopSpinner];
}
- (void)spinnerViewportChanged:(NSNotification *)notification {
    (void)notification;
    [outline enumerateAvailableRowViewsUsingBlock:^(NSTableRowView *rowView, NSInteger row) { (void)row; [(KittyAgentsSidebarCell *)[rowView viewAtColumn:0] updateSpinner]; }];
}
- (void)outlineViewSelectionDidChange:(NSNotification *)notification {
    if (syncing_outline) return;
    if (notification.object == editor_outline) {
        [self clearOtherOutlineSelection:editor_outline];
        [self setLinkSourceFromOutline:editor_outline];
        return;
    }
    [self clearOtherOutlineSelection:outline];
    id selected = outline.selectedRow >= 0 ? [outline itemAtRow:outline.selectedRow] : nil;
    [remembered_selection release];
    remembered_selection = [selected retain];
    [self setLinkSourceFromOutline:outline];
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
    if (agents_collapsed && editors_collapsed) {
        agents_collapsed = NO;
        [self persistSectionState]; [self updateSectionHeaders]; [self setFrameSize:self.frame.size];
    }
    if (![self kittySidebarHasKeyboardFocus]) {
        [previous_responders removeAllObjects];
        if (self.window.firstResponder) [previous_responders addObject:self.window.firstResponder];
    }
    id target = remembered_selection;
    if (!has_entered_navigation || !target) {
        for (KittyAgentsSidebarEntry *entry in entries) {
            if (!entry->editor && entry->active && entry->identity.unsignedLongLongValue) { target = entry->identity; break; }
        }
    }
    has_entered_navigation = YES;
    if ([target isKindOfClass:[NSNumber class]] && entries_by_id[target]) {
        NSString *key = entries_by_id[target]->group_key;
        if (key.length) [outline expandItem:key];
    }
    NSInteger row = -1;
    for (NSInteger i = 0; i < outline.numberOfRows; i++) if ([[outline itemAtRow:i] isEqual:target]) { row = i; break; }
    if (row < 0 && outline.numberOfRows) row = 0;
    if (row >= 0) {
        [outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
        [self revealRow:row];
    }
    BOOL has_agents = NO, has_editors = NO;
    KittyAgentsSidebarEntry *active_editor = nil;
    for (KittyAgentsSidebarEntry *entry in entries) {
        if (!entry->identity.unsignedLongLongValue) continue;
        if (entry->editor) {
            has_editors = YES;
            if (entry->active) active_editor = entry;
        } else has_agents = YES;
    }
    KittyAgentsOutlineView *view = agents_collapsed || (!has_agents && has_editors && !editors_collapsed) ? editor_outline : outline;
    if (view == editor_outline && active_editor) {
        NSInteger editor_row = [editor_outline rowForItem:active_editor];
        if (editor_row >= 0) {
            [editor_outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)editor_row] byExtendingSelection:NO];
            [editor_outline scrollRowToVisible:editor_row];
        }
    }
    if (view.selectedRow < 0 && view.numberOfRows) [view selectRowIndexes:[NSIndexSet indexSetWithIndex:0] byExtendingSelection:NO];
    view.keyboardFocusRequested = YES;
    [self.window makeFirstResponder:view];
    [self setFocusBorderNeedsDisplay];
}
- (void)sendCommand:(NSString *)command windowID:(unsigned long long)window_id {
    if ([command isEqualToString:@"focus"] || [command isEqualToString:@"new_window"]) [self kittySidebarCancelNavigation];
    NSString *payload = [NSString stringWithFormat:@"%u:%llu:%@", os_window_id, window_id, command];
    set_cocoa_pending_action(AGENTS_SIDEBAR_MENU, payload.UTF8String);
}
- (void)activateNode:(id)node {
    if ([node isKindOfClass:[KittyAgentsSidebarEntry class]]) node = ((KittyAgentsSidebarEntry *)node)->identity;
    if ([node isKindOfClass:[NSString class]]) {
        [self setGroup:node expanded:![outline isItemExpanded:node] animated:YES];
    } else if ([node unsignedLongLongValue]) [self sendCommand:@"focus" windowID:[node unsignedLongLongValue]];
}
- (id)selectClickedRowInOutline:(NSOutlineView *)view {
    NSInteger row = view.clickedRow;
    if (row < 0) return nil;
    [view selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
    [self clearOtherOutlineSelection:view];
    [self setLinkSourceFromOutline:view];
    return [view itemAtRow:row];
}

- (void)outlineClicked:(id)sender {
    NSOutlineView *view = sender == editor_outline ? editor_outline : outline;
    id node = [self selectClickedRowInOutline:view];
    if (node) [self activateNode:node];
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
        KittyAgentsOutlineView *next = [self navigationOutline] == outline ? editor_outline : outline;
        if (!next.enclosingScrollView.hidden) {
            next.keyboardFocusRequested = YES;
            [self.window makeFirstResponder:next];
            if (next.selectedRow < 0 && next.numberOfRows) [next selectRowIndexes:[NSIndexSet indexSetWithIndex:0] byExtendingSelection:NO];
        } else [self kittySidebarCancelNavigation];
        return YES;
    }
    if (key == NSF10FunctionKey && (modifiers & NSEventModifierFlagShift)) { [self showActions:nil]; return YES; }
    KittyAgentsOutlineView *view = [self navigationOutline];
    NSScrollView *viewport = view.enclosingScrollView;
    NSInteger row = view.selectedRow;
    id node = [self selectedNode];
    switch (key) {
        case '\r': case NSEnterCharacter: [self activateNode:node]; return YES;
        case NSLeftArrowFunctionKey:
            if ([node isKindOfClass:[NSString class]]) [self setGroup:node expanded:NO animated:YES];
            else if (node) row = [view rowForItem:[view parentForItem:node]];
            break;
        case NSRightArrowFunctionKey:
            if ([node isKindOfClass:[NSString class]]) {
                if (![view isItemExpanded:node]) [self setGroup:node expanded:YES animated:YES];
                else if ([group_children[node] count]) row++;
            }
            break;
        case NSUpArrowFunctionKey: row = row < 0 ? 0 : row - 1; break;
        case NSDownArrowFunctionKey: row++; break;
        case NSHomeFunctionKey: row = 0; break;
        case NSEndFunctionKey: row = view.numberOfRows - 1; break;
        case NSPageUpFunctionKey: row -= MAX(1, (NSInteger)(viewport.contentSize.height / 48)); break;
        case NSPageDownFunctionKey: row += MAX(1, (NSInteger)(viewport.contentSize.height / 48)); break;
        default: return YES;
    }
    if (view.numberOfRows) {
        row = MAX(0, MIN(view.numberOfRows - 1, row));
        [view selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
        [view scrollRowToVisible:row];
    }
    return YES;
}
- (void)addMenuItemTo:(NSMenu *)menu title:(NSString *)title command:(const char *)command windowID:(unsigned long long)window_id {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:@selector(agentsSidebarMenuAction:) keyEquivalent:@""];
    item.target = self;
    item.tag = (NSInteger)window_id;
    item.representedObject = @(command);
    [menu addItem:item];
    [item release];
}

// Refreshes can arrive while a menu is tracking, so menu actions carry the
// agent's window id rather than the row index the menu was opened on.
- (NSMenu *)menuForNode:(id)node {
    BOOL editor = [node isKindOfClass:[KittyAgentsSidebarEntry class]];
    unsigned long long window_id = editor ? ((KittyAgentsSidebarEntry *)node)->identity.unsignedLongLongValue : ([node isKindOfClass:[NSNumber class]] ? [node unsignedLongLongValue] : 0);
    NSMenu *menu = [[[NSMenu alloc] initWithTitle:@"Agent and sidebar actions"] autorelease];
    if (window_id && (editor || entries_by_id[node])) {
        if (!editor) [self addMenuItemTo:menu title:@"Mark as Unread" command:"mark_unread" windowID:window_id];
        [self addMenuItemTo:menu title:@"New Window Here" command:"new_window" windowID:window_id];
        [self addMenuItemTo:menu title:@"Copy Working Directory" command:"copy_cwd" windowID:window_id];
        [menu addItem:[NSMenuItem separatorItem]];
        [self addMenuItemTo:menu title:editor ? @"Close Editor Window…" : @"Close Agent Window…" command:"close" windowID:window_id];
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
    NSOutlineView *view = [self navigationOutline];
    NSRect anchor = view.selectedRow >= 0 ? [view rectOfRow:view.selectedRow] : view.visibleRect;
    [menu popUpMenuPositioningItem:nil atLocation:NSMakePoint(NSMinX(anchor) + 12, NSMinY(anchor)) inView:view];
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
    if (NSPointInRect(p, [self dividerHitRect])) [[NSCursor resizeUpDownCursor] set];
    else if (resize_hovered) [[NSCursor resizeLeftRightCursor] set];
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

- (void)setRows:(const unsigned long long *)new_window_ids
         groups:(const char **)new_groups
      groupKeys:(const char **)new_group_keys
         agents:(const char **)new_agents
         labels:(const char **)new_labels
        details:(const char **)new_details
    projectKeys:(const char **)new_project_keys
         states:(const unsigned char *)new_states
          active:(const bool *)active
           count:(size_t)count
           kinds:(const char **)new_kinds {
    enum { content_changed = 1, tooltip_changed = 2, state_changed = 4, active_changed = 8 };
    BOOL structure_changed = !entries || entries.count != count;
    if (!structure_changed) {
        BOOL editor_structure_changed = NO;
        for (size_t i = 0; i < count; i++) {
            KittyAgentsSidebarEntry *entry = entries[i];
            BOOL editor = new_kinds[i] && strcmp(new_kinds[i], "editor") == 0;
            BOOL visible = new_window_ids[i] && ((editor && new_labels[i] && *new_labels[i]) || (!editor && new_agents[i] && *new_agents[i]));
            if (entry->identity.unsignedLongLongValue != new_window_ids[i] ||
                entry->editor != editor ||
                !agents_sidebar_string_matches(entry->group_key, new_group_keys[i]) ||
                (entry->identity.unsignedLongLongValue && (entry->editor ? entry->label.length : entry->agent.length)) != visible) {
                structure_changed = YES;
                break;
            }
            if (entry->editor && !agents_sidebar_string_matches(entry->label, new_labels[i])) editor_structure_changed = YES;
        }
        if (editor_structure_changed) structure_changed = YES;
    }
    NSMutableArray *next_entries = structure_changed ? [NSMutableArray arrayWithCapacity:count] : nil;
    NSMutableDictionary *next_by_id = structure_changed ? [NSMutableDictionary dictionaryWithCapacity:count] : nil;
    NSMutableSet *changed_groups = nil;
    BOOL outline_changed = NO;
    for (size_t i = 0; i < count; i++) {
        BOOL editor = new_kinds[i] && strcmp(new_kinds[i], "editor") == 0;
        NSDictionary *old_lookup = editor ? editors_by_id : entries_by_id;
        KittyAgentsSidebarEntry *entry = old_lookup[@(new_window_ids[i])];
        if (!entry) {
            entry = [[[KittyAgentsSidebarEntry alloc] init] autorelease];
            entry->identity = [[NSNumber alloc] initWithUnsignedLongLong:new_window_ids[i]];
        }
        entry->changes = row_style_changed ? content_changed : 0;
        if (entry->editor != editor) entry->changes |= content_changed;
        entry->editor = editor;
        if (agents_sidebar_update_string(&entry->group, new_groups[i]) && entry->group_key.length &&
            [[group_children[entry->group_key] firstObject] isEqual:entry->identity]) {
            if (!changed_groups) changed_groups = [NSMutableSet set];
            [changed_groups addObject:entry->group_key];
        }
        agents_sidebar_update_string(&entry->group_key, new_group_keys[i]);
        if (agents_sidebar_update_string(&entry->agent, new_agents[i])) entry->changes |= content_changed;
        if (agents_sidebar_update_string(&entry->label, new_labels[i])) entry->changes |= content_changed | tooltip_changed;
        if (agents_sidebar_update_string(&entry->detail, new_details[i])) entry->changes |= content_changed | tooltip_changed;
        agents_sidebar_update_string(&entry->project_key, new_project_keys[i]);
        if (!editor && entry->state != new_states[i]) entry->changes |= content_changed | state_changed;
        if (entry->active != (BOOL)active[i]) entry->changes |= content_changed | tooltip_changed | active_changed;
        entry->state = new_states[i];
        entry->active = active[i];
        if (structure_changed) {
            [next_entries addObject:entry];
            if (!entry->editor) next_by_id[entry->identity] = entry;
        }
    }
        if (structure_changed) {
        [entries release];
        entries = [next_entries copy];
        NSMutableDictionary *next_editors_by_id = [NSMutableDictionary dictionary];
        [entries_by_id release];
        for (KittyAgentsSidebarEntry *entry in entries) if (entry->editor) next_editors_by_id[entry->identity] = entry;
        [editors_by_id release]; editors_by_id = [next_editors_by_id copy];
        entries_by_id = [next_by_id copy];
        NSMutableArray *roots = [NSMutableArray array];
        NSMutableDictionary *children = [NSMutableDictionary dictionary];
        for (KittyAgentsSidebarEntry *entry in entries) {
            if (!entry->identity.unsignedLongLongValue || !entry->agent.length || entry->editor) continue;
            NSString *key = entry->group_key;
            if (key.length) {
                if (!children[key]) { children[key] = [NSMutableArray array]; [roots addObject:key]; }
                [children[key] addObject:entry->identity];
            } else [roots addObject:entry->identity];
        }
        outline_changed = ![root_nodes isEqualToArray:roots] || ![group_children isEqualToDictionary:children];
        if (outline_changed) {
            [root_nodes release];
            [group_children release];
            root_nodes = [roots copy];
            group_children = [children copy];
            [self rebuildOutline];
        }
        summary_dirty = YES;
        if (remembered_selection && !entries_by_id[remembered_selection] && !group_children[remembered_selection]) {
            [remembered_selection release];
            remembered_selection = nil;
        }
    }
    {
        [editor_outline enumerateAvailableRowViewsUsingBlock:^(NSTableRowView *rowView, NSInteger row) {
            KittyAgentsSidebarEntry *entry = editor_entries[(NSUInteger)row];
            if (!entry->changes) return;
            ((KittyAgentsSidebarRowView *)rowView).activeAgent = entry->active;
            [rowView setNeedsDisplay:YES];
            KittyAgentsSidebarCell *cell = (KittyAgentsSidebarCell *)[rowView viewAtColumn:0];
            cell.toolTip = [self labelForNode:entry]; [cell setNeedsDisplay:YES];
        }];
    }
    if (!outline_changed) {
        [self syncGroupExpansion];
        [outline enumerateAvailableRowViewsUsingBlock:^(NSTableRowView *row_view, NSInteger row) {
            id node = [outline itemAtRow:row];
        KittyAgentsSidebarEntry *entry = entries_by_id[node];
            unsigned changes = entry ? entry->changes : ([changed_groups containsObject:node] ? content_changed | tooltip_changed : 0);
            if (!changes) return;
            KittyAgentsSidebarCell *cell = (KittyAgentsSidebarCell *)[row_view viewAtColumn:0];
            if (changes & active_changed) {
                ((KittyAgentsSidebarRowView *)row_view).activeAgent = entry->active;
                [row_view setNeedsDisplay:YES];
            }
            if (changes & tooltip_changed) cell.toolTip = [self labelForNode:node];
            // Working uses the same semantic color for active/inactive rows.
            if (changes & state_changed) [cell updateSpinner];
            [cell setNeedsDisplay:YES];
        }];
    }
    if (summary_dirty || structure_changed) {
        NSMutableArray *next_editors = [NSMutableArray array];
        for (KittyAgentsSidebarEntry *entry in entries) if (entry->editor && entry->identity.unsignedLongLongValue && entry->label.length) [next_editors addObject:entry];
        if (![editor_entries isEqualToArray:next_editors]) {
            id selected = editor_outline.selectedRow >= 0 ? [[editor_outline itemAtRow:editor_outline.selectedRow] retain] : nil;
            NSPoint anchor = editor_scroll_view.contentView.bounds.origin;
            BOOL was_syncing = syncing_outline;
            syncing_outline = YES;
            [editor_entries release]; editor_entries = [next_editors copy];
            [editor_outline reloadData];
            NSInteger row = selected ? [editor_outline rowForItem:selected] : -1;
            if (row >= 0) [editor_outline selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
            [editor_scroll_view.contentView scrollToPoint:anchor];
            [editor_scroll_view reflectScrolledClipView:editor_scroll_view.contentView];
            syncing_outline = was_syncing;
            [selected release];
        }
        NSUInteger agent_count = 0;
        for (NSNumber *identity in entries_by_id) if (identity.unsignedLongLongValue) agent_count++;
        agents_header.title = [NSString stringWithFormat:@"Grouping: %@ · %lu agents · %lu groups", self.grouping ? self.grouping : @"tab",
            (unsigned long)agent_count, (unsigned long)group_children.count];
        agents_header.toolTip = agents_header.title;
        [self updateSectionHeaders];
        [self setFrameSize:self.frame.size];
        summary.toolTip = summary.stringValue;
        summary_dirty = NO;
    }
    [self updateRelatedLinkState];
    row_style_changed = NO;
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


- (NSColor *)workingSpinnerColorForNode:(id)node {
    if (!node || [node isKindOfClass:[NSString class]]) return nil;
    if ([node isKindOfClass:[KittyAgentsSidebarEntry class]]) return nil;
    KittyAgentsSidebarEntry *entry = entries_by_id[node];
    if (!entry || entry->state != 2) return nil;
    return [self stateColor:2 active:entry->active];
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

- (NSImage *)iconForEditor:(NSString *)editor textColor:(NSColor *)textColor {
    NSImage *icon = [self iconForAgent:editor textColor:textColor];
    if (!icon && ([editor isEqualToString:@"vi"] || [editor isEqualToString:@"view"]))
        icon = [self iconForAgent:@"vim" textColor:textColor];
    return icon;
}

- (NSString *)labelForNode:(id)node {
    if ([node isKindOfClass:[KittyAgentsSidebarEntry class]]) {
        KittyAgentsSidebarEntry *entry = node;
        NSString *ans = [NSString stringWithFormat:@"%@, %@%@", entry->label, entry->detail, entry->active ? @", current terminal" : @""];
        if (entry->related) return [ans stringByAppendingString:@", related to the selected row in the same project"];
        if (entry->linked) return [ans stringByAppendingString:@", has related rows in the same project"];
        return ans;
    }
    if ([node isKindOfClass:[NSString class]]) {
        NSNumber *first = [group_children[node] firstObject];
        KittyAgentsSidebarEntry *entry = entries_by_id[first];
        return entry ? entry->group : @"";
    }
    KittyAgentsSidebarEntry *entry = entries_by_id[node];
    if (!entry) return @"";
    NSString *ans = [NSString stringWithFormat:@"%@, %@%@", entry->label, entry->detail, entry->active ? @", current terminal" : @""];
    if (entry->related) return [ans stringByAppendingString:@", related to the selected row in the same project"];
    if (entry->linked) return [ans stringByAppendingString:@", has related rows in the same project"];
    return ans;
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
    KittyAgentsSidebarEntry *entry = [node isKindOfClass:[KittyAgentsSidebarEntry class]] ? node : entries_by_id[node];
    if (!entry) return;
    BOOL active = entry->active;
    NSColor *primary = active ? [NSColor alternateSelectedControlTextColor] : [NSColor controlTextColor];
    NSColor *secondary = active ? [NSColor alternateSelectedControlTextColor] : [NSColor secondaryLabelColor];
    BOOL linked = entry->related || entry->linked;
    CGFloat text_right = rect.size.width - 6;
    if (linked) {
        CGFloat badge_width = 28, badge_height = 16;
        NSRect badge = NSMakeRect(rect.size.width - 6 - badge_width, NSMidY(rect) - badge_height / 2, badge_width, badge_height);
        NSBezierPath *badge_path = [NSBezierPath bezierPathWithRoundedRect:badge xRadius:badge_height / 2 yRadius:badge_height / 2];
        [[[NSColor controlAccentColor] colorWithAlphaComponent:0.85] setFill];
        [badge_path fill];
        NSMutableParagraphStyle *badge_style = [[[NSMutableParagraphStyle alloc] init] autorelease];
        badge_style.alignment = NSTextAlignmentCenter;
        NSDictionary *badge_attrs = @{NSFontAttributeName: [NSFont boldSystemFontOfSize:11],
            NSForegroundColorAttributeName: [NSColor alternateSelectedControlTextColor], NSParagraphStyleAttributeName: badge_style};
        [@"↔" drawInRect:NSMakeRect(NSMinX(badge) + 2, NSMidY(badge) - 7, badge_width - 4, 16) withAttributes:badge_attrs];
        text_right = NSMinX(badge) - 8;
    }
    if (entry->editor) {
        NSImage *icon = [self iconForEditor:entry->agent textColor:primary];
        if (!icon) {
            icon = [NSImage imageWithSystemSymbolName:@"terminal" accessibilityDescription:@"Editor"];
            icon = [icon imageWithSymbolConfiguration:[NSImageSymbolConfiguration configurationWithHierarchicalColor:primary]];
        }
        if (icon && icon.size.width > 0 && icon.size.height > 0) {
            CGFloat scale = MIN(16 / icon.size.width, 16 / icon.size.height);
            NSSize size = NSMakeSize(icon.size.width * scale, icon.size.height * scale);
            [icon drawInRect:NSMakeRect(8 + (18 - size.width) / 2, NSMidY(rect) - size.height / 2, size.width, size.height)
                   fromRect:NSZeroRect operation:NSCompositingOperationSourceOver fraction:1 respectFlipped:YES hints:nil];
        }
        NSDictionary *attrs = @{NSFontAttributeName:[NSFont boldSystemFontOfSize:12], NSForegroundColorAttributeName:primary,
            NSParagraphStyleAttributeName:paragraph};
        [entry->label drawInRect:NSMakeRect(34, NSMidY(rect) - 16, MAX(0, text_right - 34), 17) withAttributes:attrs];
        [entry->detail drawInRect:NSMakeRect(34, NSMidY(rect) + 2, MAX(0, text_right - 34), 14) withAttributes:@{
            NSFontAttributeName:[NSFont systemFontOfSize:10], NSForegroundColorAttributeName:secondary,
            NSParagraphStyleAttributeName:paragraph}];
        return;
    }
    NSColor *state_color = [self stateColor:entry->state active:active];
    CGFloat center = NSMidY(rect), text_left = 20;
    if (entry->state != 2) {
        [state_color setFill];
        [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(5.5, center - 4, 8, 8)] fill];
    }
    NSImage *icon = [self iconForAgent:entry->agent textColor:primary];
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
    CGFloat width = MAX(0, text_right - text_left);
    [entry->label drawInRect:NSMakeRect(text_left, center - 16, width, 17) withAttributes:title_attrs];
    NSMutableAttributedString *detail = [[[NSMutableAttributedString alloc] initWithString:entry->detail attributes:detail_attrs] autorelease];
    NSRange separator = [entry->detail rangeOfString:@" · " options:NSBackwardsSearch];
    if (separator.location != NSNotFound) {
        NSUInteger start = NSMaxRange(separator);
        [detail addAttributes:@{NSForegroundColorAttributeName:state_color, NSFontAttributeName:[NSFont boldSystemFontOfSize:10.5]}
                        range:NSMakeRange(start, entry->detail.length - start)];
    }
    [detail drawInRect:NSMakeRect(text_left, center + 2, width, 14)];
}
- (void)drawRect:(NSRect)dirtyRect {
    (void)dirtyRect;
    // The scroll views and outline views are transparent; paint only their section
    // content bounds so the native sidebar material remains visible elsewhere.
    [KittySidebarSectionBackgroundColor(YES) setFill];
    if (!scroll_view.hidden) NSRectFill(scroll_view.frame);
    [KittySidebarSectionBackgroundColor(NO) setFill];
    if (!editor_scroll_view.hidden) NSRectFill(editor_scroll_view.frame);
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
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    if (resize_tracking_area) [self removeTrackingArea:resize_tracking_area];
    [resize_tracking_area release];
    outline.delegate = nil;
    outline.dataSource = nil;
    outline.target = nil;
    editor_outline.delegate = nil;
    editor_outline.dataSource = nil;
    editor_outline.target = nil;
    [entries release];
    [entries_by_id release];
    [editors_by_id release];
    [root_nodes release];
    [group_children release];
    [editor_entries release]; [agents_header release]; [editors_header release];
    [editor_outline release]; [editor_scroll_view release]; [section_divider release]; [divider_hit_view release];
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
    const char **project_keys,
    const unsigned char *states,
    const bool *active,
    const char **collapsed,
    size_t collapsed_count,
    size_t count,
    const char **kinds) {
    NSWindow *window = (NSWindow *)ptr;
    if (!window || !window.contentView) return;
    [KittyAgentsSidebarView setSharedCollapsedGroups:collapsed count:collapsed_count];
    NSButton *toggle_button = agents_sidebar_toggle_button(window);
    toggle_button.hidden = NO;
    agents_sidebar_update_toggle_state(toggle_button, enabled);
    if (!enabled) {
        KittyAgentsSidebarView *sidebar = objc_getAssociatedObject(window, kitty_agents_sidebar_key);
        [sidebar kittySidebarCancelNavigation];
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
    if (!NSEqualRects(sidebar.frame, frame)) [sidebar setFrame:frame];
    if (!NSEqualRects([sidebar backgroundView].frame, frame)) [[sidebar backgroundView] setFrame:frame];
    if (!agents_sidebar_string_matches(sidebar.grouping, grouping)) sidebar.grouping = [NSString stringWithUTF8String:grouping];
    if (!agents_sidebar_string_matches(sidebar.iconsDir, icons_dir)) sidebar.iconsDir = icons_dir ? [NSString stringWithUTF8String:icons_dir] : nil;
    [sidebar setRows:window_ids groups:groups groupKeys:group_keys agents:agents labels:labels details:details projectKeys:project_keys states:states active:active count:count kinds:kinds];
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
static monotonic_t last_notification_response_at = 0;
static struct {
    pid_t pid;
    monotonic_t at;
} last_other_instance_activation = {0};

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
    last_notification_response_at = monotonic();
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
add_user_global_menu_entries(NSMenu *bar) {
    if (OPT(global_menu.entries)) {
        for (size_t i = 0; i < OPT(global_menu.count); i++) {
            struct MenuItem *e = OPT(global_menu.entries) + i;
            if (e->definition && e->location && e->location_count > 1) { add_user_global_menu_entry(e, bar, i); }
        }
    }
}

static void
remove_user_global_menu_entries(NSMenu *menu) {
    for (NSInteger i = [menu numberOfItems] - 1; i >= 0; i--) {
        NSMenuItem *item = [menu itemAtIndex:i];
        if ([item isKindOfClass:[UserMenuItem class]]) [menu removeItem:item];
        else if ([item hasSubmenu]) remove_user_global_menu_entries([item submenu]);
    }
}

static GlobalShortcut *
global_shortcut_for_action(GlobalShortcuts *shortcuts, SEL action) {
#define Q(x) \
    if (action == @selector(x:)) return &shortcuts->x;
    Q(new_os_window);
    Q(close_os_window);
    Q(close_tab);
    Q(edit_config_file);
    Q(reload_config);
    Q(previous_tab);
    Q(next_tab);
    Q(new_tab);
    Q(new_window);
    Q(close_window);
    Q(reset_terminal);
    Q(clear_terminal_and_scrollback);
    Q(clear_screen);
    Q(clear_scrollback);
    Q(clear_last_command);
    Q(toggle_macos_secure_keyboard_entry);
    Q(toggle_fullscreen);
    Q(open_kitty_website);
    Q(hide_macos_app);
    Q(hide_macos_other_apps);
    Q(minimize_macos_window);
    Q(quit);
    Q(search_scrollback);
    Q(macos_cycle_through_os_windows);
    Q(macos_cycle_through_os_windows_backwards);
    Q(paste_from_clipboard);
    Q(copy_or_noop);
#undef Q
    return NULL;
}

// The menu items we create that have global shortcuts and the shortcuts
// that were applied to them.
static NSMutableArray<NSMenuItem *> *global_shortcut_menu_items = nil;
static GlobalShortcuts applied_global_shortcuts;

static bool
menu_item_has_shortcut(NSMenuItem *item, const GlobalShortcut *gs) {
    return [item.keyEquivalent isEqualToString:@(gs->key)] && item.keyEquivalentModifierMask == gs->mods;
}

static void
update_global_shortcuts_in_menu(void) {
    // Cocoa modifies some of our menu items, for example, it gives Enter Full
    // Screen the Globe+F shortcut and moves our shortcut to a hidden copy of the
    // item. So only change shortcuts that were actually changed in the config
    // and change them on whichever item has the old shortcut, see
    // https://github.com/kovidgoyal/kitty/issues/10793
    for (NSMenuItem *item in global_shortcut_menu_items) {
        GlobalShortcut *gs = global_shortcut_for_action(&global_shortcuts, item.action);
        GlobalShortcut *old = global_shortcut_for_action(&applied_global_shortcuts, item.action);
        if (!gs || !old || (strcmp(gs->key, old->key) == 0 && gs->mods == old->mods)) continue;
        NSMenuItem *target_item = item;
        if (!menu_item_has_shortcut(item, old)) {
            for (NSMenuItem *q in [item.menu itemArray]) {
                if (q != item && q.action == item.action && q.target == item.target && menu_item_has_shortcut(q, old)) {
                    target_item = q;
                    break;
                }
            }
        }
        [target_item setKeyEquivalent:@(gs->key)];
        [target_item setKeyEquivalentModifierMask:gs->mods];
    }
    applied_global_shortcuts = global_shortcuts;
}

static void
cocoa_create_global_menu(void) {
    NSString *app_name = find_app_name();
    NSMenu *bar = [[NSMenu alloc] init];
    GlobalMenuTarget *global_menu_target = [GlobalMenuTarget shared_instance];
    [NSApp setMainMenu:bar];
    if (global_shortcut_menu_items) [global_shortcut_menu_items removeAllObjects];
    else global_shortcut_menu_items = [[NSMutableArray alloc] init];

#define MENU_ITEM(menu, title, name)                                                                                         \
    {                                                                                                                        \
        NSMenuItem *__mi = [menu addItemWithTitle:title action:@selector(name:) keyEquivalent:@(global_shortcuts.name.key)]; \
        [__mi setKeyEquivalentModifierMask:global_shortcuts.name.mods];                                                      \
        [__mi setTarget:global_menu_target];                                                                                 \
        [global_shortcut_menu_items addObject:__mi];                                                                         \
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

    add_user_global_menu_entries(bar);
    [bar release];
    applied_global_shortcuts = global_shortcuts;


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
    // Update the existing menu in place rather than replacing it, as replacing
    // the main menu and windows menu breaks the Globe key shortcuts that
    // Cocoa injects into the windows menu, see https://github.com/kovidgoyal/kitty/issues/10793
    NSMenu *bar = [NSApp mainMenu];
    if (bar) {
        update_global_shortcuts_in_menu();
        remove_user_global_menu_entries(bar);
        add_user_global_menu_entries(bar);
    } else cocoa_create_global_menu();
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
    [[[NSWorkspace sharedWorkspace] notificationCenter] addObserverForName:NSWorkspaceDidActivateApplicationNotification
                                                                    object:nil
                                                                     queue:[NSOperationQueue mainQueue]
                                                                usingBlock:^(NSNotification *note) {
                                                                  NSRunningApplication *app = note.userInfo[NSWorkspaceApplicationKey];
                                                                  // LaunchServices activates us before sending the reopen event, so ignore our own activation
                                                                  if (!app || app.processIdentifier == getpid()) return;
                                                                  NSString *our_id = [[NSBundle mainBundle] bundleIdentifier];
                                                                  if (our_id && [app.bundleIdentifier isEqualToString:our_id]) {
                                                                      last_other_instance_activation.pid = app.processIdentifier;
                                                                      last_other_instance_activation.at = monotonic();
                                                                  } else {
                                                                      last_other_instance_activation.pid = 0;
                                                                  }
                                                                }];
    return true;
}

bool
cocoa_yield_reopen_to_other_instance(void) {
    // When a notification is clicked, macOS delivers the response to the kitty
    // instance that posted it, which brings itself forward. Then usernoted asks
    // LaunchServices to bring the application forward by bundle URL, which
    // picks an arbitrary running instance and sends it a reopen event, stealing
    // focus. Detect that and hand activation back to the posting instance.
    NSAppleEventDescriptor *event = [[NSAppleEventManager sharedAppleEventManager] currentAppleEvent];
    if (!event) return false;
    NSData *token = [[event attributeDescriptorForKeyword:keySenderAuditTokenAttr] data];
    if (!token || token.length != sizeof(audit_token_t)) return false;
    audit_token_t t;
    memcpy(&t, token.bytes, sizeof(t));
    char name[256] = {0};
    // val[5] is the pid, see audit_token_to_pid() in libbsm
    if (proc_name((pid_t)t.val[5], name, sizeof(name)) <= 0 || strcmp(name, "usernoted") != 0) return false;
    const monotonic_t now = monotonic(), threshold = s_double_to_monotonic_t(2.0);
    // We received the notification response so this reopen is for us
    if (last_notification_response_at && now - last_notification_response_at < threshold) return false;
    if (!last_other_instance_activation.pid || now - last_other_instance_activation.at > threshold) return false;
    NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:last_other_instance_activation.pid];
    if (!app || app.terminated) return false;
    debug_rendering("Yielding activation to kitty instance: %d that owns the clicked notification\n", app.processIdentifier);
    // yieldActivationToApplication was introduced in macOS 14 (Sonoma)
    SEL selector = NSSelectorFromString(@"yieldActivationToApplication:");
    if ([NSApp respondsToSelector:selector]) {
        [NSApp performSelector:selector withObject:app];
        [app activateWithOptions:0];
    } else {
#define NSApplicationActivateIgnoringOtherApps 2
        [app activateWithOptions:NSApplicationActivateIgnoringOtherApps];
#undef NSApplicationActivateIgnoringOtherApps
    }
    return true;
}
