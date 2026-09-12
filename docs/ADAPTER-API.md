# Add your headphones: device API v1

The contribution is your model. Keep its identity, configuration, observations,
regression session and owner report in `devices/<id>/`. Existing bridge scripts
and owner pins stay where they are. A new profile must not claim an already
supported owner's reported name.

## Start with the device

Use an isolated checkout, outside the running plugin and its Git metadata.
Run `tools/add-device` for the interactive guide, or supply recorded identity:

```sh
tools/add-device init brand-model --owner YOUR_LOGIN --info /tmp/headphones-info.txt
```

The input is complete `bluetoothctl info ADDRESS` output. You can instead pass
`--address ADDRESS` to read it live. The reported Name is used, not the writable
Alias. All configured UUIDs and an exact reported name must match. BLE profiles
also require the six-digit Fast Pair model ID (`--model-id`). A matching UUID
suggests an adapter; it is not proof that controls work.

The guide creates one package and refuses to overwrite it:

```text
devices/brand-model/
  device.json          identification, adapter parameters, declared capabilities
  identity.txt         complete device info
  protocol.md          observed protocol, firmware and limits
  test_adapter.py      model-specific fault scenarios, initially skipped
  owner-checks.json    manual observations, initially untested
  adapter.py           only when no existing protocol fits
```

Capture, replay, live testing and a real panel screenshot add:

```text
  probe.jsonl          initial query/reply capture (optional)
  capture.jsonl        control session, including raw RX/TX and reported state
  session.json         reviewed expectations referencing capture events
  hardware-check.json  machine-written adapter check and restoration results
  screenshot.png       owner's panel screenshot
```

V1 uses the existing listening-mode contract in `BRIDGE.md`: a supported model
declares at least one listening mode. Fast Pair and BlueZ battery-only devices
continue to work through the existing shared reader without a mode adapter.

## Describe only what this model supports

This is a **fictional shape example**, not a supported device or protocol claim:

```json
{
  "apiVersion": 1,
  "id": "example-headphones",
  "model": "Example Headphones",
  "owner": "your-login",
  "match": {
    "names": ["Example Headphones"],
    "uuids": ["aeac4a03-dff5-498f-843a-34487cf133eb"]
  },
  "adapter": {
    "builtin": "nothing",
    "transport": "classic",
    "parameters": {"channels": [28]}
  },
  "capabilities": {
    "modes": ["off", "anc", "ambient"],
    "ancLevels": ["low", "mid", "high", "adaptive"],
    "latency": true,
    "battery": {"source": "bridge", "parts": ["headset"]}
  }
}
```

Do not copy that channel or those capabilities into your profile without your
own evidence. During development the declarations are hypotheses; the package
checker requires confirmed replies before accepting the contribution.

Builtin adapters are `sony`, `nothing`, `soundcore`, `samsung`, `xiaomi`, `oppo`,
`bose` and `jbl`. These model parameters must be explicit:

| Adapter | Parameters | Meaning |
|:--|:--|:--|
| Sony | `wear` (boolean) | Whether this model is asked for its wear sensor |
| Nothing | `channels` (integer array) | Only the observed RFCOMM channel(s) for this model |
| Bose | `channels` (integer array) | Only this model's observed BMAP channel(s), without changing QC45's discovery |
| Soundcore | `offset` (integer), `query` (boolean) | State block offset and whether to send the extra mode query |
| Others | Empty object | Existing protocol with its existing startup behavior |

The remaining startup queries, parsing and timers belong to the selected
implementation. If your capture does not support that behavior, use a local
adapter; do not quietly alter another model's bridge. The builtin wrapper
loads a fresh module in the new device's process and configures that module
only. No original `MODELS` row or fallback changes.

Optional capabilities use existing shell controls:

