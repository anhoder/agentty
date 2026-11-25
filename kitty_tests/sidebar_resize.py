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


class TestSidebarResizeInteraction(BaseTest):
    def test_native_drag_previews_without_committing_until_release(self):
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = (root / 'kitty' / 'cocoa_window.m').read_text()
        drag_start = source.index('- (void)mouseDragged:(NSEvent *)event {', source.index('@implementation KittyAgentsSidebarView'))
        drag_end = source.index('- (void)mouseUp:(NSEvent *)event {', drag_start)
        drag = source[drag_start:drag_end]
        self.assertIn('resize_preview_width = width', drag)
        self.assertIn('resize_indicator', drag)
        self.assertNotIn('set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE', drag)
        self.assertNotIn('[self previewSizeWidth:', drag)
        release_end = source.index('- (NSString *)grouping', drag_end)
        release = source[drag_end:release_end]
        self.assertIn('kitty_agents_sidebar_resize_should_commit', release)
        self.assertIn('set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE', release)
        self.assertIn('[resize_indicator removeFromSuperview]', release)
        self.assertIn('if (dragging_divider)', release)
        self.assertIn('resizePending = YES', release)
        self.assertIn('[self cancelWidthResize]; [self kittySidebarCancelNavigation]', source)
        self.assertIn('[resize_indicator release]', source)
        self.assertIn('if (resizing && event.keyCode == 53)', source)
        self.assertIn('[self updateResizePreviewForEvent:event]', release)
        self.assertIn('resize_pointer_offset = resize_start_width - pointer.x', source)
        self.assertIn('p.x + resize_pointer_offset', source)
        self.assertNotIn('if (resize_start_width == self.frame.size.width', source)

    def test_resize_production_math_helper(self):
        if not shutil.which('clang'):
            self.skipTest('Native resize helper probe requires clang')
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = r'''
#include <assert.h>
#include "kitty/agents_sidebar_resize.h"
int main(void) {
    unsigned start = kitty_agents_sidebar_resize_columns(260, 2, 8, 1000);
    assert(start == 65);
    unsigned moved = kitty_agents_sidebar_resize_columns(400, 2, 8, 1000);
    assert(moved == 100);
    assert(!kitty_agents_sidebar_resize_should_commit(start, start));
    assert(kitty_agents_sidebar_resize_should_commit(start, moved));
    unsigned returned = kitty_agents_sidebar_resize_columns(260, 2, 8, 1000);
    assert(!kitty_agents_sidebar_resize_should_commit(start, returned));
    // A stationary pointer grabbed at the 8pt rail preserves the start width.
    unsigned click = kitty_agents_sidebar_resize_columns(260, 2, 8, 1000);
    assert(click == start);
    assert(!kitty_agents_sidebar_resize_should_commit(start, click));
    assert(kitty_agents_sidebar_resize_columns(-100, 2, 8, 1000) == 16);
    assert(kitty_agents_sidebar_resize_columns(5000, 2, 8, 1000) == 188);
    return 0;
}
'''
        build = root / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='sidebar-resize-', dir=build) as directory:
            probe = Path(directory) / 'probe.c'
            executable = Path(directory) / 'probe'
            probe.write_text(source)
            process = subprocess.run(['clang', '-O2', '-I', str(root), str(probe), '-lm', '-o', str(executable)], capture_output=True, text=True)
            self.ae(process.returncode, 0, process.stdout + process.stderr)
            process = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
            self.ae(process.returncode, 0, process.stdout + process.stderr)


