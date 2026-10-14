# SQLCipher compatibility decision — Storage Gate 1.5

Decision date: 2026-08-07

## Bounded environments

- Local: macOS arm64, Python 3.14.3, `sqlcipher3-wheels==0.5.7` cp314 arm64 wheel.
- Production base: `python:3.13.5-slim-bookworm`, Linux aarch64, cp313 manylinux wheel.
- SQLCipher reported `4.12.0 community` in both environments.
- The complete application dependency layer built and imported SQLCipher as the non-root container user.

## Required checks

Both environments created and reopened a keyed database, kept a synthetic marker and the standard SQLite
header out of raw database bytes, rejected a different 32-byte key, produced a separately keyed encrypted
database copy, and reopened the copy successfully. The production-mode application container also enforced
owner-only mounted runtime permissions and wrote an encrypted synthetic database.

## Decision and boundary

The binding is sufficiently reliable for this Mac mini/aarch64 deployment boundary, so Gate 1.5 adopts
whole-database SQLCipher encryption. The pinned wheel and platform compatibility must be re-spiked before
changing Python, CPU architecture, base image, SQLCipher binding, or SQLCipher compatibility settings.
Dependency auditing reported no known vulnerability for the installed environment at the decision date.