| Field | Shape |
|:--|:--|
| `ambient` | `{ "min": 0, "max": 20, "voice": "Focus on voice", "voiceCommand": "voice" }`; Soundcore uses `wind` |
| `ancLevels` | Subset of `low`, `mid`, `high`, `adaptive` |
| `latency`, `worn` | Boolean; a live device reply is still required |
| `battery` | Source `bridge`, `fast-pair` or `bluez`; parts `headset` or `left`, `right`, `case` |

The wrapper filters controls and state to the profile's capabilities. It never
publishes a requested setting as a reported setting, and rejects controls until
the device has answered. A genuinely new type of control needs a separate
contract/UI change; it cannot be smuggled into arbitrary JSON.

## Capture and test your device

The running plugin holds the mode channel. Save its current `useModeControl`
setting, switch it off before opening another bridge, and restore the setting
afterwards. Do not stop the Bluetooth daemon or operate another owner's device.

```sh
tools/add-device capture brand-model --address ADDRESS
tools/add-device live brand-model --address ADDRESS
```

`capture` only asks initial queries and saves `probe.jsonl`; it sends no control
commands. `live` runs **this checkout's adapter**, saves `capture.jsonl`, reads
all initial control values, exercises declared controls, and independently
attempts to restore each setting in `finally`, with the original mode last.
Unknown initial state aborts all control writes. Failures, interruption and
failed restoration make the report fail. A control already in the desired
reported state needs no redundant write.

Both commands refuse existing capture files. To retain an earlier recording,
use `--output /tmp/another-session.jsonl`; select the final reviewed session as
`capture.jsonl` in the package. Likewise retain an old hardware report outside
the package before repeating `live`. BLE adapters require `--ble-address`
from this device's Fast Pair stream; never derive it from the Classic address.

Capture JSONL records have a unique `id`, monotonic receipt `timeNs`, `direction`,
`encoding` and `data`. Directions are `metadata`, `rx`, `tx`, `command`, `state`
and `external`. Stream RX/TX is hex **as read/written**, retaining partial and
combined chunks. TX records successful writes. JBL records the explicitly
labelled `client-line` boundary of btgatt-client; its client commands are not
claimed to be captured HCI packets. The metadata identifies the device, UUIDs,
adapter implementation hash and boundary. Keep negative results and record any
redactions or omitted intervals in `protocol.md`.

## Review and replay the session

```sh
tools/add-device session brand-model
tools/add-device check brand-model
```

The session command prepares references to the recording, but leaves expected
state as `null`. Fill each expectation from your interpretation of the actual
replies and label the relevant cases printed by the guide. It does not approve
the bridge's own output as its specification.

Each step has one action/assertion, with optional `note` and `case`:

| Step | Meaning |
|:--|:--|
| `{"start": true}` | Start the opening exchange in the offline transport double |
| `{"rx": "event-id"}` | Feed the exact recorded transport input |
| `{"command": "set anc"}` | Request a control; state must not change before RX |
| `{"advance": true}` | Fire the next batch of recorded test timers, where supported |
| `{"advance": 2000}` | Advance a socket adapter's deterministic clock by milliseconds |
| `{"tx": ["id-1", "id-2"]}` | Assert **all** outgoing data so far against captured TX, including ACKs |
| `{"expect": {...}, "case": "mode:anc"}` | Assert complete state with a capability coverage label |

Stream TX comparison preserves all bytes and their order without assuming one
system write equals one packet. RX references cannot point to TX. State labels
such as `mode:anc`, `ambient:20` and `battery:left` must match their asserted
values. The runner also repeats the complete session with every recorded stream
chunk split at every byte boundary. This is synthetic fault injection, never a
claim that the device sent a damaged frame.

