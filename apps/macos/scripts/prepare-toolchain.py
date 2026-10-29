#!/usr/bin/env python3
"""Print the active swiftc path, wrapping it only for known broken CLT layouts."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

def tool_output(*args):
    return subprocess.check_output(["xcrun", *args], text=True).strip()

def compiler_version(path):
    match = re.search(r"^// swift-compiler-version: (.+)$", path.read_text(errors="replace"), re.M)
    return match.group(1) if match else None

def duplicate_map(old, new):
    if not old.is_file() or not new.is_file():
        return False
    # Ignore copyright-only drift while requiring the actual declarations to match.
    clean = lambda p: b"\n".join(
        line.strip() for line in p.read_bytes().splitlines()
        if line.strip() and not line.lstrip().startswith((b"//", b"/*", b"*")))
    return clean(old) == clean(new)

def main():
    swiftc = tool_output("--find", "swiftc")
    sdk = tool_output("--sdk", "macosx", "--show-sdk-path")
    if not Path(sdk).is_dir():
        raise RuntimeError(f"macOS SDK is unavailable: {sdk}")
    toolchain = Path(swiftc).resolve().parents[2]
    swift_include = toolchain / "usr/include/swift"
    old_map, new_map = swift_include / "module.modulemap", swift_include / "bridging.modulemap"
    duplicate = duplicate_map(old_map, new_map)
    manifest = toolchain / "usr/lib/swift/pm/ManifestAPI"
    stale_manifest = False
    for private in manifest.glob("*.swiftmodule/*.private.swiftinterface"):
        public = private.with_name(private.name.replace(".private.swiftinterface", ".swiftinterface"))
        old, current = compiler_version(private), compiler_version(public) if public.exists() else None
        if old and current and old != current:
            stale_manifest = True
            break

    if not duplicate and not stale_manifest:
        print(swiftc)
        return

    compat = Path(__file__).resolve().parent.parent / ".build/toolchain-compat"
    compat.mkdir(parents=True, exist_ok=True)
    overlay = compat / "overlay.json"
    roots = []
    if duplicate:
        empty = compat / "empty.modulemap"
        empty.write_text("")
        roots.append({"type": "file", "name": str(old_map), "external-contents": str(empty)})
    isolated_manifest = compat / "ManifestAPI"
    if stale_manifest:
        isolated_manifest.mkdir(exist_ok=True)
        for module in manifest.glob("*.swiftmodule"):
            destination = isolated_manifest / module.name
            destination.mkdir(exist_ok=True)
            for source in module.iterdir():
                if source.suffix in (".swiftinterface", ".swiftdoc") and ".private." not in source.name:
                    target = destination / source.name
                    if not target.exists() or target.read_bytes() != source.read_bytes():
                        target.write_bytes(source.read_bytes())
        dylib = manifest / "libPackageDescription.dylib"
        link = isolated_manifest / dylib.name
        if dylib.exists() and not link.exists():
            link.symlink_to(dylib)

    overlay.write_text(json.dumps({"version": 0, "case-sensitive": "false", "roots": roots}))
    wrapper = compat / "swiftc"
    wrapper.write_text(textwrap.dedent(f'''\
        #!/usr/bin/python3
        import os, sys
        compiler = {swiftc!r}
        sdk = {sdk!r}
        overlay = {str(overlay)!r}
        cache = {str(compat / "module-cache")!r}
        original_manifest = {str(manifest) if stale_manifest else ''!r}
        isolated_manifest = {str(isolated_manifest)!r}
        args = [arg.replace(original_manifest, isolated_manifest) if original_manifest else arg for arg in sys.argv[1:]]
        flags = ["-vfsoverlay", overlay, "-Xcc", "-ivfsoverlay", "-Xcc", overlay,
                 "-module-cache-path", cache, "-F", {str(toolchain / "Library/Developer/Frameworks")!r}]
        env = os.environ.copy()
        env.setdefault("SDKROOT", sdk)
        os.execve(compiler, ["swiftc", *flags, *args], env)
    '''))
    wrapper.chmod(0o755)
    print(wrapper)

if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"prepare-toolchain: {error}", file=sys.stderr)
        raise SystemExit(1)
