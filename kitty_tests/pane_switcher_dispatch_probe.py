#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>


def main():
    import ctypes as C
    import json
    import os
    import sys
    import traceback
    from pathlib import Path

    out = Path(sys.argv[1]).resolve()
    out.mkdir(exist_ok=True)
    for key, part in [('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')]:
        os.environ[key] = str(out / part)
        (out / part).mkdir(exist_ok=True)

    from kitty.boss import Boss
    from kitty.fast_data_types import add_timer
    from kitty.main import main

    objc = C.CDLL('/usr/lib/libobjc.A.dylib')
    p = C.c_void_p
    objc.objc_getClass.argtypes = [C.c_char_p]
    objc.objc_getClass.restype = p
    objc.sel_registerName.argtypes = [C.c_char_p]
    objc.sel_registerName.restype = p
    class Point(C.Structure):
        _fields_ = [('x', C.c_double), ('y', C.c_double)]

    def cls(name): return objc.objc_getClass(name.encode())
    def send(obj, selector, result=p, types=(), args=()):
        return C.PYFUNCTYPE(result, p, p, *types)(('objc_msgSend', objc))(obj, objc.sel_registerName(selector.encode()), *args)
    def string(s): return send(cls('NSString'), 'stringWithUTF8String:', types=(C.c_char_p,), args=(s.encode(),))
    def find(view):
        if send(view, 'isKindOfClass:', C.c_bool, (p,), (cls('KittyPaneSwitcher'),)): return view
        children = send(view, 'subviews')
        for i in range(send(children, 'count', C.c_ulong)):
            if found := find(send(children, 'objectAtIndex:', types=(C.c_ulong,), args=(i,))): return found
        return None

    def event(kind, flags, code, chars=''):
        return send(cls('NSEvent'), 'keyEventWithType:location:modifierFlags:timestamp:windowNumber:context:characters:charactersIgnoringModifiers:isARepeat:keyCode:',
            types=(C.c_ulong, Point, C.c_ulong, C.c_double, C.c_long, p, p, p, C.c_bool, C.c_ushort),
            args=(kind, Point(0,0), flags, 0, send(state['window'], 'windowNumber', C.c_long), None, string(chars), string(chars), False, code))
    state = {}
    original_start = Boss.start

    def later(fn, delay=.2):
        def callback(timer):
            try: fn()
            except BaseException:
                state['error'] = traceback.format_exc()
                print(state['error'], flush=True)
                state['boss'].quit()
        add_timer(callback, delay, False)

    def start(boss, *args, **kwargs):
        original_start(boss, *args, **kwargs)
        state['boss'] = boss
        later(begin, 1)
    Boss.start = start

    def begin():
        app = send(cls('NSApplication'), 'sharedApplication')
        window = send(send(app, 'windows'), 'firstObject')
        state.update(window=window, tab=state['boss'].active_tab)
        # Isolate OS focus ownership only. Real GLFW keyDown, global flags monitor,
        # production switcher open/close and Boss focus commit are all exercised.
        objc.class_getInstanceMethod.argtypes = [p,p]
        objc.class_getInstanceMethod.restype = p
        objc.method_setImplementation.argtypes = [p,p]
        objc.method_setImplementation.restype = p
        state['key'] = C.CFUNCTYPE(C.c_bool,p,p)(lambda obj, sel: True)
        method = objc.class_getInstanceMethod(cls('NSWindow'), objc.sel_registerName(b'isKeyWindow'))
        state['key_method'] = method
        original = objc.method_setImplementation(method, state['key'])
        objc.class_getClassMethod.argtypes = [p,p]
        objc.class_getClassMethod.restype = p
        state['physical_flags'] = 1 << 20
        state['flags_query'] = C.CFUNCTYPE(C.c_ulong,p,p)(lambda obj, sel: state['physical_flags'])
        flags_method = objc.class_getClassMethod(cls('NSEvent'), objc.sel_registerName(b'modifierFlags'))
        state['flags_original'] = objc.method_setImplementation(flags_method, state['flags_query'])
        content = send(window, 'contentView')
        send(window, 'makeFirstResponder:', C.c_bool, (p,), (content,))
        tab = state['tab']
        for iteration in range(24):
            before = tab.active_window.id
            state['physical_flags'] = 1 << 20
            send(content, 'flagsChanged:', None, (p,), (event(12, 1<<20, 55),))
            send(content, 'keyDown:', None, (p,), (event(10, 1<<20, 30, ']'),))
            q = find(content)
            if not q: raise RuntimeError('actual configured shortcut failed to open switcher')
            release = send(send(q, 'valueForKey:', types=(p,), args=(string('release_modifiers'),)), 'unsignedLongLongValue', C.c_ulonglong)
            if release != 1 << 20: raise RuntimeError('originating physical modifiers were lost')
            if tab.active_window.id != before: raise RuntimeError('opening changed focus prematurely')
            # Force the real GLFW global monitor path, as local monitor ordering is undefined.
            monitor = send(q, 'valueForKey:', types=(p,), args=(string('event_monitor'),))
            send(cls('NSEvent'), 'removeMonitor:', None, (p,), (monitor,))
            objc.class_getInstanceVariable.argtypes = [p,C.c_char_p]
            objc.class_getInstanceVariable.restype = p
            objc.ivar_getOffset.argtypes = [p]
            objc.ivar_getOffset.restype = C.c_long
            ivar = objc.class_getInstanceVariable(cls('KittyPaneSwitcher'), b'event_monitor')
            p.from_address(q + objc.ivar_getOffset(ivar)).value = None
            if iteration % 2:
                send(content, 'keyDown:', None, (p,), (event(10, 1<<20, 30, ']'),))
            selected = send(send(q, 'valueForKey:', types=(p,), args=(string('selected'),)), 'unsignedLongLongValue', C.c_ulonglong)
            entries = send(q, 'valueForKey:', types=(p,), args=(string('entries'),))
            entry = send(entries, 'objectAtIndex:', types=(C.c_ulong,), args=(selected,))
            target = send(send(entry, 'valueForKey:', types=(p,), args=(string('pane_id'),)), 'unsignedLongLongValue', C.c_ulonglong)
            state['physical_flags'] = 0
            send(app, 'sendEvent:', None, (p,), (event(12, 0, 55),))
            if find(content) or tab.active_window.id != target:
                raise RuntimeError(f'iteration {iteration}: modifier release failed to close and select')
        state['physical_flags'] = 1 << 20
        send(content, 'keyDown:', None, (p,), (event(10, 1<<20, 30, ']'),))
        q = find(content)
        if not q: raise RuntimeError('fast release scenario did not open')
        selected = send(send(q, 'valueForKey:', types=(p,), args=(string('selected'),)), 'unsignedLongLongValue', C.c_ulonglong)
        entries = send(q, 'valueForKey:', types=(p,), args=(string('entries'),))
        entry = send(entries, 'objectAtIndex:', types=(C.c_ulong,), args=(selected,))
        target = send(send(entry, 'valueForKey:', types=(p,), args=(string('pane_id'),)), 'unsignedLongLongValue', C.c_ulonglong)
        state['physical_flags'] = 0
        def check_fast_release():
            if find(content) or tab.active_window.id != target:
                raise RuntimeError('release before monitor delivery left switcher open')
            objc.method_setImplementation(method, original)
            objc.method_setImplementation(flags_method, state['flags_original'])
            print('PASS 24 actual shortcut cycles and fast release: physical capture, repeated selection, global monitor release, removal and focus commit', flush=True)
            state['boss'].quit()
        later(check_fast_release, .05)

    (out / 'session.conf').write_text('layout stack\nlaunch --title ONE /bin/cat\nlaunch --title TWO /bin/cat\n')
    sys.argv = ['kitty', '--config', 'NONE', '-o', 'persist_session=no', '-o', 'restore_session_after_logout=no', '-o', 'confirm_os_window_close=0',
                '-o', 'map=cmd+0x1e next_window', '--session', str(out / 'session.conf')]
    getattr(sys, 'kitty_run_data', {}).pop('cli_flags', None)
    main()
    if 'error' in state: raise SystemExit(1)


if __name__ == '__main__':
    main()
