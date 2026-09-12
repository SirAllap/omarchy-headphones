# Device adapter API v1

Omaphones owns the device process, Bluetooth transport, timers, observed state,
command validation, battery-source selection, restart policy and UI. An adapter
owns only the device's wire protocol. A model supplies the differences that can
be decided from identity before the first frame is sent.

This API is implemented by `omaphones-device`, `omaphones/` and `adapters/`.
Sony and JBL use it. The other five bridges remain on the original
[bridge contract](../BRIDGE.md) through explicit compatibility metadata. Their
protocols, pins and command bytes have not been changed.

## Ownership

| Work | Owner |
| --- | --- |
| Discover BlueZ devices and Fast Pair identity | Omaphones |
| Resolve adapter claims and model parameters | Omaphones registry |
| Register a profile, open RFCOMM, run the GATT helper | Omaphones transport |
| Wait, cancel timers, close descriptors, reap children | Omaphones runtime |
| Interpret headers, checksums, payloads and notifications | Adapter |
| Choose handshake/query frames and protocol retry timing | Adapter |
| Maintain sequence numbers, ACK queue and incomplete input | Adapter |
| Execute a requested protocol delay | Omaphones clock |
| Merge reports, suppress duplicates, validate controls | Omaphones state |
| Store support history and choose a battery source | Omaphones |
| Draw controls, send notifications, pause media | Omaphones |
| Supply observed model variants and evidence | Model author |

Protocol retries are not transport retries. The runtime never automatically
repeats a command. The adapter decides whether another frame is appropriate;
the host runs its timer. Reconnecting starts a fresh adapter and state store.

## Package

```
adapters/<id>/
  adapter.json
  protocol.py
  models/<model-id>.json
  pins/<model-id>.json
  captures/<model-id>.txt
  tests/protocol_test.py
```

Existing owner pins and captures stay in their original directories. Model
records can reference them without moving or rewriting them. Original bridge
executables stay available as regression references and for explicit comparison;
the QML launcher selects the new runtime for Sony and JBL.

`adapter.json` contains `apiVersion: 1`, `id`, `status`, `priority`, `match`,
`entry` and `transport`. `status: draft` excludes a package from runtime routing.
Matching supports exact service `uuids`, a `uuidPrefix`, or the existing
`ble: true` fallback. Lower priorities are considered first. Priorities are
unique, overlapping UUID/prefix claims are rejected, and the BLE fallback is
last. A native GATT adapter can use an exact UUID claim; requiring a BLE address
is separate from claiming every Fast Pair device.

A model contains `id`, `match`, `parameters`, `owners`, `pins` and `captures`.
Match all supplied identity fields: exact reported `name`, Fast Pair `modelId`,
or `uuidSuffix` in the complete advertised UUID list. More than one matching
model is an error. Use the reported name for model identity. The current QML caller retains its
historical display-name fallback when BlueZ supplies no device name; changing
that fallback is a separate routing change, not part of this migration.

Parameters must be declared in the adapter's `parameterSchema`. Supported
parameter types are `boolean`, `integer`, `string` and `array`, with `required`
and optional allowed `values`. The adapter's `unknownModel` and `namelessModel`
are explicit defaults; known model rows are never inferred from another model.
Sony's `wear` flag retains exactly its prior known/unknown/nameless behavior.
Draft model records never participate in selection.

## Adapter

Subclass `omaphones.api.Protocol`. Implement:

```python
class Adapter(Protocol):
    def connected(self):
        # Request only the handshake this device was observed to answer.
        pass

    def received(self, data):
        # Parse bytes; report only what this device actually answered.
        pass

    def command(self, control, value):
        # Encode an already validated request using reported protocol state.
        pass
```

The host calls `on_event(Event(...))`, which returns an ordered list of effects.
The convenience methods accumulate those effects; they do not perform I/O.

| Event kind | Payload |
| --- | --- |
| `connected` | Transport is ready; for GATT, discovery and subscription policy have completed |
| `received` | `bytes`; arbitrary stream chunks for RFCOMM, complete notification values for GATT |
| `command` | `control` identifier and typed `value` |
| `timer` | Token returned by `schedule()` |
| `disconnected` | Transport failure reason |
| `stop` | Clean shutdown |

