ifdef V
	VVAL=--verbose
endif
ifdef VERBOSE
	VVAL=--verbose
endif

ifdef FAIL_WARN
export FAIL_WARN
endif

# The official upstream macOS packaging path. bypy builds a frozen app bundle
# (with the bundled slangc and the quick access sub app completed), runs the
# tests and creates the DMG. publish.py runs the same command, just inside
# bypy's macOS VM:
#
#   python ../bypy macos program --sign-installers --notarize ...
#
# This target runs the build half locally against a prepared bypy root. That
# directory is bypy's workspace, not something this repository creates:
#
#   $(BYPY_ROOT)/bypy     checkout of https://github.com/kovidgoyal/bypy
#   $(BYPY_ROOT)/src      synchronized copy of this source tree (filled in below)
#   $(BYPY_ROOT)/sources  downloaded dependency sources
#   $(BYPY_ROOT)/sw/pkg   compiled dependency packages (bypy dependencies)
#   $(BYPY_ROOT)/sw/sw    installed prefix; bypy removes it after packing and
#                         reinstalls it from sw/pkg when packaging
#
# Official releases build the dependencies and run the program inside bypy's
# macOS VM (python ../bypy macos program ...). Locally, prepare the root once:
# clone bypy into $(BYPY_ROOT)/bypy, then run `make macos-app-deps`. The target
# refuses to run without a prepared root because bypy reinstalls the prefix
# from sw/pkg before building. Point BYPY_ROOT at any prepared root instead of
# using the default: make macos-app BYPY_ROOT=/path/to/root.
BYPY_ROOT ?= /Users/Shared/kitty-build
BYPY_TOOL ?= $(BYPY_ROOT)/bypy
# bypy needs Python >= 3.12. Prefer a modern interpreter from PATH and fall
# back to the one shipped in the prepared bypy root, so make works regardless
# of whether /usr/bin (which can have Python 3.9) comes first in PATH.
BYPY_PYTHON ?= $(shell for p in python3.15 python3.14 python3.13 python3.12; do command -v $$p 2>/dev/null && break; done)
ifeq ($(strip $(BYPY_PYTHON)),)
BYPY_PYTHON := $(BYPY_ROOT)/sw/sw/bin/python
endif
BYPY_UNIVERSAL ?= true
BYPY_DEPLOY_TARGET ?= 12.0
# Dependency prefix exported by the project's CI, produced by bypy inside the
# same macOS build VM the official packages use.
BYPY_DEPS_BUNDLE_URL ?= https://download.calibre-ebook.com/ci/kitty/macos-64.tar.xz
MACOS_SIGN ?= 0
MACOS_NOTARIZE ?= 0
SKIP_TESTS ?= 0
VCS_REV ?= $(shell git rev-parse HEAD)

