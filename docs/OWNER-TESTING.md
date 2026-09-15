# Test the adapter refactor on your headphones

Test the exact candidate revision named by the maintainer in an isolated clone.
This workflow runs that clone's adapter directly. The installed plugin retains
its current routing; `tools/check-live` against the installed plugin does not
test this candidate's QML. A successful adapter result is one part of migration
review. Shell integration and a release remain separate steps.

## The coding assistant leads the session

Every contributor is expected to use a coding assistant. The contributor starts
with the prompt in [the GitHub message](CONTRIBUTOR-REFACTOR-MESSAGE.md). The
steps and command examples below are the assistant's operating instructions.

The assistant prepares the clone, identifies the device, runs commands, manages
capture processes and timeline files, checks results, restores settings and
assembles the return archive and draft reply. It gathers information available
on the machine before asking the owner. It asks the owner one short question or
physical action at a time and handles terminal input for the test tool using
the owner's actual responses. It never supplies an observation on their behalf.
The shared interview records the exact question, answer, attempt and timestamps.
Use the browser panel when available, or JSON relay for an assistant-led chat.

The owner has the headphones nearby, presses buttons, handles the case or
charging cable, performs app actions the assistant cannot access, and reports
what they see or hear. They provide missing information and any necessary
system authorization, then review and return the prepared results. Copying
commands, editing data files and interpreting protocol bytes belong to the
assistant's work, not the owner's checklist.

## 1. Prepare the candidate and record identity

Run `tools/check` in the candidate clone and retain its output. Confirm that the
reported model and adapter match your contribution. For a new, unrecognized
model, prepare a draft with `tools/add-device` and the model's own observed
parameters; do not treat a matching brand as proof of protocol compatibility.

Save the complete `bluetoothctl info ADDRESS` output to a file outside the
installed plugin. Record firmware from a source that actually displays it, or
use `unknown`. Find the controller with `bluetoothctl list` and `btmon --help`.
For JBL's GATT path, also record the current Fast Pair model id and rotating BLE
address from Omaphones' Fast Pair state. The latter can be read with
`omarchy-shell omaphones bleAddressFor ADDRESS`. Never borrow these values from
another device. Keep Fast Pair battery reading enabled during the adapter test.

Save your current Omaphones **Noise cancellation (Off / ANC / Ambient)** (`useModeControl`) setting and turn it off
before testing: the installed bridge otherwise holds the same channel. Restore
that setting after stopping the candidate. Do not restart the shell or Bluetooth
service, replace the installed plugin, or change another device's settings.
Mode changes and explicit reconnect tests can briefly affect audio.

## 2. Start independent recording before connecting

Use a fresh directory for each run. Example commands below use `/tmp/owner-run`;
copy completed results somewhere durable after review.

```bash
tools/owner-session init /tmp/owner-run --owner YOUR_GITHUB_LOGIN \
  --info /tmp/headphones-info.txt --firmware unknown --controller hci0 \
  --scenario 'Candidate adapter: initial state, controls, manual changes, restoration'
```

For GATT, add `--model-id OBSERVED_ID` at initialization. Disconnect the selected
headphones if necessary, then start the printed `btmon` command in another
terminal. It writes **BTSnoop**, independently of our parser and runtime. Leave
it running through the session, including connection/setup and the final
disconnect. Capture the controller hosting the device under test. A capture on
this computer does not observe a separate phone-to-headphones link; capture on
the phone as well if that exchange is part of the question being tested.

## 3. Record what you do and what you observe

Use the same clock for the packet capture and action log. Before an action,
record a start marker; immediately after it, record completion and the observed
effect. This gives an honest interval when the exact physical transition time
is unknown. The CSV timestamp is the time the note was entered, not a sensor
measurement of the button press. Do not invent retrospective timing.

```bash
tools/owner-session mark /tmp/owner-run action 'Start: connecting the headphones'
bluetoothctl connect ADDRESS
tools/owner-session mark /tmp/owner-run action 'End: connect command completed'
tools/owner-session mark /tmp/owner-run observation 'Connection tone heard; audio resumed'
```

Use `failure` for a failed operation and `note` for context. Describe the actual
button/app, selected value and device. For case, charging and wear tests, record
each physical action. Keep “selected ANC” separate from “heard noise reduction”
and report uncertainty. Automated adapter output is logged as an adapter note,
never as an owner's physical observation.

## 4. Run the candidate adapter

```bash
tools/test-refactor --adapter sony --session /tmp/owner-run --interview web
```

Choose the applicable adapter: `sony`, `jbl`, `samsung`, `xiaomi`, `oppo`, `bose`,
`nothing` or `soundcore`. For `jbl`, also supply `--ble-address OBSERVED_ADDRESS`
and `--model-id OBSERVED_ID`. Re-read the BLE address after reconnecting; it can
rotate. `--automatic` runs control checks without prompting for physical
actions and leaves those checks explicitly untested.

Choose `--interview web`, `--interview terminal` (default), or `--interview json`.
The browser URL is printed on stderr; open it in a local browser on the Bluetooth
host. The panel has no remote service or additional runtime dependency. The
terminal accepts `choice | optional comment`. JSON mode emits one object per
line on stdout and accepts one response object per line on stdin. See the
[interview protocol](OWNER-INTERVIEW.md) and its schema. The assistant may render
each pending JSON question using its own question tool or relay it in chat.
It must submit only the owner's actual choice and text, with the matching ids.
Keep the process available for input while waiting; do not start a second tester.

