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
- Structured owner interviews through terminal, JSON lines and an optional local
  browser panel. Initial Ready starts the first control; saving an observation
  starts the displayed next step without another Ready. Save & pause waits for
  Resume; Save & repeat restores a baseline and repeats after two seconds; an ordered plan shows the current and next test. Questions/answers are inside
  the existing source CSV checksum boundary.
- Explicit Pause, Skip, Stop and bounded owner waits; adapter restoration still
  runs when interview logging fails. The assistant remains responsible for its
  separate btmon process and temporary plugin setting.

## Verified locally

`CHECK_BASE=upstream-baseline tools/check` passed:

- 277 Python tests, including transport callbacks, original pins, native codecs,
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

The independent Astra-low rehearsal on candidate `d9f1b646` confirmed audible
Sony mode changes and return to initial ANC. It also reproduced a timeout when
setting voice focus false in ANC, and sealing rejected backward timestamps in
the original BTSnoop captures. Both failures and all original bytes are retained.
JBL testing is paused. No overall candidate hardware pass is claimed.

The new interview was verified with synthetic devices and real stdin/stdout
pipes. Browser QA covered Ready, an uncertain observation with comment, Repeat,
Stop/restoration, and a completed comparison. Refresh preserved the pending
question. The combined save/start path was also verified in the browser: the
next comparison opens directly, Repeat runs without Ready, and Save & pause
waits for Resume. These UI exercises used no Bluetooth or plugin-setting operations.
The new guided path still needs an owner hardware rehearsal; it does not fix
or mask the two earlier Sony-session failures. General acoustic performance,
reconnect, battery, wear, charging, peer isolation and candidate QML integration
remain separate hardware checks.

Existing models continue to use their original bridges in normal plugin
routing. The explicit test runner exercises the native codecs without activating
them in the installed plugin. Migration/release decisions follow returned owner
evidence. The GitHub message is a local draft; substitute a published candidate
SHA and documentation URL before sending. No contributor messages, push,
release or article publication were performed as part of this validation.
