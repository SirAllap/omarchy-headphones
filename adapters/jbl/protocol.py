"""JBL mode frames. GATT discovery/subscription and the support cache are host work."""
from omaphones.api import Protocol

FRAME_NOTIFY_ON = bytes.fromhex("aa 9b 02 01 01")
FRAME_GET = bytes.fromhex("aa 91 01 11")
FRAME_SET = {
    "off": bytes.fromhex("aa 91 07 10 01 00 02 00 03 00"),
    "anc": bytes.fromhex("aa 91 07 10 01 01 02 00 03 00"),
    "ambient": bytes.fromhex("aa 91 07 10 01 00 02 01 03 00"),
    "talkthru": bytes.fromhex("aa 91 07 10 01 00 02 00 03 01"),
}
FRAME_GAP = 2000
ANSWER_TIMEOUT = 12000


class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.answered = False

    def connected(self):
        # The host has completed discovery and waited for subscription exactly
        # as configured in adapter.json. Preserve the two observed frame gaps.
        self.schedule(FRAME_GAP, "enable_notifications")

    def enable_notifications(self):
        self.write(FRAME_NOTIFY_ON)
        self.schedule(FRAME_GAP, "query")

    def query(self):
        self.write(FRAME_GET)
        self.schedule(ANSWER_TIMEOUT, "answer_timeout")

    def answer_timeout(self):
        if not self.answered:
            self.finish(3, "the device did not answer the listening-mode query")

    def received(self, data):
        # Notifications are complete ATT values, not an RFCOMM byte stream.
        if len(data) != 10 or data[:5] != bytes.fromhex("aa 91 07 12 01"):
            return
        if data[6] != 2 or data[8] != 3:
            return
        mode = "anc" if data[5] == 1 else "ambient" if data[7] == 1 else "talkthru" if data[9] == 1 else "off"
        self.answered = True
        self.report({"noise.mode": mode}, {"noise.mode": {"values": list(FRAME_SET)}})

    def command(self, control, value):
        if control == "noise.mode" and value in FRAME_SET:
            self.write(FRAME_SET[value])