The tester reads initial state before sending control changes. It waits for
Ready before each control, then asks about that single change. Unsure is valid.
Repeat retains the answer, restores that attempt's comparison baseline, and
asks for readiness again. Pause stops progression; Skip leaves that check
incomplete. Restoration is visibly separate from listening observations.
The external mode-change step records the owner's action and observation. It attempts to restore all initial writable
settings, restoring mode last, including after a failure or interruption.
`--owner-timeout 180` limits each answer wait (seconds; default 180). Closing
input, an idle timeout or Stop ends the test as incomplete and restores the
adapter settings. Browser disconnect is noticed at the next question and has a
90-second grace period for reopening the page. The current device operation is
bounded by its existing timeout; Stop takes effect after it returns.

The assistant still owns the independent btmon process and the temporary plugin
setting. On **every** test exit, including timeout and UI loss, it must stop the
capture, restore the original `useModeControl` setting and report the outcome.
The tester does not own those external processes/settings and cannot claim to
have cleaned them up. Its final `cleanupRequired` status names both actions.
If restoration fails, use the original controls/app to restore the settings and
record what happened. Never overwrite a failed run with a successful one.

WH-CH520 is a battery-only exception: the candidate confirms that mode control
remains unavailable, and sends no ANC/wear queries after the handshake. Its
battery still needs a separate observation of the Fast Pair/BlueZ path.

Per-attempt answers appear in `adapter-result.json` as `ownerObservations`;
structured question/answer records also live in `actions.csv`, inside the
existing source checksum boundary. Protocol `passed` is not acoustic approval.
General acoustic performance remains untested; each recorded comparison is
available separately, including uncertainty. Guided runs leave repeated-report
and unsupported-command verification untested until dedicated assertions exist.

Keep explicit manual results for reconnect, battery, wear when supported,
charging, acoustic effects and peer isolation. Mark each `passed`, `failed`,
`untested` or `not-applicable`, with concrete observations. For a reconnect
retest, use another fresh session and compare initial/final values with the
previous run. The adapter must reacquire device state after reconnect; an old
cached value is not evidence of a new reply. Report shell integration as
untested until the candidate QML itself is staged and tested with the owner.

## 5. Stop, check and return the evidence

Stop the candidate, restore the previous useModeControl setting, record
the final device state and completion of your scenario, then stop `btmon` with
Ctrl+C in its terminal. Do not append to a capture once it has been sealed.

```bash
tools/owner-session seal /tmp/owner-run \
  --notes 'Describe completed scenarios, failures, untested checks and any omissions'
tools/owner-session check /tmp/owner-run
```

The check validates format, file integrity and timeline structure. It rejects
known packet loss, truncation and backwards timestamps; it cannot prove that
every scenario was performed, every packet was captured, or that the owner
heard an acoustic effect. Hashes detect later changes, not capture authenticity.
If sealing fails, retain and return the incomplete run with the error as well.

Return these files together:

- `traffic.btsnoop`: the original binary recording, including unknown packets.
- `actions.csv`: timestamped actions and independent observations.
- `session.json`, `identity.txt`, `manifest.json`: device, firmware, environment,
  exact revision/code fingerprint, clock origin, scope and source checksums.
- `runtime.jsonl`, `adapter-result.json`: candidate runtime events and control /
  restoration results. Keep them as supplementary evidence; the source recording
  remains BTSnoop.
- Test-suite output and your concise manual pass/fail/untested report.

Before sharing, inspect the recording for unrelated device traffic and secrets
such as pairing keys. Keep the original private if needed; identify any
redaction and preserve a separately named derivative with its own checksums.
Do not silently replace bytes in an existing owner's capture. Attach a reviewed
archive to the PR when supported; otherwise ask the maintainer for a suitable
transfer location. Failed and incomplete results are useful too.

## Deriving future tests without recording again

Retain the sealed bundle under a new model package's `source-session/`. Read it
in `btmon` or Wireshark; `tools/owner-session packets DIRECTORY` also exposes
every record, its original file offset, direction, timestamp and raw bytes.
Record the independent interpretation in protocol notes before writing expected
states. Human actions and observations provide the context for those states.

`tools/owner-session extract DIRECTORY --direction rx --slice 12:7:10` selects
10 bytes at zero-based byte offset 7 in one-based packet 12. Repeat `--slice`
for a fragmented payload, in wire order. These numbers are an example, not
offsets for any headphone model. Use the actual capture's framing. This is
explicit extraction, not automatic RFCOMM/L2CAP/GATT reassembly.

The output includes a `source` reference (BTSnoop SHA-256 plus packet slices).
Attach that reference to the corresponding RX/TX event in the derived
`capture.jsonl`; it does not modify the binary source. Package readiness checks
that every RX/TX byte matches its source and that source identity matches the
owner/model. Derived command/timer events and reviewed `session.json`
expectations remain test-harness inputs, not independent packet evidence.
Existing historical pins/captures remain intact; they do not become complete
BTSnoop sessions by conversion or by changing their label.

References: [BlueZ btmon](https://github.com/bluez/bluez/wiki/btmon),
[Wireshark capture formats](https://wiki.wireshark.org/FileFormatReference).
