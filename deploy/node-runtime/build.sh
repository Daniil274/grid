#!/bin/sh
# Builds the portable runtimes a node fetches from /node/runtime/<name>.tar.gz
# when its device has no Python of its own: a standalone CPython, and uv as
# the fallback that brings one on first use.
#
#     deploy/node-runtime/build.sh                # every target
#     deploy/node-runtime/build.sh uv-linux-arm64 # only these
#
# The archives land in dist/node-runtime/<name>.tar.gz with the
# <name>.tar.gz.sha256 the node holds the download against. dist/ is not
# tracked by git, so on a new server this script is the way to (re)build
# them. The pins are defaults; override with GRID_PBS_TAG, GRID_PYTHON and
# GRID_UV_TAG.
set -eu

cd "$(dirname "$0")/../.."
out="dist/node-runtime"
mkdir -p "$out"

pbs_tag="${GRID_PBS_TAG:-20261110}"
python="${GRID_PYTHON:-3.12.16}"
uv_tag="${GRID_UV_TAG:-0.13.7}"

# fetch <name> <url>: the archive into dist/node-runtime, its sha256 beside it.
fetch() {
  name="$1"; url="$2"
  echo "node-runtime: $name"
  curl -fL --retry 3 -o "$out/$name.tar.gz" "$url"
  sha256sum "$out/$name.tar.gz" | cut -d' ' -f1 > "$out/$name.tar.gz.sha256"
}

targets="${*:-python-linux-amd64 python-linux-arm64 uv-linux-amd64 uv-linux-arm64}"

for target in $targets; do
  case $target in
    python-linux-amd64)
      fetch "$target" "https://github.com/astral-sh/python-build-standalone/releases/download/$pbs_tag/cpython-$python+$pbs_tag-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz" ;;
    python-linux-arm64)
      fetch "$target" "https://github.com/astral-sh/python-build-standalone/releases/download/$pbs_tag/cpython-$python+$pbs_tag-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz" ;;
    uv-linux-amd64)
      fetch "$target" "https://github.com/astral-sh/uv/releases/download/$uv_tag/uv-x86_64-unknown-linux-gnu.tar.gz" ;;
    uv-linux-arm64)
      fetch "$target" "https://github.com/astral-sh/uv/releases/download/$uv_tag/uv-aarch64-unknown-linux-gnu.tar.gz" ;;
    *)
      echo "node-runtime: unknown target $target (python-linux-amd64, python-linux-arm64, uv-linux-amd64, uv-linux-arm64)" >&2
      exit 2 ;;
  esac
done

ls -la "$out"
