# GitHub message template — owner validation of the refactor

Draft for all headphone PR authors. Before sending, replace the placeholders
with the author's name/model, the published candidate SHA and its documentation
link. Do not send a moving branch name as the only revision identifier. Send
the same workflow to every author, with model-specific notes where needed.

---

Hi @AUTHOR, thanks again for contributing support for MODEL!

I've been refactoring Omaphones so Bluetooth transport, timers and state handling
are shared, while each model's protocol and evidence stay separate. Before
releasing it, I'd like your help testing your headphones: I can test my Sony
WH-CH720N and JBL TUNE230NC TWS, but I don't have your model.

Please paste the prompt below into your coding assistant to test candidate
**CANDIDATE_SHA**. It will prepare the isolated clone, run the tests, manage the
recordings and assemble the results for you to return, including failures.
You'll need your headphones nearby to perform physical actions and describe
what you observe. The detailed workflow is in **OWNER_TESTING_LINK**.

We're also keeping an independent BTSnoop recording and a timestamped log of
what you did and observed. That will let us revisit unknown packets and test
future changes without repeatedly asking you to record the same scenarios.

Paste this into your coding assistant:

```text
Help me test my MODEL headphones against Omaphones candidate CANDIDATE_SHA
from https://github.com/ncr/omarchy-headphones. My GitHub login is AUTHOR.

Lead the session from preparation through the return report. Run the commands,
manage recording and logging, inspect failures and prepare the result files.
Ask me for missing information, physical actions and independent observations,
one short step at a time. Ask for system authorization only when needed. Don't
make me copy commands or edit JSON/CSV when you can do that work yourself.

Use a separate clone checked out at that exact commit. Read AGENTS.md and
docs/OWNER-TESTING.md there, then run tools/check and save the results. Confirm
my device's reported name, full identity and applicable adapter. Don't infer
model capabilities or transport settings from another pair of headphones.

Prepare a new owner session with tools/owner-session. Record firmware (or
unknown), controller, environment and the candidate revision. Save the existing
useModeControl setting, release the competing installed mode bridge,
and start an independent btmon BTSnoop recording before connecting. Run btmon
in the foreground of a persistent interactive terminal (PTY), retain its session
handle, and authorize startup if required. Stop it with Ctrl+C in that same
terminal and wait for exit; never detach it or use a separate privileged stop
command. Confirm this terminal lifecycle is available before capture begins.
Keep Fast Pair enabled; for GATT use this device's observed model id and BLE address.

Run tools/test-refactor for this model with --interview web if I can use a local
browser, otherwise --interview json so you can relay its structured questions.
Use --interview terminal if that fits your harness better. Read
OWNER-INTERVIEW.md. Keep the process available for responses. Ask exactly the
pending question with its choices; submit my actual answer using its request
and session ids. Never answer Ready or a physical observation on my behalf.
The runner gates individual changes, retains Unsure/Repeat, and announces
restoration. Ask me to perform the physical/app actions and describe their
actual effects. Log actions and observations
separately, with timestamps; use start/end markers when exact action timing
is unknown. Do not fill observations from your own interpretation of packets
or claim acoustic effects without my confirmation.

Keep failures, interrupted runs and restoration results. Label battery,
reconnect, wear, charging, acoustic effects, peer isolation and candidate shell
integration as passed, failed, untested or not applicable, with evidence. A
test of the installed plugin is not a test of the candidate QML. Do not modify
or reload the running shell, replace the installed plugin or operate another
device as an incidental part of this test.

Restore the original device settings and useModeControl setting, stop
recording, and check/seal the session. If anything fails, retain the incomplete
files and error. Review the capture for unrelated private data before preparing
the return archive; document redactions and preserve the private original.

Prepare a reply for me to post with the exact revision, model/firmware,
environment, automated results, manual observations, restoration outcome and
limitations, plus the session files listed in OWNER-TESTING.md. Don't send or
upload anything automatically. Don't edit old owner pins or turn a failed
check into a pass by changing its expectations.
```

Even an incomplete test is helpful—return the assistant's report and reviewed
session archive, including where the process got stuck. I'll review the
recordings, fix any issues and request a retest where needed before deciding
on the release.

Thank you!