For `external-change`, record a change made on the headphones: its unsolicited
reply where supported, or its subsequent polled reply on devices such as Bose.
Say how the change was made in the session note. Use repeated real replies for
the repeated-input case. Unsupported
commands must cause no additional TX. Implement the six scaffolded fault tests
using your model's own data: silence, disconnect, invalid frame, coalesced input,
repeated input and unsupported command. Skipped or expected-failure scenarios
are incomplete. Tests must drive the actual timer/run-loop/transport callback;
calling `finish(3)` does not test silence. The common replay Session can be
created with `adapter_api.replay.factory(profile, directory, capture_metadata)`.
Builtin sessions expose `bridge`, `module` and `session` for model-specific
fault injection. Nothing and Bose run their actual socket loops with a fake
clock and socket; `advance` wakes those loops, which execute their own deadline
and readback logic. Add explicit time steps where the selected recording has
timer-driven TX. The scaffold does not guess elapsed-time steps from printing
delays. Bose replay exercises the real BMAP init validator on one open channel;
connection attempts and candidate cleanup belong in its model fault tests.

## When your device needs its own adapter

Use `"module": "adapter.py"` instead of `"builtin"`. Keep its observed
parameters in the profile. `adapter.py` exports:

```python
API_VERSION = 1

def run(context):
    # Hold this device's link and implement BRIDGE.md, including cleanup.
    # Read context.address / name / uuids / ble_address / model_id.
    # context.profile["adapter"]["parameters"] belongs only to this model.
    # context.record("rx", chunk) records actual transport receipt.
    # context.record("tx", chunk) records successful transport writes.
    # context.record("command", line) records stdin commands for replay.
    # context.emit(state) filters, records and prints device-reported state.
    # Return 0, 1, 3 or 4, as BRIDGE.md specifies.
    raise NotImplementedError

def replay(context):
    # Return the offline Session below, using the same parser/command logic.
    raise NotImplementedError
```

The offline Session provides `start()`, `receive(bytes_or_client_line)`,
`command(line)`, `advance()`, `wait_sent(count)` and `close()`, plus `sent`
(all bytes/lines sent so far) and `lines` (all reported states). It must not
open real sockets, D-Bus or child processes. Use `context.emit()` with its
`output` callback set to collect state. The runtime and replay share the same
protocol implementation. No production code imports the test harness.

The launcher handles profile/identity checks, parent death and error reporting.
The local adapter owns transport shutdown, signal handling, EOF, EPIPE and its
timers. Check `context.mode_seen` and `runtime.allowed(context.profile, line)`
before executing a control. Record before parsing RX, after successful TX, and
before processing a command. See `BRIDGE.md` and canonical coverage for the full
contract; a Python file that merely returns exit codes is not an adapter.

## Finish the contribution

`hardware-check.json` proves only the owner-run adapter check. It includes an
implementation hash; editing runtime code, parameters or capabilities makes it
stale. Generated entries for unrelated models do not change that hash.

In `owner-checks.json`, copy that hash and record actual observations for
`shell-integration` and `reconnect`. Wear edges and battery supplied by Fast Pair
or BlueZ need their own observations too. Each check has `status` and `evidence`
(a concrete account of requested action, reported result and restoration, with
capture references where applicable). This is an **owner attestation**, not an
independent hardware test by CI. Record `peer-isolation`, `charging` and
`acoustics` as passed, untested or not applicable with a reason. An absent second
headset means peer isolation is untested. Never mark it passed by omission.

Exercise the actual checkout in the shell for the integration check, preserve
the user's settings, then take `screenshot.png` using the gallery workflow.
Do not fabricate a panel or use another owner's screenshot.

```sh
tools/add-device sync
CHECK_BASE=origin/main tools/check
tools/add-device check brand-model --markdown
```

`sync` generates the runtime registry and the new-model README table/gallery;
include those generated changes in the PR. The existing gallery and supported
models are retained. `check` reports identity, captured-byte replay, capability
coverage, fault tests, owner hardware, integration limits and gallery separately.
CI publishes the same report in its job summary, runs all existing tests and
rejects edits/removals in established owner packages and pins.

Send the package and generated files in your PR. If the implementation needs
changes after owner testing, repeat affected tests on the final executable and
refresh the hardware report. A green replay is not hardware confirmation.
