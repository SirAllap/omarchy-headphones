# Adapter refactor candidate — local validation

Branch: `codex/adapter-session-evidence`.

Built from the combined adapter branch at `d1d6e80` and current Omaphones 1.3.3
at `f0f8006`. The merge retains the refactor tests and the WH-CH520 owner test.

## Implemented

- Shared adapter runtime and per-model contribution workflow from the combined
  branch, integrated with 1.3.3.
- Sony WH-CH520's battery-only exclusion in the native codec, without changing
  its original owner pin. Nothing's original model-specific RFCOMM channels and
  Soundcore's observed UUID, offset and query policy in the migration host.
- Independent BTSnoop session archives, an action/observation CSV timeline,
  source checksums and validation, and explicit packet-slice provenance for
  derived replay bytes. Source RX/TX slices cannot be reused or reordered within
  a direction. Original historical owner evidence stays unchanged.
- An explicit `tools/test-refactor` owner workflow for the new adapter host,
  with revision fingerprint, control results, failure logging and restoration.
- Contributor instructions and a reusable GitHub message/agent prompt.

## Verified locally

`CHECK_BASE=upstream-baseline tools/check` passed:

- 256 Python tests, including transport callbacks, original pins, native codecs,
  source-file corruption/loss, provenance and interrupted control restoration.
- Model.js tests (65 declarations), generated registry/package checks, QML lint
  and Omarchy plugin validation.
- Manifest remains at version 1.3.3; no version bump.
- Original bridges, Fast Pair reader, pins and captures match `f0f8006` byte for
  byte (`git diff --exit-code upstream-baseline -- '*-bridge' gfps-reader
  tests/pins docs/captures`).
- BlueZ btmon 5.87 read a synthetic BTSnoop format fixture as an HCI Reset
  command and Command Complete event with the expected 1 ms interval. This was
  file-format validation, not traffic sent to headphones.

## Hardware and publication status

Sony WH-CH720N and JBL TUNE230NC TWS are paired on the maintainer's machine;
both were disconnected when checked. No candidate hardware pass is claimed.
Physical controls, reconnects, independent battery delivery, acoustics and
candidate QML integration still need owner testing. A successful automated
adapter run alone will not close these checks.

Existing models continue to use their original bridges in normal plugin
routing. The explicit test runner exercises the native codecs without activating
them in the installed plugin. Migration/release decisions follow returned owner
evidence. The GitHub message is a local draft; substitute a published candidate
SHA and documentation URL before sending. No contributor messages, push,
release or article publication were performed as part of this validation.
