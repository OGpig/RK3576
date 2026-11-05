#!/usr/bin/env bash
# Cross-compile the C mirror for RK3576 (aarch64 glibc) with Zig.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIG="${ZIG:-F:/data/toolchains/zig/zig-windows-x86_64-0.13.0/zig.exe}"
LIBDIR="${LIBDIR:-F:/data/toolchains/board-libs}"

if [ ! -x "$ZIG" ]; then
    echo "Zig not found: $ZIG" >&2
    echo "Set ZIG=/path/to/zig.exe" >&2
    exit 1
fi
for lib in libturbojpeg.so.0 libssl.so.3 libcrypto.so.3 librga.so.2 \
           libgstreamer-1.0.so.0 libgstapp-1.0.so.0 libgobject-2.0.so.0 libglib-2.0.so.0; do
    if [ ! -f "$LIBDIR/$lib" ]; then
        echo "$lib not found in $LIBDIR" >&2
        echo "Copy it from the board: scp root@192.168.31.153:/usr/lib/$lib $LIBDIR/" >&2
        exit 1
    fi
done

"$ZIG" cc -target aarch64-linux-gnu -O3 -pthread \
    -L "$LIBDIR" -l:libturbojpeg.so.0 -l:libssl.so.3 -l:libcrypto.so.3 \
    -l:librga.so.2 -l:libgstreamer-1.0.so.0 -l:libgstapp-1.0.so.0 \
    -l:libgobject-2.0.so.0 -l:libglib-2.0.so.0 \
    "$ROOT/rkdesk_c.c" -lm -o "$ROOT/rkdesk_c"

ls -lh "$ROOT/rkdesk_c"
echo "built $ROOT/rkdesk_c"
