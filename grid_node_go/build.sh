#!/bin/sh
# Builds the grid-node binaries this server hands out at /node/bin/<target>.
#
#     ./grid_node_go/build.sh                    # every target
#     ./grid_node_go/build.sh linux-arm64        # only these
#
# The Linux and Windows targets are CGO-free, so cross-compiling them needs no
# C toolchain at all.
#
# Android is different in two ways and needs the NDK:
#   - Android refuses a fixed-address executable (e_type 2 / ET_EXEC — "has
#     unexpected e_type: 2" from the system linker) and runs only position
#     independent ones, so the build is a PIE that asks for /system/bin/linker64;
#   - DNS does not work without libc. Go says so itself (net/conf.go: "DNS
#     requests don't work on Android, so prefer the cgo resolver", issue #10714),
#     and a cgo-free node looks names up in /etc/resolv.conf, which Android does
#     not have: it ends with "dial tcp: lookup grid.example on [::1]:53:
#     connection refused". So android-arm64 is built with cgo, against bionic.
#
# The NDK is found by GRID_ANDROID_NDK, ANDROID_NDK_HOME, ANDROID_NDK_ROOT or
# $ANDROID_HOME/ndk/<version>; the API level comes from GRID_ANDROID_API (24 by
# default, the oldest Termux supports).
set -eu

cd "$(dirname "$0")"
output="${GRID_NODE_BIN_DIR:-../dist/grid-node}"
mkdir -p "$output"

targets="${*:-linux-amd64 linux-arm64 android-arm64 windows-amd64}"

# android_toolchain prints the clang that builds for Android arm64.
android_toolchain() {
  api="${GRID_ANDROID_API:-24}"
  roots="${GRID_ANDROID_NDK:-} ${ANDROID_NDK_HOME:-} ${ANDROID_NDK_ROOT:-}"
  if [ -d "${ANDROID_HOME:-/nonexistent}/ndk" ]; then
    roots="$roots $(ls -d "${ANDROID_HOME}/ndk/"* 2>/dev/null || true)"
  fi
  for root in $roots; do
    [ -n "$root" ] || continue
    clang="$root/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android$api-clang"
    if [ -x "$clang" ]; then
      printf '%s\n' "$clang"
      return 0
    fi
  done
  echo "grid-node: android-arm64 needs the Android NDK (r23+) to link bionic;" >&2
  echo "  install it and name it: GRID_ANDROID_NDK=\$HOME/android-ndk ./grid_node_go/build.sh android-arm64" >&2
  return 1
}

for target in $targets; do
  buildmode=""
  cgo=0
  cc=""
  case $target in
    linux-amd64)   GOOS=linux   GOARCH=amd64 ;;
    linux-arm64)   GOOS=linux   GOARCH=arm64 ;;
    android-arm64)
      GOOS=android; GOARCH=arm64; buildmode="-buildmode=pie"; cgo=1
      cc="$(android_toolchain)"
      # The clang in the NDK finds its own siblings by name.
      PATH="$(dirname "$cc"):$PATH"
      export PATH
      ;;
    windows-amd64) GOOS=windows GOARCH=amd64 ;;
    *)
      echo "grid-node: unknown target $target (linux-amd64, linux-arm64, android-arm64, windows-amd64)" >&2
      exit 2
      ;;
  esac
  name="grid-node-$target"
  case $GOOS in windows) name="$name.exe" ;; esac
  echo "grid-node: $name"
  if [ -n "$cc" ]; then
    CGO_ENABLED="$cgo" CC="$cc" GOOS="$GOOS" GOARCH="$GOARCH" \
      go build $buildmode -trimpath -ldflags "-s -w" -o "$output/$name" .
  else
    CGO_ENABLED="$cgo" GOOS="$GOOS" GOARCH="$GOARCH" \
      go build $buildmode -trimpath -ldflags "-s -w" -o "$output/$name" .
  fi
done
