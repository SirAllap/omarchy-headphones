# Owner interview protocol v1

The same runner supports a local browser panel, a terminal, and JSON lines.
No AI-provider SDK or browser automation is required to drive the JSON path.
[JSON Schema](schemas/owner-interview-v1.json) defines questions and answers.
Other status events and the final result have a `type` field as shown below.
The panel shows the current test, the next test and an expandable session plan.
That plan comes from the same ordered controls the runner actually executes;
skipped, failed and unrun steps retain their distinct statuses.

## Start

Use `tools/test-refactor --adapter sony --session SESSION --interview web`
for the local panel, or choose `terminal` or `json`. The usual independent
capture and device preparation in OWNER-TESTING.md are still required.
`tools/owner-interview-demo --interview web` exercises a short simulated
comparison without Bluetooth, device settings or persistent owner evidence.

The browser is ephemeral, binds only to loopback on a random port, and requires
its random session token for data and responses. It serves no session files.
Refreshing the page preserves the token within that tab's session storage;
it is cleared at completion. Closing the page does not submit an answer.
After a grace period the runner ends the interview and restores device state.

## JSON relay

Stdout contains one JSON object per line. Stderr contains diagnostics.
Keep the process alive while the owner responds. Handle these event types:

- `interview-status`: display its `message` where useful. The restoration phase
  announces settings changes that are not another test. The final status lists
  `cleanupRequired`: stop capture and restore the previous plugin setting.
- `owner-question`: display `question` and `choices` for its `device`.
  `context.progress` names the current/next step and the ordered plan. Status
  events also carry `progress` during actions, baseline resets and restoration. Forward
  the original ids in the answer. `context` contains the baseline and control;
  an observation question also references the action/reply timeline entries.
- `input-error`: an answer was rejected; the same question is still pending.
- `test-result`: `report` is the adapter result, including protocol checks,
  restoration and individual `ownerObservations`.

Example answer (copy ids from the actual pending question):

```json
{"type":"owner-answer","version":1,"sessionId":"COPY_SESSION_ID","requestId":"COPY_REQUEST_ID","answer":"unsure","text":"I was distracted","channel":"assistant-relay"}
```

These seven fields are required. A repeatable observation may additionally
include `"next":"repeat"` to save the answer and repeat the comparison. Omit
`next` (or use `"continue"`) to save and advance. The pending question advertises
this with `context.repeatAllowed`. There is no separate decision question. `text` may be empty for choices such as Ready
or Unsure. Done requires a description of the actual physical action; Observed
requires the actual observation. Comments are limited to 4000 characters.
The runner rejects unknown choices, stale ids and a channel mismatch. It assigns
reception timestamps itself. The channel records that the answer was relayed;
it does not cryptographically prove human authorship. No default answer is sent.

## Step behavior and evidence

Readiness → one control → save observation and advance. In the browser, Ready,
Pause and Resume act immediately; they do not require Save answer as a second
click. An observation offers **Save & next** and **Save & repeat** on the same
screen. The first opens the next readiness question, without sending that next
control until Ready. The second saves the observation, immediately restores the
comparison baseline, then asks for readiness before replaying the change.
Pause waits for Resume; Skip leaves the check incomplete. In the terminal,
`quieter | comment` saves and advances, while `repeat:unsure | comment` saves
and repeats. JSON uses the optional `next` field above.
The first attempt uses the current reported state as its baseline. If the state
changes while waiting, the runner asks again with the new baseline. A physical
step separately records instruction, completion and independent observation.

Every question and answer is a JSON object inside the description column of
`actions.csv`. Question publication and readiness are notes, never claimed
physical actions. The outer CSV supplies its existing ordered timestamps; the
JSON carries ids, attempt, phase and receipt clock values. An observation keeps
its original comment. It cannot be rewritten by a later attempt. Schema v1
covers these question/answer objects; generic status/action records retain their
own types. The existing seal protects the entire CSV, with no sidecar loophole.

UI answers are distinct from adapter reports. Unsure is inconclusive. Skipped
checks make the run incomplete. Protocol `passed` never grants acoustic approval;
listen comparisons are reported individually and general acoustic performance
remains untested. A command failure goes directly to restoration without another
required question. It stays a failure regardless of earlier owner observations.

## Boundaries

The runner restores and closes its adapter on Stop, failed command, input loss
or owner timeout. The assistant owns btmon and temporary plugin settings and
must clean those up on every exit. The panel cannot manage arbitrary processes
or change desktop settings. It prints no install instructions and needs no
additional dependency.

The previous Sony voice-focus timeout and strict capture timestamp rejection
remain tracked separately. This implementation does not modify their packets,
old pins, recordings or failed outcomes.