class TestSidebarResizeQueue(BaseTest):
    def test_drag_widths_coalesce_without_reordering_actions(self):
        if not shutil.which('clang'):
            self.skipTest('Native action queue probe requires clang')
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        source = (root / 'kitty' / 'child-monitor.c').read_text()
        start = source.index('void\nset_cocoa_pending_action(CocoaPendingAction action, const char *data)')
        end = source.index('\nstatic void\nprocess_cocoa_pending_actions', start)
        prelude = r'''
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
typedef enum {LAUNCH_URLS, COCOA_NOTIFICATION_UNTRACKED, SEARCH_EVENT,
    AGENTS_SIDEBAR_CLICK, AGENTS_SIDEBAR_MENU, AGENTS_SIDEBAR_RESIZE,
    AGENTS_SIDEBAR_GROUP_TOGGLE, NUM_COCOA_PENDING_ACTIONS} CocoaPendingAction;
static bool cocoa_pending_actions[NUM_COCOA_PENDING_ACTIONS], has_cocoa_pending_actions;
static struct {
    char *wd;
    struct {char **items; size_t count,capacity;} open_urls,untracked_notifications,search_events;
    struct {struct {CocoaPendingAction action; char *data;} *items; size_t count,capacity;} sidebar_actions;
} cocoa_pending_actions_data;
#define ensure_space_for(obj,field,type,needed,capacity,chunk,zero) do { \
    if ((obj)->capacity < (needed)) { (obj)->capacity=(needed)+(chunk); \
    (obj)->field=realloc((obj)->field,(obj)->capacity*sizeof(type)); } } while(0)
static void cocoa_append_to_pending_list(void *array,const char *data) {(void)array;(void)data;}
static void wakeup_main_loop(void) {}
'''
        scenario = r'''
static void require(bool value,const char *message) {
    if (!value) {fprintf(stderr,"%s\n",message);exit(1);}
}
static void expect(size_t index,CocoaPendingAction action,const char *data) {
    require(index<cocoa_pending_actions_data.sidebar_actions.count,"missing queued action");
    require(cocoa_pending_actions_data.sidebar_actions.items[index].action==action,"reordered action type");
    require(!strcmp(cocoa_pending_actions_data.sidebar_actions.items[index].data,data),"wrong final width or action payload");
}
int main(void) {
    char data[64];
    for (unsigned i=0;i<200;i++) {
        snprintf(data,sizeof(data),"1:%u",30+i);
        set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,data);
    }
    require(cocoa_pending_actions_data.sidebar_actions.count==1,"drag burst causes redundant layouts");
    expect(0,AGENTS_SIDEBAR_RESIZE,"1:229");
    set_cocoa_pending_action(AGENTS_SIDEBAR_MENU,"1:7:focus");
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,"1:40");
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,"1:41");
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,"10:60");
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,"10:61");
    set_cocoa_pending_action(AGENTS_SIDEBAR_GROUP_TOGGLE,"1:1:group");
    set_cocoa_pending_action(AGENTS_SIDEBAR_RESIZE,"1:41");
    require(cocoa_pending_actions_data.sidebar_actions.count==6,"lost action barrier or coalescing boundary");
    expect(1,AGENTS_SIDEBAR_MENU,"1:7:focus");
    expect(2,AGENTS_SIDEBAR_RESIZE,"1:41");
    expect(3,AGENTS_SIDEBAR_RESIZE,"10:61");
    expect(4,AGENTS_SIDEBAR_GROUP_TOGGLE,"1:1:group");
    expect(5,AGENTS_SIDEBAR_RESIZE,"1:41");
    for(size_t i=0;i<cocoa_pending_actions_data.sidebar_actions.count;i++)
        free(cocoa_pending_actions_data.sidebar_actions.items[i].data);
    free(cocoa_pending_actions_data.sidebar_actions.items);
    puts("sidebar resize queue passed");
    return 0;
}
'''
        build = root / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='sidebar-queue-', dir=build) as directory:
            probe = Path(directory) / 'probe.c'
            executable = Path(directory) / 'probe'
            probe.write_text(prelude + source[start:end] + scenario)
            process = subprocess.run(['clang', '-O2', str(probe), '-o', str(executable)], capture_output=True, text=True)
            self.ae(process.returncode, 0, process.stdout + process.stderr)
            process = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
            self.ae(process.returncode, 0, process.stdout + process.stderr)
