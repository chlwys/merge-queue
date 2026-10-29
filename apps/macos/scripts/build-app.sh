#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$app_dir"
# Build entirely from Apple's Command Line Tools; full Xcode and third-party packages are optional.
"$app_dir/scripts/swift.sh" build -c release
bin_dir=$("$app_dir/scripts/swift.sh" build -c release --show-bin-path)
bundle="$app_dir/dist/Merge Queue.app"
mkdir -p "$bundle/Contents/MacOS"
cp "$bin_dir/MergeQueueMenu" "$bundle/Contents/MacOS/MergeQueueMenu"
cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>MergeQueueMenu</string>
<key>CFBundleIdentifier</key><string>local.merge-queue.menu</string>
<key>CFBundleName</key><string>Merge Queue</string>
<key>CFBundleDisplayName</key><string>Merge Queue</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$bundle"
printf '%s\n' "$bundle"
