# Explicit native GUI checks

These checks are **not unit tests**. They instantiate production AppKit views,
may create native windows, and may access application preferences. They are
kept outside `kitty_tests` so that `./test.py` and build-time unit-test discovery
do not run them.

The Linux single-instance IPC check is also kept here: it starts a normal kitty
command rather than a headless runner, so it is not safe for default unit-test
execution even though its intended path only contacts a test socket.

Do not run these checks unless the user has explicitly authorized real-GUI
testing. An opt-in command-line flag is an additional safeguard, not a substitute
for that authorization. Review and isolate application preferences, configuration,
and session storage before any authorized run. Never target an installed/running
user application or use the user's real session as a fixture.

For normal verification, run the corresponding headless modules instead:

```sh
./test.py --module sidebar_sections
./test.py --module sidebar_fonts
./test.py --module pane_switcher
```

The explicit GUI entry point must refuse execution without
`--allow-real-app`. Do not add these checks back to default unit-test discovery,
or import a GUI test class into a unit-test module (unittest would discover it).
