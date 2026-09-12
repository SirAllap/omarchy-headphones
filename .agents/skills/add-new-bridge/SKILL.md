---
name: add-new-bridge
description: Add headphone support to Omaphones using owner captures, model pins and hardware validation. Use /add-new-bridge or requests to add a headphone model or brand; first check whether an existing bridge suffices. Does not merge or release the result.
---

# Add headphone support

Deliver a contribution another person can review without owning these
headphones. Start with the owner's device and observed replies, then make
implementation, tests, documentation and the hardware report agree.

Read `AGENTS.md`, `BRIDGE.md`, `docs/CANONICAL-TESTS.md` and the relevant
`PROTOCOL.md` section from the repository root. They define the contract and
required coverage; use this workflow to produce it, not a second rulebook.

## Choose the smallest integration

Record the reported name, firmware when available, address and complete
`bluetoothctl info` UUID list. Inspect the existing backend selection and
try the supported interface first. Working battery/control may need only a
new owner capture, pin, model documentation and gallery entry. Extend an
existing brand bridge by a model row when its protocol fits. Create a new
bridge only for a distinct protocol or transport that requires one.

Work in an isolated clone outside the installed plugin, including Git
metadata and generated outputs. Resolve symlinks before writing. Do not
reload the live plugin while capturing or testing. Before probing, release
its competing mode bridge through the supported setting and save the setting
for restoration. Do not operate another person's headphones or restart the
Bluetooth daemon as an incidental part of adding this model.

## Capture before implementing

Keep exact TX and RX bytes, direction, transport/channel and timestamps at
receipt. Record complete frames, including headers; a decoded payload is
useful alongside the raw bytes, not a replacement for them. Preserve partial
and combined reads in the logger. Identify redactions and omissions. A timer
that prints buffered replies after a wait does not measure response latency.

External implementations can suggest probes; they do not prove this model
answered. Separate observed replies, negative results and untested hypotheses.
Probe only understood operations within the owner's requested scope. Do not
ship guessed controls or expand runtime discovery using another model's
unverified channel. Keep experiments in explicit probe options.

Probe tools must actually execute the documented queue. Read the initial
settings before changing them, restore that observed state in `finally`, and
verify the restoration response. Abort writes if initial state is unknown.
On a lost connection, report failed restoration rather than claiming success.
Do not silently set a preferred default. Close sockets and release profiles
on errors, interruption and normal exit. Document any temporary audio impact.

## Implement the contract

Use `BRIDGE.md` and a relevant existing bridge as references. Match argument
validation, parent death, stdin EOF, stdout EPIPE, signal handling, reported
state and exit semantics. Read a reference critically: copying an existing
bug does not fulfill the contract.

Authenticate the protocol response, not merely a successful socket connect
or matching function ID. Reject echoes, error replies and unrelated frames;
validate the observed response shape. Preserve data following initialization
when a read contains several frames. Handle resets/errors inside the probe
and close the candidate socket on every failed path.

Shutdown must interrupt discovery, retry waits and the main loop. Check stop
state before another connect/send. Bound waits so a clean stop cannot turn
into minutes of background retries. Distinguish a connected but silent device
from transport failure. Do not publish requested state until the device
reports it. Ignore unsupported commands without sending bytes.

Keep known devices' rows, bytes, retry paths, routing and owner pins intact.
A new backend must not steal an existing device through UUID precedence.
Do not add compatibility aliases or migration scaffolding for contributor
workflows; preserve the actual supported-headphone contract.

## Test real paths and evidence

Add a Session on `tests/harness.py` and the model's own pin naming its owner
and capture. Cover exact outgoing startup and control sequences and actual
incoming replies. Reuse captured RX bytes, not the production encoder's
reconstruction as independent evidence. Keep existing pins/captures intact.

Follow the applicable canonical scenarios. In particular:

- Drive timeout through the clock and run loop; calling `finish(3)` only tests
  assignment, not detection of silence.
- Exercise disconnect, partial/coalesced reads, invalid replies and cleanup
  through the socket callbacks or loop. Verify retry destinations and counts.
- Request shutdown during connection/probing and assert no later attempts.
- Drive delayed readback through scheduling; a manual poll alone does not
  establish that the timer works.
- Mock clock, sockets and selectors to make failures deterministic. Keep
  platform-specific constants inside test doubles where CI lacks them.
- Mark synthetic damage/battery boundary cases as synthetic. A plausible
  battery value is not a captured sample. Never alter another owner's pin
  or weaken the harness to make a result pass.

Test the probes too when they change state: successful restoration, unknown
initial state, interruption/failure, frame retention and complete queueing.
These are tests of behavior, not assertions that source contains keywords.

## Complete and validate the contribution

Update the new model's README row, prominent brand list, gallery image and
credit, manifest aliases and relevant descriptions, protocol notes, bridge
list/contract and probe instructions. Use the real owner's screenshot; never
synthesize a hardware claim. Match every claim to a capture or explicit owner
test. If required evidence is unavailable, document the gap and keep the
contribution pending or draft rather than inventing evidence.

Run `CHECK_BASE=<current-upstream-base> tools/check`. Investigate each failure
and identify skipped tools. Check the actual test workflow on the PR head;
a green owner-pin check is not the full CI result.

Have the owner test the final executable revision on the device. Record SHA,
initial settings, controls requested, device-reported results, reconnect,
restoration and failures. Label untested capabilities, including charging,
peer isolation or acoustic effects, explicitly. A later repair to transport
or control behavior needs owner confirmation of that repaired revision.

Prepare a focused PR describing the final behavior, evidence, checks and
limits. Preserve contributor credit. Publishing follows the user's actual
authorization; adding support does not authorize a merge or release.
Maintainers use `.agents/skills/land-pr/SKILL.md` for that next step.
