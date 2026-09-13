# CI checks and author feedback

Run `tools/check` locally. The GitHub workflow runs it with Python 3.12 and
current Python 3.x, using different hash seeds. This catches version assumptions
and accidental dependence on unordered collection iteration. Each job has a
15-minute ceiling; a hung parser/test must not hold a runner indefinitely.

For one model, run `tools/add-device check <id>`. JSON and Markdown reports
contain the same failure, source file, suggested fix and reproduction command.
`--github` emits file annotations using GitHub's
[workflow command format](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#setting-an-error-message).
The workflow also appends the report to its job summary, even when an earlier
check failed. Malformed manifests are reported independently so one broken
package does not hide the other packages' results. Inert drafts are skipped by
the repository check; check a draft explicitly by id while developing it.

## Additional automatic checks

| Check | What fails | Author action |
|:--|:--|:--|
| Command round trips | A declared control value has no accepted command, subsequent wire write and explicit fresh RX observation of that control value | Record requests one at a time and wait for replies. Fix command encoding/readback; an initial or cached value is insufficient. |
| Coalesced delivery | Combining causally adjacent stream RX changes writes, reports or exit status | Keep incomplete input in a buffer; consume all complete frames in each received() call. |
| Session isolation | Interleaved instances diverge, or fresh instances differ from an independent replay | Move mutable counters, buffers and pending requests into each instance; remove nondeterministic output. |
| Owner evidence preservation | An existing device package, pin or raw capture was edited/deleted relative to the baseline | Restore the named original evidence; contribute new evidence separately. |
| Package diagnostics | Invalid JSON, forbidden protocol imports or missing evidence | Follow the file/line annotation and the indicated local check. |

Coalescing never crosses a captured TX, command, timer or lifecycle boundary.
It is inapplicable to GATT notifications. A capture with no mergeable input is
reported as such; the model's explicit coalescing fault test is still required.
Automatic scenarios are synthetic transformations of recorded input, not new
hardware evidence. The round-trip check proves ordering and observed values;
it cannot establish protocol-level causality without the reviewed wire fixture.

Example failure:

```
FAILED synthetic: command round trips
File: devices/synthetic/capture.jsonl
No command followed by a wire write and explicit RX observation for noise.mode:off.
Fix: record that request and its actual reply; a case label is insufficient.
Run: tools/add-device check synthetic
```

Replay errors identify the session step; malformed capture JSON identifies its
line. Boundary errors identify the actual shared or local protocol source line.
Annotations escape author-controlled text so a filename or error message cannot
create a second workflow command. Summaries include the fix, not only a red mark.

Existing checks still cover exact owner pins, fragmentation, capability coverage,
model fault scenarios, current owner reports, restoration, generated registry and
gallery, import boundaries, UI tests, syntax and shell validation where available.

Useful later additions would be a worker time limit per package (so one hanging
model still leaves a report for other models), targeted mutation testing of
model fault suites, and headless QML process-handoff tests with a mocked service.
These are not implemented by this change. Physical reconnect, acoustics and
actual battery/charging behavior remain owner checks.
