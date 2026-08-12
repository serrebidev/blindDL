# Vendored musicdl

This directory contains musicdl from:

- Upstream: https://github.com/CharlesPikachu/musicdl
- Commit: `485edb58487d4393f6aba5addefb6e24b1aac652`
- Commit date: 2026-08-11
- Upstream version: 2.13.6
- License: PolyForm Noncommercial License 1.0.0 (see `LICENSE`)

blindDL vendors this revision because upstream 2.13.6 and current Git HEAD pin
`cryptography>=46.0.5,<47`, while that line has published security advisories.
The application instead declares and tests `cryptography>=50,<51`; no musicdl
source code is modified for that compatibility change.
