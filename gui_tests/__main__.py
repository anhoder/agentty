#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import argparse
import unittest


MODULES = ('sidebar_sections', 'pane_switcher', 'sidebar_fonts', 'single_instance', 'check_build')


def main():
    parser = argparse.ArgumentParser(
        description='Run real-application tests only with explicit user authorization '
        'and an isolated configuration/session (including native preferences).',
    )
    parser.add_argument(
        '--allow-real-app', action='store_true',
        help='permit real application/window creation; explicit user authorization '
        'and isolation are still required before using this flag',
    )
    parser.add_argument('modules', nargs='*', metavar='MODULE',
                        help='modules to run (default: all): ' + ', '.join(MODULES))
    args = parser.parse_args()
    if not args.allow_real_app:
        parser.error('refusing real-application tests without --allow-real-app; '
                     'obtain explicit user authorization and isolate the configuration, '
                     'session and native preferences first')
    modules = args.modules or MODULES
    for module in modules:
        if module not in MODULES:
            parser.error(f'unknown GUI test module: {module}')
    suite = unittest.defaultTestLoader.loadTestsFromNames([f'gui_tests.{module}' for module in modules])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
