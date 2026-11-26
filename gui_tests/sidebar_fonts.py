#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import ctypes as C
import unittest

from kitty.constants import is_macos

from kitty_tests.base import BaseTest


class TestSidebarFonts(BaseTest):
    @unittest.skipUnless(is_macos, 'Native sidebar fonts require macOS')
    def test_native_sidebar_font_selection(self):
        from kitty import fast_data_types

        del fast_data_types
        C.CDLL('/System/Library/Frameworks/Cocoa.framework/Cocoa')
        objc = C.CDLL('/usr/lib/libobjc.A.dylib')
        ptr = C.c_void_p
        objc.objc_getClass.argtypes = [C.c_char_p]
        objc.objc_getClass.restype = ptr
        objc.sel_registerName.argtypes = [C.c_char_p]
        objc.sel_registerName.restype = ptr

        def cls(name):
            return objc.objc_getClass(name.encode())

        def send(obj, selector, result=ptr, types=(), args=()):
            return C.CFUNCTYPE(result, ptr, ptr, *types)(('objc_msgSend', objc))(
                obj, objc.sel_registerName(selector.encode()), *args)

        def string(value):
            return send(cls('NSString'), 'stringWithUTF8String:', types=(C.c_char_p,), args=(value.encode(),))

        def text(value):
            return send(value, 'UTF8String', C.c_char_p).decode()

        send(cls('NSApplication'), 'sharedApplication')
        sidebar = send(cls('KittyAgentsSidebarView'), 'alloc')
        sidebar = send(sidebar, 'initWithOSWindowID:width:cellWidth:', types=(C.c_uint, C.c_double, C.c_uint), args=(0, 240, 8))
        self.assertTrue(sidebar)
        self.addCleanup(send, sidebar, 'release', None)
        send(sidebar, 'setFontFamily:', None, (ptr,), (string('system'),))
        self.addCleanup(send, sidebar, 'setFontFamily:', None, (ptr,), (string('system'),))

        def font(bold=False):
            return send(sidebar, 'sidebarFontOfSize:bold:', types=(C.c_double, C.c_bool), args=(12, bold))

        system = send(cls('NSFont'), 'systemFontOfSize:', types=(C.c_double,), args=(12,))
        self.ae(text(send(font(), 'fontName')), text(send(system, 'fontName')))
        send(sidebar, 'setFontFamily:', None, (ptr,), (string('Menlo'),))
        self.ae(text(send(font(), 'familyName')), 'Menlo')
        self.ae(text(send(font(True), 'familyName')), 'Menlo')
        self.ae(send(font(), 'pointSize', C.c_double), 12)
        send(sidebar, 'setFontFamily:', None, (ptr,), (string('Menlo-Regular'),))
        self.ae(text(send(font(), 'familyName')), 'Menlo')
        for name in ('Missing-Sidebar-Font-123456', '', 'system'):
            send(sidebar, 'setFontFamily:', None, (ptr,), (string(name),))
            self.ae(text(send(font(), 'fontName')), text(send(system, 'fontName')))
