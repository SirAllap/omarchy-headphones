# Device adapter API v1 — combined design

This version combines the shared event/effect runtime with one contribution
package per model. Contributors implement protocol bytes once. Live operation,
recording, replay and fault tests all drive the same `Protocol` and `Session`.

**Existing supported headphones still launch their original bridges.** All
original bridge files, owner pins, captures and canonical tests remain intact.
The eight native codecs are migration candidates and reusable starting points
for new, exactly identified models. Automated fixture tests do not constitute
owner hardware approval of these codecs or this QML revision.

## One model, one package

```
devices/<id>/
  device.json
  identity.txt
  protocol.py          # only when a shared codec cannot express this protocol
  capture.jsonl
  session.json
  test_adapter.py
  protocol.md
  hardware-check.json
  owner-checks.json
  screenshot.png
```

Shared codecs live in `adapters/<brand>/protocol.py`, with their parameter and
transport schemas in `adapter.json`. `referenceModels` is private compatibility
metadata for replaying old pins; new contributors never add models there.
There is no second author-maintained model registry.

A device manifest declares API version, id, model, GitHub owner, `draft` or
`active` status, exact identity, adapter definition and observed capabilities.
Here is a schema example; the identity and transport are placeholders:

```json
{
  "apiVersion": 1,
  "id": "brand-model",
  "model": "Reported model name",
  "owner": "owner-login",
  "status": "draft",
  "match": {
    "names": ["Reported model name"],
    "uuids": ["10000000-0000-0000-0000-000000000001"]
  },
  "adapter": {
    "module": "protocol.py",
    "parameters": {},
    "transport": {"kind": "rfcomm", "channels": [1]}
  },
  "capabilities": {"noise.mode": {"values": ["off", "anc"]}}
}
```

A reusable adapter uses `"id": "sony"` instead of `module`, plus explicit
model parameters such as `{"wear": false}` and observed transport overrides.
RFCOMM `channels` and profile `uuidPreference` must be supplied for each new
model; scaffolded lists are empty until the owner fills them. A Soundcore model
must also supply its observed `offset` and `query` behavior. These values must
come from this device, not from a sibling's table.

Each package declares one exact reported name and **every** required UUID.
Routing requires all of them. An
optional six-digit `modelId` must also match; GATT packages require it and an
observed BLE address. Shared UUIDs alone cannot select a device package. Active
packages cannot overlap an existing owner's known model name or another active
package identity. Drafts are inert. All unmatched devices retain legacy routing
and launch arguments, including Bose QC45 from main 1.3.2.

## Protocol boundary

```python
from omaphones.api import Protocol

class Adapter(Protocol):
    def connected(self):
        pass  # Send the observed handshake/query.

    def received(self, data):
        pass  # Frame and validate replies; report actual observations.

    def command(self, control, value):
        pass  # Encode a validated request. Do not report its desired result.
```

| Protocol operation | Shared host responsibility |
|:--|:--|
| `write(bytes)` | Send bytes, buffering partial writes |
| `schedule(ms, "method", *args)` | Run a one-shot timer and return its token |
| `cancel_timer(token)` | Cancel that timer |
| `report(values, capabilities)` | Validate, limit, merge and deduplicate observations |
| `next_endpoint()` | Close the current RFCOMM candidate and try the next declared channel |
| `finish(code, message)` | Stop once, cancel timers and close resources |

The adapter owns framing, sequence numbers, checksums, ACK queues and protocol
retry decisions. The host owns OS resources, transport retries, clocks,
subprocesses, command validation, capture and shell integration. Adapters have
no `run()`/`replay()` transport implementations and do not patch old bridge
classes. The import checker enforces this review boundary; it is not a sandbox
for hostile Python.

Events are `connected`, `received` (bytes), `command`, `timer`, `disconnected`
and `stop`. Stream input may be split at any byte; GATT input is a notification
payload. Each connection gets a new protocol instance and state store.
Exit codes are 0 clean stop, 1 transient failure, 3 connected but unanswered
mode query, and 4 setup failure. A failed connection must not retire a model.
New device packages do not modify the legacy global model-support cache.

## Observed capabilities

| Key | Declaration |
|:--|:--|
| `noise.mode` | `values`: subset of off, anc, ambient, talkthru |
| `ambient.level` | Integer min, max, step |
| `ambient.focus_on_voice` | `type: boolean` |
| `noise.wind_reduction` | `type: boolean`; distinct from focus on voice |
| `anc.strength` | `values`: subset of low, mid, high, adaptive |
| `audio.low_latency` | `type: boolean` |
| `wear.detected` | `type: boolean`, `readOnly: true` |
| `battery` | `readOnly: true`, observed `parts`: left/right/case or headset |

For battery, also declare `batterySource`: bridge, fast-pair or bluez. Only
bridge-sourced battery values enter protocol state. The follower selects the
declared source for new packages; BlueZ remains the fallback when detailed
levels are unavailable. Existing devices retain their current source order.

