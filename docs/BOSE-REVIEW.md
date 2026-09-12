# QC45 review and owner confirmation

The original pin, capture and tests are unchanged. Review tests supplement
them with explicitly synthetic transport faults and recorded reply payloads.

## Repair scope

- Init requires STATUS with a version-shaped payload, rejecting GET echoes,
  ERROR and malformed replies. Bytes after init are retained for parsing.
- Probe reset/EOF is transient. Failed candidates close; a clean stop closes
  the candidate and prevents remaining attempts or retry waits.
- Tests drive real loop timeout, scheduled readback, reset and stdin EOF.
- The raw session tool logs RX chunks at receipt, keeps split frames, reads
  initial mode before writes, restores it on interruption and checks readback.
- The diagnostic Profile1 probe drains its queue and closes the descriptor.
- README, manifest and bridge list include Bose. Protocol text no longer
  claims latency or evidence absent from the stored capture.

## Evidence limits

The [original capture](captures/bose-qc45.txt) contains decoded 90% battery
and mode 0–3 replies, including return to mode 1. In the
[owner confirmation](https://github.com/ncr/omarchy-headphones/pull/13#issuecomment-5648598888),
@Driskol explicitly identified the pin's `59 ff ff 00` (89%) battery sample
as synthetic, derived from the real 90% reply. It tests battery changes and
is not an observed reply. The original pin and capture remain unchanged.

Commit `d663c6d` adds a [raw session](captures/bose-qc45-session.txt) recorded
with review revision `fc8d7f9`: init, 100% battery, the complete GET-All burst,
mode 0–3 readbacks and verified restoration to initial mode 1. Its raw chunks
decode to the 25 listed frames without trailing bytes. The separate
[Profile1 traces](captures/bose-qc45-channels.txt) record the DETECT prelude
on the deca-fade UUID and silence on the other vendor UUID.

The complete `bluetoothctl info` output mentioned in the owner's comment
is absent from these files. The vendor UUIDs have connection traces, but the
full UUID list in the routing test lacks a stored SDP listing. The maintainer
explicitly accepted this evidence gap for 1.3.2. This is a documented exception
for this contribution, not a replacement for the canonical capture requirement.

Channel 8 is confirmed. Candidates 2/9 are exploratory, not proof of another
model's support. Charging, acoustic effects, peer isolation and behavior while
custom slots are configured are unconfirmed. The runtime still exposes only
Quiet/ANC and Aware/Ambient. The original screenshot is retained.

## Owner test on the repaired revision

On 2026-09-12, @Driskol confirmed the following on QC45
`AC:BF:71:64:56:B9` with `fc8d7f9`:

1. Connection and bridge battery reporting worked; status showed 100%.
2. Both ANC and Ambient commands worked, with subsequent `modeFor` readback.
   The physical Action button changed the mode and the next poll followed it.
3. Disconnect ended the bridge with exit 1 and left no process. Reconnect
   started a fresh bridge and restored working control.
4. Starting the shell with `useModeControl: false` started no bridge and left
   channel 8 available to the capture tool. Restoring the setting started a
   new bridge and restored control. This report does not separately demonstrate
   interruption during an in-flight socket connection; synthetic tests cover
   stopping during connect and probe.
5. The raw session restored and verified the initial mode. The owner confirmed
   restoration of the original widget setting and headphone mode (Aware).

Reported state was checked through the follower's IPC (`modeFor`), not by
visually inspecting the rendered panel on the repaired revision. The original
screenshot remains available; this PR does not change the panel's QML.

The maintainer accepted the owner confirmation and the documented limits for
1.3.2. The final review update changes documentation only, so it does not
require another hardware run. Local checks passed 156 Python tests and 57
Model.js tests, QML lint and plugin validation; the full PR CI check also
passed on `d663c6d`. These software results do not replace the owner's test.
