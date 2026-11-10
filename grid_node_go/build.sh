#!/bin/sh
# Builds the grid-node binaries this server hands out at /node/bin/<target>.
#
#     ./grid_node_go/build.sh                    # every target
#     ./grid_node_go/build.sh linux-arm64        # only these
#
# All targets are CGO-free, so cross-compiling needs no C toolchain. Android is
# not built like Linux: Android refuses a fixed-address executable (e_type 2 /
# ET_EXEC — "has unexpected e_type: 2" from the linker) and only runs position
# independent ones, so the Android binary is a PIE that asks for
# /system/bin/linker64. Building it as linux/arm64 makes a binary Termux cannot
# even load.
set -eu

cd "$(dirname "$0")"
output="${GRID_NODE_BIN_DIR:-../dist/grid-node}"
mkdir -p "$output"

targets="${*:-linux-amd64 linux-arm64 android-arm64 windows-amd64}"

for target in $targets; do
  buildmode=""
  case $target in
    linux-amd64)   GOOS=linux   GOARCH=amd64 ;;
    linux-arm64)   GOOS=linux   GOARCH=arm64 ;;
    android-arm64) GOOS=android GOARCH=arm64; buildmode="-buildmode=pie" ;;
    windows-amd64) GOOS=windows GOARCH=amd64 ;;
    *)
      echo "grid-node: unknown target $target (linux-amd64, linux-arm64, android-arm64, windows-amd64)" >&2
      exit 2
      ;;
  esac
  name="grid-node-$target"
  case $GOOS in windows) name="$name.exe" ;; esac
  echo "grid-node: $name"
  CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" \
    go build $buildmode -trimpath -ldflags "-s -w" -o "$output/$name" .
done
