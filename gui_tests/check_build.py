#!/usr/bin/env python
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import shutil
import subprocess
import tempfile
import textwrap

from kitty_tests.base import BaseTest


class TestBuild(BaseTest):
    def test_macos_dictation_forwarding(self) -> None:
        from kitty.constants import glfw_path, is_macos

        if not is_macos or not shutil.which('clang'):
            self.skipTest('Dictation smoke test is macOS only and requires clang')
        cocoa_module = glfw_path('cocoa')
        probe = textwrap.dedent("""\
            #import <AppKit/AppKit.h>
            #import <dlfcn.h>
            #import <objc/runtime.h>
            #import <objc/message.h>

            static int start_calls = 0;
            static int stop_calls = 0;
            static id last_sender = nil;

            static void fake_start_dictation(id self, SEL _cmd, id sender) {
                (void)self; (void)_cmd;
                start_calls++;
                last_sender = sender;
            }

            static void fake_stop_dictation(id self, SEL _cmd, id sender) {
                (void)self; (void)_cmd;
                stop_calls++;
                last_sender = sender;
            }

            static void require_true(BOOL condition, const char *message) {
                if (!condition) {
                    fprintf(stderr, "FAIL: %s\\n", message);
                    exit(1);
                }
            }

            int main(void) {
                @autoreleasepool {
                    [[NSApplication sharedApplication] setActivationPolicy:NSApplicationActivationPolicyProhibited];
                    void *handle = dlopen(@@COCOA_MODULE@@, RTLD_NOW | RTLD_GLOBAL);
                    require_true(handle != NULL, dlerror());

                    SEL start = NSSelectorFromString(@"startDictation:");
                    SEL stop = NSSelectorFromString(@"stopDictation:");
                    Method start_method = class_getInstanceMethod([NSApplication class], start);
                    Method stop_method = class_getInstanceMethod([NSApplication class], stop);
                    require_true(start_method != NULL, "NSApplication startDictation: missing");
                    require_true(stop_method != NULL, "NSApplication stopDictation: missing");
                    method_setImplementation(start_method, (IMP)fake_start_dictation);
                    method_setImplementation(stop_method, (IMP)fake_stop_dictation);

                    Class view_cls = NSClassFromString(@"GLFWContentView");
                    Class context_cls = NSClassFromString(@"GLFWTextInputContext");
                    require_true(view_cls != Nil, "GLFWContentView class not loaded");
                    require_true(context_cls != Nil, "GLFWTextInputContext class not loaded");

                    SEL init_with_glfw_window = NSSelectorFromString(@"initWithGlfwWindow:");
                    id view = ((id (*)(id, SEL, void *)) objc_msgSend)([view_cls alloc], init_with_glfw_window, NULL);
                    require_true(view != nil, "GLFWContentView initWithGlfwWindow: failed");
                    require_true([view respondsToSelector:start], "GLFWContentView does not expose startDictation:");
                    require_true([view respondsToSelector:stop], "GLFWContentView does not expose stopDictation:");

            #pragma clang diagnostic push
            #pragma clang diagnostic ignored "-Warc-performSelector-leaks"
                    [view performSelector:start withObject:@"menu sender"];
            #pragma clang diagnostic pop
                    require_true(start_calls == 1, "startDictation: action was not forwarded to NSApplication");
                    require_true([(id)last_sender isEqual:@"menu sender"], "startDictation: forwarded wrong sender");

                    [view doCommandBySelector:start];
                    require_true(start_calls == 2, "doCommandBySelector:startDictation: was swallowed");
                    require_true(last_sender == view, "doCommandBySelector:startDictation: should forward self as sender");

                    id context = [view inputContext];
                    require_true(context != nil, "GLFWContentView inputContext missing");
                    require_true([context isKindOfClass:context_cls], "GLFWContentView inputContext has wrong class");
                    [context doCommandBySelector:stop];
                    require_true(stop_calls == 1, "GLFWTextInputContext did not forward stopDictation:");
                    require_true(last_sender == nil, "GLFWTextInputContext should forward nil sender");

                    printf("dictation forwarding probe passed\\n");
                }
                return 0;
            }
        """).replace('@@COCOA_MODULE@@', json.dumps(cocoa_module))
        with tempfile.TemporaryDirectory() as tdir:
            src = os.path.join(tdir, 'dictation_probe.m')
            exe = os.path.join(tdir, 'dictation_probe')
            with open(src, 'w') as f:
                f.write(probe)
            nm = subprocess.run(['nm', '-g', cocoa_module], capture_output=True, text=True)
            asan_flags = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer'] if '__asan_init' in nm.stdout else []
            cp = subprocess.run(['clang', '-framework', 'AppKit'] + asan_flags + [src, '-o', exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            cp = subprocess.run([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn('dictation forwarding probe passed', cp.stdout)
