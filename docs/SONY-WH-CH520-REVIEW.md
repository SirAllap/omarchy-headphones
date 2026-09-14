# WH-CH520 review evidence

The reviewed change is battery-only presentation: after the MDR handshake,
WH-CH520 exits 3 without mode or wear queries. Other Sony names retain their
existing behavior.

## Battery source

The owner captured BlueZ Battery Percentage 100% in
`captures/sony-wh-ch520-bluetoothctl.txt`. Three Fast Pair captures contain
model ID, BLE address and firmware, but no battery frame.
The pin note saying battery was confirmed over Fast Pair is inaccurate.
The owner pin is preserved; its assertions cover MDR parking, not battery.

## Hardware revisions

`captures/sony-wh-ch520-live.json` is the historical 2026-09-12 report,
before the battery-only correction. Its ANC state and passed flag do not
validate the corrected UI.

The owner's [2026-09-14 confirmation](https://github.com/ncr/omarchy-headphones/pull/15#issuecomment-5657745432)
reports deployment of the corrected bridge, a full disconnect/connect,
battery still displayed and mode controls hidden. The updated
`gallery/sony-wh-ch520.png` shows that panel. This is owner confirmation,
not an independent maintainer hardware run.

## Automated scope and limits

The pin and supplementary tests cover no mode/wear queries after handshake,
ignored controls, pending timers, transient handshake failure, fresh-session
parking, peer-state isolation and routing from all 13 captured UUIDs.
MDR RX captures contain decoded payloads, so Session framing is synthetic;
these tests do not claim replay of original RX checksums.
Existing Sony pins remain unchanged.

Peer isolation here is simulated. Physical multi-device isolation and
charging or changing battery levels remain unverified on this model.
