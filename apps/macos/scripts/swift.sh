#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export SWIFT_EXEC="$(python3 "$script_dir/prepare-toolchain.py")"
# Standalone Command Line Tools ships Swift Testing outside the system framework search path.
if [ "${1:-}" = "test" ]; then
    compiler=$(xcrun --find swiftc)
    frameworks="${compiler%/usr/bin/swiftc}/Library/Developer/Frameworks"
    if [ -d "$frameworks/Testing.framework" ]; then
        exec swift "$@" -Xswiftc -F -Xswiftc "$frameworks" -Xlinker -rpath -Xlinker "$frameworks"
    fi
fi
exec swift "$@"
