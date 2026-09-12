# Working on Omaphones

Read this before changing anything. It is short because the details live in
the files it names.

## What this is

A plugin for the Omarchy bar: one icon per connected pair of headphones,
filling with the battery, and a panel that switches Off / ANC / Ambient.
`Service.qml` follows the connected devices, `DeviceFollower.qml` holds one
device — its Fast Pair battery reading and its mode bridge — `Panel.qml`
draws, `Model.js` decides (no QML in it, so it runs under Deno in the
tests). The battery comes from `gfps-reader` over Fast Pair; the listening
mode comes from one `<brand>-bridge` process per device, a Python script
that holds the device's own channel. `PROTOCOL.md` records what every
device was seen to say; `BRIDGE.md` is what every bridge promises the shell.

## The fact that shapes everything

Nobody has more than their own headphones. The maintainer cannot test your
model, you cannot test anybody else's, and every model in README's table
works today on frames that only its owner can retest. Two rules follow, and
a pull request that breaks either is sent back:

1. **A new model may not change what an existing one is sent.** Existing models
   have their rows in `MODELS`/`BACKENDS` and frozen
   owner sessions in `tests/pins/<brand>/<model>.json`. New models have an
   isolated `devices/<model>/` package under the device API. Adding yours
   adds its own configuration and evidence. It does not edit another owner's
   row or pin, and it does not
   turn something that was always sent into something now decided. Where a
   decision is unavoidable, widen: `UNKNOWN` gets the wider behaviour,
   known models keep theirs.
2. **Ship only what you saw the headphones answer.** No bytes from a vendor
   table your headset never answered — not in the code, not in
   `PROTOCOL.md`. Existing pins retain their `docs/captures/` evidence. New
   packages
   keep exact transport RX/TX in `capture.jsonl`, referenced by `session.json`;
   a frame the adapter parses should be in there.

## Canonical owner examples

JBL TUNE230NC TWS and Sony WH-CH720N are the canonical reference models,
prepared and hardware-tested by the maintainer @ncr on his own headphones.
Read `docs/CANONICAL-TESTS.md` before adding support. Their `*-canonical.json`
pins, packet evidence, bridge fault tests, Fast Pair tests and live check show
the expected coverage. Match coverage for the capabilities your model has;
use its own replies, never borrowed bytes. Keep original pins intact and label
synthetic damage separately from observed protocol evidence. Other owners'
pins remain equally binding. A capability not tested is documented as such.

The expanded coverage requirement applies to new models and brands only.
Existing supported models keep their current tests and evidence; owners do
not have to fill historical gaps to remain supported. A change to an existing
model must test the changed behaviour and be confirmed by its owner, without
requiring a complete coverage retrofit. Existing pins remain binding.

## One command

```bash
tools/check
```

Runs everything a pull request is held to: the bridge tests and pins, the
`Model.js` tests, "no pin edited or removed since main", the grep for
install-command words, README's gallery against `docs/gallery/`,
`manifest.json`, `qmllint` and `omarchy plugin validate` where the shell is.
CI runs the same script on every pull request. Run it until it passes; a
skipped line names the tool this machine lacks.

## Adding your headphones

Use `tools/add-device` and [the device API guide](docs/ADAPTER-API.md).
The contribution unit is `devices/<model>/`: profile, exact device identity,
capture, reviewed session, model fault tests, owner hardware/integration checks,
protocol notes and screenshot. The guide suggests an existing adapter from
UUIDs; a device with a different protocol brings `adapter.py` beside its profile.

Known protocol parameters stay local to the new profile. Do not edit another
model's bridge row, pin or package. Existing bridge scripts and their owner pins
remain binding and are not migrated by this workflow. New packages may not
claim an existing owner's reported model name.

`tools/add-device sync` generates the new-device registry in `Model.js` and the
README section. Run `CHECK_BASE=origin/main tools/check`; missing evidence,
skipped model fault scenarios, stale generated files and stale hardware reports
fail. CI writes the same per-device readiness report into its job summary.

New control types still require an explicit change to `BRIDGE.md` and the UI.
The v1 profile vocabulary covers the existing listening-mode controls; a local
adapter implements the same contract rather than inventing new shell behavior.

## Never

- Edit or delete a pin that is not yours. `tools/check` fails on it and CI
  comments on the pull request naming the owner.
- Add an install command, a package name to install, or a dependency
  Omarchy does not ship — not in code, not in README, not in a comment.
  The marketplace scanner greps for the words; the shell's own services
  (`Quickshell.Services.Mpris`, `.Bluetooth`, `.Notifications`) are there
  for what a binary would otherwise do.
- Write into the plugin directory while the shell runs it: every file
  written there reloads the plugin. Work in a clone or a worktree, as the
  tools in `tools/` do.
- Change what applies to every device or every user (a new default-on
  setting, a new action on disconnect) inside a pull request about one
  model. Say it separately; the maintainer decides it.

## Where things are

| | |
|:--|:--|
| `devices/<model>/` | One new model: profile, adapter if needed, evidence and owner checks |
| `adapter_api/`, `device-adapter` | Profile validation, runtime adapter boundary, capture, replay and readiness |
| `tools/add-device` | Guided contribution, live check and generated registry/gallery |
| `Model.js` | the decisions: device picking, `BACKENDS`, parsing, formatting. Deno tests in `tests/model.test.js` |
| `Service.qml` | the followed devices, the Fast Pair reader, parking and backoff, the IPC methods |
| `DeviceFollower.qml` | one device: reading, bridge process, the state the panel reads |
| `Panel.qml` | the panel |
| `<brand>-bridge` | one process per protocol; `BRIDGE.md` is their contract |
| `gfps-reader` | the Fast Pair Message Stream reader, one for every device |
| `tests/harness.py`, `tests/pins/` | how a bridge is tested; the frozen sessions |
| `tools/check` | the one list |
| `tools/*_probe.py` | how a protocol is read off a device |
| `PROTOCOL.md` | what every device said, and how it was found |
| `docs/captures/` | the raw evidence behind a pin |
| `.agents/skills/land-pr/` | shared PR review and landing workflow; Claude entry point refers here |

For a guided contribution, use `/add-new-bridge` or read
[the shared skill](.agents/skills/add-new-bridge/SKILL.md) before probing or implementing.