# Same exclusions as bypy's VM sync (bypy/vms.py + bypy/rsync.conf), plus the
# bundle this tree may contain from an earlier local build.
BYPY_RSYNC_EXCLUDES := \
	*.pyc *.pyo *.swp *.swo *.pyj-cached *~ .git .cache \
	/bypy/b /dependencies /build /dist /shaders /kitty/launcher/kitty* \
	/.build-cache /.ruff_cache /tags __pycache__ /*_commands.json \
	*.so *.pyd *_generated.go /kitty.app

macos-app: FORCE
	@test "$$(uname -s)" = Darwin || { echo 'macos-app requires macOS' >&2; exit 1; }
	@test -f "$(BYPY_TOOL)/__main__.py" || { echo "bypy build environment not found at $(BYPY_ROOT)" >&2; echo "run 'make macos-app-deps' first (it fetches the bypy tool and the prebuilt dependency snapshot)" >&2; echo "or build the dependencies from source with 'make macos-app-deps-source'" >&2; exit 1; }
	@test -n "$$(ls -A "$(BYPY_ROOT)/sw/pkg" 2>/dev/null)" || { echo "no compiled dependency packages in $(BYPY_ROOT)/sw/pkg" >&2; echo "run 'make macos-app-deps' (prebuilt snapshot) or 'make macos-app-deps-source'" >&2; exit 1; }
	@test -x "$(BYPY_PYTHON)" || { echo "no usable Python for bypy; set BYPY_PYTHON to a Python >= 3.12" >&2; exit 1; }
	@$(BYPY_PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "bypy needs Python >= 3.12, set BYPY_PYTHON" >&2; exit 1; }
	@test -f docs/_build/html/index.html || $(MAKE) FAIL_WARN=1 docs
	@mkdir -p "$(BYPY_ROOT)/src" "$(BYPY_ROOT)/sources" "$(BYPY_ROOT)/sw/pkg"
	rsync -a --delete --delete-excluded --chmod=og-w $(foreach x,$(BYPY_RSYNC_EXCLUDES),--exclude='$(x)') ./ "$(BYPY_ROOT)/src/"
	cd "$(BYPY_ROOT)/src" && \
	BYPY_ROOT="$(BYPY_ROOT)" BYPY_ARCH=macos BYPY_UNIVERSAL="$(BYPY_UNIVERSAL)" BYPY_DEPLOY_TARGET="$(BYPY_DEPLOY_TARGET)" \
	"$(BYPY_PYTHON)" "$(BYPY_TOOL)" program --non-interactive \
		--compression-level 9 \
		$(if $(filter 1,$(DONT_STRIP)),--dont-strip) \
		$(if $(filter 1,$(SKIP_TESTS)),--skip-tests) \
		$(if $(filter 1,$(MACOS_SIGN)),--sign-installers) \
		$(if $(filter 1,$(MACOS_NOTARIZE)),--notarize) \
		$(if $(VCS_REV),--extra-program-data "$(VCS_REV)")
	@ls -l "$(BYPY_ROOT)/sw/dist/"*.dmg

# bypy has no Homebrew formula and is not a pip package (the PyPI "bypy" is an
# unrelated Baidu client). It is a tool run from a checkout. Fetch it into
# $(BYPY_TOOL) without requiring git, for machines where git access is blocked.
macos-app-tool: FORCE
	@if test -f "$(BYPY_TOOL)/__main__.py"; then \
		echo "bypy tool present at $(BYPY_TOOL)"; \
	else \
		mkdir -p "$(BYPY_TOOL)"; \
		echo "Downloading bypy tool into $(BYPY_TOOL)"; \
		tmp=$$(mktemp -t bypy).tar.gz; \
		curl -fsSL -o "$$tmp" https://codeload.github.com/kovidgoyal/bypy/tar.gz/refs/heads/master; \
		tar xzf "$$tmp" -C "$(BYPY_TOOL)" --strip-components=1; \
		rm -f "$$tmp"; \
	fi

# Prepare $(BYPY_ROOT)/sw/pkg from the prebuilt dependency snapshot exported by
# CI. This avoids compiling the whole dependency stack from source. bypy's
# program step wipes sw/sw and reinstalls the prefix from sw/pkg, so the snapshot
# is stored there as the "python" package (a dependency name bypy understands).
# The snapshot contains absolute symlinks to the default root, so it is only
# used for BYPY_ROOT=/Users/Shared/kitty-build; use macos-app-deps-source for
# other roots.
macos-app-deps: macos-app-tool
	@test "$$(uname -s)" = Darwin || { echo 'macos-app-deps requires macOS' >&2; exit 1; }
	@test "$(BYPY_ROOT)" = /Users/Shared/kitty-build || { echo "the prebuilt snapshot assumes BYPY_ROOT=/Users/Shared/kitty-build" >&2; echo "use 'make macos-app-deps-source' for a custom root" >&2; exit 1; }
	@test -f "$(BYPY_TOOL)/__main__.py" || { echo "bypy tool not found at $(BYPY_TOOL)" >&2; exit 1; }
	@$(BYPY_PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "bypy needs Python >= 3.12, set BYPY_PYTHON" >&2; exit 1; }
	@set -e; \
		archive="$(BYPY_ROOT)/sw/macos-64.tar.xz"; \
		mkdir -p "$(BYPY_ROOT)/sw"; \
		if test ! -s "$$archive" || ! xz -t "$$archive" 2>/dev/null; then \
			rm -f "$$archive"; \
			echo "Downloading prebuilt dependency snapshot from $(BYPY_DEPS_BUNDLE_URL)"; \
			size=$$(curl -fsSIL "$(BYPY_DEPS_BUNDLE_URL)" | tr -d '\r' | awk 'tolower($$1)=="content-length:"{v=$$2} END{print v}'); \
			if test -n "$$size"; then \
				n=8; chunk=$$(( (size + n - 1) / n )); tmp=$$(mktemp -d); pids=""; \
				for i in 0 1 2 3 4 5 6 7; do \
					a=$$((i * chunk)); b=$$((a + chunk - 1)); \
					if test "$$b" -ge "$$size"; then b=$$((size - 1)); fi; \
					curl -fsSL -r "$$a-$$b" -o "$$tmp/part.$$i" "$(BYPY_DEPS_BUNDLE_URL)" & \
					pids="$$pids $$!"; \
				done; \
				for p in $$pids; do wait "$$p" || { rm -rf "$$tmp"; echo "Snapshot download failed" >&2; exit 1; }; done; \
				for i in 0 1 2 3 4 5 6 7; do cat "$$tmp/part.$$i"; done > "$$archive"; \
				rm -rf "$$tmp"; \
			else \
				curl -fsSL -o "$$archive" "$(BYPY_DEPS_BUNDLE_URL)"; \
			fi; \
			xz -t "$$archive"; \
		fi
	@set -e; \
		rm -rf "$(BYPY_ROOT)/sw/sw" "$(BYPY_ROOT)/sw/pkg"; \
		mkdir -p "$(BYPY_ROOT)/sw/pkg/python"; \
		tar xf "$(BYPY_ROOT)/sw/macos-64.tar.xz" -C "$(BYPY_ROOT)/sw/pkg/python"; \
		test -x "$(BYPY_ROOT)/sw/pkg/python/bin/slangc" || { echo "snapshot is missing bin/slangc" >&2; exit 1; }
	@echo "Prebuilt dependencies ready in $(BYPY_ROOT)/sw/pkg; run 'make macos-app'."

# Build every dependency from source with bypy. This is what the official
# release does inside bypy's macOS VM: slow, needs network access to all source
# sites and GitHub submodules, plus the toolchain. Kept for custom roots or when
# the prebuilt snapshot is not acceptable.
macos-app-deps-source: macos-app-tool
	@test "$$(uname -s)" = Darwin || { echo 'macos-app-deps-source requires macOS' >&2; exit 1; }
	@test -f "$(BYPY_TOOL)/__main__.py" || { echo "bypy tool not found at $(BYPY_TOOL)" >&2; exit 1; }
	@$(BYPY_PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "bypy needs Python >= 3.12, set BYPY_PYTHON" >&2; exit 1; }
	@mkdir -p "$(BYPY_ROOT)/src" "$(BYPY_ROOT)/sources" "$(BYPY_ROOT)/sw/pkg"
	rsync -a --delete --delete-excluded --chmod=og-w $(foreach x,$(BYPY_RSYNC_EXCLUDES),--exclude='$(x)') ./ "$(BYPY_ROOT)/src/"
	cd "$(BYPY_ROOT)/src" && \
	BYPY_ROOT="$(BYPY_ROOT)" BYPY_WORKER="$(BYPY_ROOT)/src" BYPY_ARCH=macos BYPY_UNIVERSAL="$(BYPY_UNIVERSAL)" BYPY_DEPLOY_TARGET="$(BYPY_DEPLOY_TARGET)" \
	"$(BYPY_PYTHON)" "$(BYPY_TOOL)" dependencies
	@test -n "$$(ls -A "$(BYPY_ROOT)/sw/pkg" 2>/dev/null)" || { echo "bypy dependencies produced no packages in $(BYPY_ROOT)/sw/pkg" >&2; echo "check its output above; packaging cannot run until the dependencies succeed" >&2; exit 1; }
	@echo "Dependencies are in $(BYPY_ROOT)/sw/pkg. bypy removes the installed prefix after packing it, so the next target reinstalls it from there."

all:
	python3 setup.py $(VVAL)

test:
	python3 setup.py $(VVAL) test

clean:
	python3 setup.py $(VVAL) clean

# A debug build
debug:
	python3 setup.py build $(VVAL) --debug

debug-event-loop:
	python3 setup.py build $(VVAL) --debug --extra-logging=event-loop

# Build with the ASAN and UBSAN sanitizers
asan:
	python3 setup.py build $(VVAL) --debug --sanitize

profile:
	python3 setup.py build $(VVAL) --profile

app:
	python3 setup.py kitty.app $(VVAL)

linux-package: FORCE
	rm -rf linux-package
	python3 setup.py linux-package

FORCE:

man:
	$(MAKE) -C docs man

html:
	$(MAKE) -C docs html

dirhtml:
	$(MAKE) -C docs dirhtml

linkcheck:
	$(MAKE) -C docs linkcheck

website:
	./publish.py --only website

docs: man html


develop-docs:
	$(MAKE) -C docs develop-docs


prepare-for-cross-compile: clean all
	python3 setup.py $(VVAL) clean --clean-for-cross-compile

cross-compile:
	python3 setup.py linux-package --skip-code-generation