| Adapter method | Effect |
| --- | --- |
| `write(bytes)` | `Send`: exact bytes on the selected transport |
| `schedule(milliseconds, "method_name", *args)` | `Schedule`: host timer; returns a token |
| `cancel_timer(token)` | `CancelTimer`: remove the pending callback |
| `report(values, capabilities)` | `Report`: validate and merge device observations |
| `finish(code, message)` | `Finish`: end the conversation once |

Timers are one-shot. Repeating a protocol query requires explicitly scheduling
the next query. Timer callbacks name methods on this adapter instance. The first
ending wins, all timers are cancelled, and late events cannot send more bytes.

Exit codes retain the original contract: `0` clean stop, `1` transient connection
or protocol failure, `3` connected and asked but silent, `4` setup failure. Code
3 requires an actual completed transport setup and an unanswered query; a failed
connection or subscription must not retire a model.

The adapter receives only model parameters. It cannot reach QML, another device,
settings, cache files, sockets or the GLib clock through this API. The boundary
checker rejects platform imports and direct file/process operations. This is a
reviewable code boundary, **not a security sandbox for untrusted Python**.

## Capabilities and state

A report declares supported controls and supplies observations. For example:

```python
self.report(
    {"noise.mode": "ambient", "ambient.level": 10,
     "ambient.focus_on_voice": False},
    {"noise.mode": {"values": ["off", "anc", "ambient"]},
     "ambient.level": {"min": 0, "max": 20, "step": 1},
     "ambient.focus_on_voice": {"type": "boolean"}},
)
```

This is an API-shape example, not device evidence.

| Identifier | Shape |
| --- | --- |
| `noise.mode` | Subset of `off`, `anc`, `ambient`, `talkthru` |
| `ambient.level` | Integer `min`, `max`, `step` |
| `ambient.focus_on_voice` | Boolean |
| `noise.wind_reduction` | Boolean; distinct from focus on voice |
| `anc.strength` | Subset of `low`, `mid`, `high`, `adaptive` |
| `audio.low_latency` | Boolean |
| `wear.detected` | Boolean, `readOnly: true` |
| `battery` | `readOnly: true`; `left`, `right`, `case`, `headset`, `charging`, `caseStale` |

The runtime rejects unsupported, out-of-range and sensor-write commands. A
control becomes writable only after its value has been reported. Omitted report
fields keep their prior value within this session. Identical reports do not
produce duplicate output. A fresh connection has no observed values.

Sending a SET or receiving a transport ACK does not establish the new setting.
The adapter must wait for a reply/notification, or schedule a protocol readback.
The host does not change observed values on a command.

The stdout snapshot includes `apiVersion`, `values` and `capabilities`, plus the
legacy fields consumed by existing IPC and panel code. The compatibility
projection belongs to `omaphones/state.py`, not to adapters. stdin accepts
`{"apiVersion":1,"control":"noise.mode","value":"anc"}` per line;
the host also accepts the original bridge commands for manual callers.

The UI knows standard capability names. It does not know vendor command
spellings or model-specific ranges. A new capability needs a deliberate shared
API/UI extension; arbitrary plugin-defined UI is outside v1.

## Transports

All transport parameters describe observed behavior. No dependencies beyond the
ones already used by Omaphones are introduced.

| Kind | Configuration |
| --- | --- |
| `bluez-profile` | `uuidPreference`; optional profile name/path and connection timing |
| `rfcomm` | Ordered `channels`, optional `connectTimeout` in milliseconds |
| `ble-gatt` | `writeHandle`, `notifyHandle`, `addressField: bleAddress`, address type and deadlines |

For `bluez-profile`, `replyTimeout` and `connectTimeout` are D-Bus seconds;
`connectDelay` and `connectRetry` are milliseconds and `connectAttempts` is a
count. For GATT, `discoveryTimeout` and `registerTimeout` are milliseconds.
`allowUnconfirmedSubscription` defaults to false; JBL explicitly retains its
existing five-second fallback policy, followed by the two two-second frame gaps.

The direct RFCOMM transport has deterministic callback tests. No currently
migrated adapter uses it, so its physical Bluetooth behavior still needs an
owner's test before a model is moved onto it.

