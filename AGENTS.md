# Repository instructions

## Mandatory test isolation

- Unit tests MUST NOT launch, activate, or control a real application. In
  particular, do not launch the installed kitty App, run `open -a`, start a
  normal kitty GUI process, or create/show a real application window.
- Do not attach to, restart, close, or otherwise interact with a user's running
  application or its windows from a test. Never use the user's live session,
  configuration, or application preferences as test fixtures or write targets.
- Mock application/process-launch boundaries in unit tests. Pure in-memory
  AppKit objects are acceptable only when they do not create real windows,
  activate an application, or read/write persistent user preferences. Mock or
  isolate those boundaries as well.
- Headless test runners (for example, `kitty +runpy`), CLI fixtures, and native
  probes are permitted only if they cannot launch a GUI application or affect
  the user's application state. A timeout or temporary config directory alone
  does not make a real application launch acceptable in a unit test.
- Real-application/UI end-to-end checks must live outside the unit-test suite
  and its default discovery. Run them only with explicit user authorization,
  using an isolated configuration and session; never silently include them in
  `./test.py` or a build's unit-test checks.
  Keep such checks in `gui_tests/`, not `kitty_tests/`, and do not import their
  test classes into `kitty_tests/` (unittest also discovers imported classes).
- Do not run a test suite until any discovered real-application launch paths
  have been removed, mocked, or excluded from default unit-test discovery.
  Verify the test helpers as well as the test bodies. Report checks that remain
  unverified instead of launching a real App to obtain a green result.

## Focused verification

- Use `./test.py --module <module_name>` for a specific Python test module.
  A positional argument selects a test method, not a module. Repeating
  `--module` does not select multiple modules; run separate commands.
- Preserve unrelated working-tree changes. Do not commit changes unless asked.
