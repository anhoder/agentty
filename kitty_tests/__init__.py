#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys
import unittest

is_ci = os.environ.get('CI') == 'true'
requires_source_checkout = unittest.skipIf(getattr(sys, 'frozen', False), 'requires a source checkout')


def in_isolated_test_env() -> bool:
    """Whether HOME and the XDG dirs point at the throwaway directory created by env_for_python_tests()."""
    return 'KT_ORIGINAL_HOME' in os.environ