A GATT transport filters notification handles and validates the reported payload
length before handing bytes to the adapter. A stream transport handles partial
writes and delivers a final readable chunk before disconnect. These are host
responsibilities, not protocol framing rules.

## Author workflow

Work in an isolated clone. Create a draft adapter using an **observed** service
UUID and an available priority:

```
tools/new-adapter <id> --uuid <observed-uuid> --priority <number>
```

`--transport rfcomm` requires observed `--channel` values. `--transport ble-gatt`
requires observed `--write-handle` and `--notify-handle` values. The generator
writes no protocol bytes and creates a failing evidence-test placeholder.

For an already native adapter:

```
tools/new-model <adapter> <model-id> --name '<reported-name>' --owner '<owner>' --parameters '<json>'
```

`--model-id` or `--uuid-suffix` can replace `--name`. The model is a draft until
you add its evidence and explicitly change its status. A legacy adapter still
uses its existing model table and owner workflow; this generator refuses to
pretend that a descriptor changes that old bridge.

Keep the exact capture, then add an API pin with `apiVersion: 1`, `adapter`,
`model`, `owner`, `capture`, `context` and `steps`. A step has one action or
assertion, plus an optional `note`:

| Action/assertion | Meaning |
| --- | --- |
| `event: "connected"` | Opened transport; `stop` and `disconnected` are also available |
| `device: "hex bytes"` | Recorded input; arbitrary chunks for a stream protocol |
| `command: {control, value}` | Typed user request through the runtime validator |
| `advance: milliseconds` | Advance the deterministic host clock |
| `sent: ["hex bytes", ...]` | All exact writes so far, in order |
| `values: {...}` | Complete current observed state |
| `reports: [...]` | All versioned snapshots so far |
| `exit: null/number` | Current exit decision |

`omaphones.testing.Replay` runs the same Session as the live process. Tests under
`adapters/<id>/tests/*_test.py` are discovered by the shared checker. Cover the
applicable [canonical scenarios](CANONICAL-TESTS.md), including spontaneous
changes, unsupported requests, fragmentation/damage, timeout, shutdown and peer
isolation. Label synthetic faults separately from recorded responses. A pin
replay proves behavior against supplied inputs; it is not an independent claim
that those inputs came from hardware.

```
tools/check-adapter <id> --include-drafts
tools/build-adapter-registry
tools/check
```

Activate a package/model only after its evidence and tests are complete. Rebuild
the registry after activation. `Model.js` contains a generated registry block
because both QML and the existing Deno tests load it as a plain script. Do not
edit the generated block. `tools/check` checks that it exactly matches package
metadata. No bridge row or command branch needs to be added by hand.

README/gallery and release metadata remain curated product documentation; adding
a package does not publish, release or validate hardware. Use the actual owner's
screenshot and final-revision test report.

## Migration and review gates

| Stage | Current state |
| --- | --- |
| Event/effect API, host state and three transports | Implemented; automated tests |
| Declarative registry and model parameters | Implemented |
| Sony codec and JBL codec | Native adapters; all existing pins replayed unchanged |
| Capability-based UI commands | Implemented; old bridge spelling translated from metadata |
| Author generators and package checker | Implemented |
| Samsung/Nothing/Xiaomi/Soundcore/OPPO codecs | Legacy process compatibility; separate migration pending |
| Physical Sony/JBL checks of this revision | Pending; earlier hardware reports do not validate this revision |
| Deployment/release | Not performed |

The legacy bridges remain byte-for-byte reference implementations. This makes
comparison inspectable and permits an owner to test both paths. Remove each
reference only in a later reviewed migration, preserving its pins and tests.

Do not mechanically port behavior that contradicts reported-state semantics.
For example, the existing Soundcore bridge reports some level/wind changes
immediately after sending them. Changing that needs a separate behavior decision
and owner confirmation, rather than silently modifying its frozen contract here.

The next hardware check must use this checkout's `omaphones-device` or this
checkout's staged QML. Running `tools/check-live` against the currently active
plugin would test the old code. Release the competing device channel before
starting the staged process, record the revision and initial settings, request
only the intended controls, verify device-reported results, and restore and
verify the initial state. Document reconnect and Fast Pair separately.
