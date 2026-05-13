#!/usr/bin/env python3
"""
skills/jcode_swarm/coord_daemon.py — REMOVED

jcode (github.com/1jehuang/jcode) provides a built-in coordination server
natively via `jcode serve --port 7865`. It handles per-file conflict
detection, change notifications to sibling swarm workers, session memory,
and ONNX-powered local embeddings.

This file previously contained a custom re-implementation of that server,
which has been removed. Use the real binary instead:

  cargo install jcode
  jcode serve --port 7865

supervisord will start `jcode serve` automatically when the binary is present.
See supervisord.conf [program:jcode-server].
"""

import sys

print(
    "[coord_daemon] This custom coordination daemon has been removed.\n"
    "Use the real jcode binary instead: cargo install jcode\n"
    "Then: jcode serve --port 7865",
    file=sys.stderr,
)
sys.exit(1)
