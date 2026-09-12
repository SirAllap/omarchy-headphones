"""Owner-invoked smoke check of the exact checkout's adapter, with restoration.

This tests the adapter, not the running shell. Battery from Fast Pair/BlueZ,
wear edges, reconnect, acoustics and peer isolation need separate owner checks.
"""
import copy
import datetime
import json
import queue
import signal
import subprocess
import threading
import time

from . import profiles


class Client:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.events = queue.Queue()
        self.state = {}
        self.serial = 0

        def reader():
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put({"modes": False, "error": "invalid adapter JSON"})
            self.events.put(None)

        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()

    def wait(self, predicate, timeout=20, after=-1):
        deadline = time.monotonic() + timeout
        while not (self.serial > after and predicate(self.state)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("device did not report the expected state")
            try:
                state = self.events.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("device did not report the expected state") from None
            if state is None:
                raise RuntimeError("adapter exited before reporting the expected state")
            self.state.update(state)
            self.serial += 1
            if state.get("modes") is False:
                raise RuntimeError(state.get("error", "adapter is unavailable"))
        return copy.deepcopy(self.state)

    def set(self, command, field, value):
        # Consume queued reports before sending. A cached value does not confirm a write.
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item is None:
                raise RuntimeError("adapter has exited")
            self.state.update(item)
            self.serial += 1
        if self.state.get(field) == value:
            # A control already in its original state needs no restoration write.
            return copy.deepcopy(self.state)
        serial = self.serial
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.wait(lambda state: state.get(field) == value, after=serial)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


def controls(profile):
    caps = profile["capabilities"]
    out = [("mode:" + value, "set " + value, "mode", value) for value in caps["modes"]]
    out += [("ancLevel:" + value, "level " + value, "ancLevel", value) for value in caps.get("ancLevels", [])]
    ambient = caps.get("ambient")
    if ambient:
        out += [("ambient:" + str(value), "level " + str(value), "level", value)
                for value in (ambient["min"], ambient["max"])]
        out += [("voice:" + text, ambient["voiceCommand"] + " " + text, "voice", value)
                for text, value in (("on", True), ("off", False))]
    if caps.get("latency"):
        out += [("latency:" + text, "latency " + text, "latency", value)
                for text, value in (("on", True), ("off", False))]
    return out


def initial_ready(profile, state):
    caps = profile["capabilities"]
    if state.get("modes") is not True or state.get("mode") not in caps["modes"]:
        return False
    if caps.get("ancLevels") and state.get("ancLevel") not in caps["ancLevels"]:
        return False
    if caps.get("ambient"):
        level = state.get("level")
        if type(level) is not int or not caps["ambient"]["min"] <= level <= caps["ambient"]["max"] or type(state.get("voice")) is not bool:
            return False
    if caps.get("latency") and type(state.get("latency")) is not bool:
        return False
    return True


def restore_actions(profile, initial):
    caps = profile["capabilities"]
    actions = []
    if caps.get("ancLevels"):
        actions.append(("level " + initial["ancLevel"], "ancLevel", initial["ancLevel"]))
    if caps.get("ambient"):
        actions.append(("level " + str(initial["level"]), "level", initial["level"]))
        actions.append((caps["ambient"]["voiceCommand"] + " " + ("on" if initial["voice"] else "off"), "voice", initial["voice"]))
    if caps.get("latency"):
        actions.append(("latency " + ("on" if initial["latency"] else "off"), "latency", initial["latency"]))
    actions.append(("set " + initial["mode"], "mode", initial["mode"]))
    return actions


def run(profile, directory, client, output, root=profiles.ROOT):
    report = {"apiVersion": 1, "device": profile["id"], "owner": profile["owner"],
              "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "implementation": profiles.implementation_hash(profile, directory, root),
              "scope": "adapter", "checks": [], "restoration": [], "passed": False,
              "untested": ["shell-integration", "reconnect", "peer-isolation", "charging", "acoustics"]}
    initial = None
    try:
        initial = client.wait(lambda state: initial_ready(profile, state))
        report["initial"] = initial
        report["checks"].append({"case": "initial", "reported": initial, "passed": True})
        for case, command, field, value in controls(profile):
            state = client.set(command, field, value)
            report["checks"].append({"case": case, "command": command, "reported": state, "passed": True})
        battery = profile["capabilities"].get("battery", {})
        for part in battery.get("parts", []):
            if battery["source"] != "bridge":
                report["untested"].append("battery:" + part)
                continue
            state = client.wait(lambda s: type(s.get("battery", {}).get(part)) is int
                                and 0 <= s["battery"][part] <= 100)
            report["checks"].append({"case": "battery:" + part, "reported": state, "passed": True})
        if profile["capabilities"].get("worn"):
            report["untested"].extend(("worn:on", "worn:off"))
        report["passed"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        if initial is not None:
            for command, field, value in restore_actions(profile, initial):
                try:
                    state = client.set(command, field, value)
                    report["restoration"].append({"command": command, "reported": state, "passed": True})
                except BaseException as error:
                    report["restoration"].append({"command": command, "passed": False, "error": str(error)})
                    report["passed"] = False
        else:
            report["restoration"] = [{"passed": False, "error": "initial state unknown; no controls sent"}]
        try:
            client.close()
        finally:
            output.write(json.dumps(report, indent=2) + "\n")
            output.flush()
    return report


def interrupt(_signum, _frame):
    raise KeyboardInterrupt("owner interrupted the live check")
