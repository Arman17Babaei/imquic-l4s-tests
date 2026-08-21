#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
IMQUIC_DIR="$ROOT/deps/imquic"
OUTPUT="$ROOT/build/imquic-3dgs-moq-scheduled"

mkdir -p "$ROOT/build"
${CC:-cc} -std=c11 -O2 -Wall -Wextra \
  -I"$IMQUIC_DIR/src" \
  $(pkg-config --cflags glib-2.0 libssl libcrypto jansson) \
  "$ROOT/tests/3dgs-scheduled-moq-gated-test.c" -o "$OUTPUT" \
  -L"$IMQUIC_DIR/src/.libs" -limquic \
  $(pkg-config --libs glib-2.0 libssl libcrypto jansson) -lm -pthread \
  -Wl,-rpath,'$ORIGIN/../deps/imquic/src/.libs'

echo "$OUTPUT"