The host intersects reported capabilities with the model declaration. A control
is writable only after a valid value has actually been observed. Commands
cannot publish state. Soundcore's new codec keeps level/wind parameters as
observations until RX; Nothing's new codec exposes ANC strength only after an
observed strength and keeps stale case level only within the current session.
Those differences do not change either original bridge or its owner's pins.

The host emits `apiVersion`, `values`, `capabilities` and a legacy projection for
existing panel/IPC consumers. It accepts one JSON command per stdin line:
`{"apiVersion":1,"control":"noise.mode","value":"anc"}`.
Protocol-specific spelling remains outside the UI.

## Transports

| Kind | Model transport settings |
|:--|:--|
| `bluez-profile` | Ordered observed `uuidPreference`, intersected with identity UUIDs |
| `rfcomm` | Ordered observed `channels` (1–30) |
| `ble-gatt` | Observed `writeHandle` and `notifyHandle`, plus Fast Pair BLE identity |

Profile `replyTimeout` and `connectTimeout` are D-Bus seconds. Profile delay and
retry, RFCOMM connect timeout/retry, and GATT discovery/registration deadlines
are milliseconds. `connectAttempts` is a count. The legacy JBL subscription
fallback remains explicit in its shared codec metadata. GATT capture records
notification values and successfully submitted client-command payloads, not
HCI packets or proof of over-the-air delivery.

## Owner workflow

Work in an isolated checkout. Release the competing mode bridge before any
probe or live run, and restore its setting afterwards.

```
tools/add-device init brand-model --owner owner-login --address AA:BB:CC:DD:EE:FF --adapter sony
```

Use `--info identity.txt` for a previously saved complete `bluetoothctl info`
record. Omit `--adapter` for a model-local protocol skeleton. Add `--model-id`
for a GATT model. The tool creates an inert draft with unknown parameters,
empty capabilities and failing fault-test placeholders. It never copies another
owner's evidence or invents protocol bytes.

Fill the manifest from this device's replies, then use:

```
tools/add-device capture brand-model --address AA:BB:CC:DD:EE:FF
tools/add-device live brand-model --address AA:BB:CC:DD:EE:FF
tools/add-device session brand-model
tools/add-device check brand-model
```

GATT also requires `--ble-address`. `capture` records an initial probe without
control commands. `live` records `capture.jsonl`, observes initial settings,
exercises declared controls, prompts for a physical/app mode change and repeated
status, sends an unsupported API request, and attempts restoration even after
failure/interruption. `hardware-check.json` records success or failure and a
hash of the actual implementation. Existing evidence files are never silently
overwritten; `--output` selects a new capture path. Archive failed runs before
rerunning, and put the reviewed successful capture at `capture.jsonl`.

The replay skeleton deliberately leaves state expectations unapproved. Review
these against the actual replies and protocol notes, then label coverage cases.
`feed` references a captured RX, command, timer or lifecycle event id; `sent`
references all captured TX ids so far; `expect` is the complete versioned
snapshot, optionally carrying a `case`. Replay preserves every input in order
and checks exact transmitted bytes. Stream replay additionally splits each RX
at every possible boundary. Empty captures, wrong identity, missing TX, skipped
inputs and self-declared legacy exemptions fail.

Required cases include initial state, each writable enum/boolean value and
numeric endpoint, external change, repeated state, unsupported command, and
observed bridge battery/wear readings when declared. A response to a pending
command cannot be labelled external change. Synthetic damage belongs in
`test_adapter.py`, separately from the raw capture. Required fault scenarios
cover silence, disconnect, invalid frame, coalesced input, repeated input and
unsupported commands. Test names alone do not prove coverage: review the tests.

Fill `owner-checks.json` with current implementation hash and concrete shell
integration/reconnect observations, plus wear and non-bridge battery checks when
applicable. Record peer isolation, charging and acoustics as passed, untested or
not applicable with an explanation. Add real protocol notes and the owner's
panel screenshot. These remain owner attestations; software cannot authenticate
that an image or capture came from hardware.

For staged shell testing, temporarily activate the package in an isolated staged
copy. After all checks are complete, set `status: active` in the contribution:

```
tools/add-device sync
tools/check
```

`sync` generates the single BACKENDS block and README device table/gallery.
`check --json`, `check --github` and `check --markdown --summary <file>` expose the same readiness
report to contributors and CI. CI checks all active packages and protects
existing package evidence against modification. Adding a package does not
publish a release or deploy it to the running shell.

## Validation boundary

Sony/JBL native codecs replay their original pins. Samsung, Xiaomi, OPPO and
Bose additionally check frozen wire/state exchanges; all six added stream codecs
exercise fixture fragmentation/coalescing, silence, stop and specific state
regressions. Original bridges and their full owner suites still run separately.
The shared transport tests use controlled endpoints, not a Bluetooth radio.
Physical transport behavior, panel integration and new codec behavior need
owner testing before migration. No supported model has been migrated here.

See [CI checks and actionable feedback](CI-FEEDBACK.md) for automatic command
round-trip, coalescing and session-isolation checks and how to fix failures.
