"""Host-owned Fast Pair support history, preserving the existing cache format."""
import fcntl
import json
import os
from pathlib import Path


def remember(model_id, answered):
    if not model_id:
        return
    directory = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omaphones"
    path = directory / "mode-support.json"
    temporary = directory / ("mode-support.%d.tmp" % os.getpid())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "mode-support.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                known = json.loads(path.read_text())
            except (OSError, ValueError):
                known = {}
            if not isinstance(known, dict):
                known = {}
            entry = known.get(model_id, {})
            if isinstance(entry, bool):
                entry = {"supported": entry}
            if not isinstance(entry, dict):
                entry = {}
            if entry.get("supported") is True:
                return
            if answered:
                entry = {"supported": True}
            else:
                try:
                    misses = max(0, int(entry.get("misses", 0))) + 1
                except (ValueError, TypeError):
                    misses = 1
                entry["misses"] = misses
                if misses >= 3:
                    entry["supported"] = False
            known[model_id] = entry
            temporary.write_text(json.dumps(known, separators=(",", ":"), sort_keys=True))
            os.replace(temporary, path)
    except OSError:
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
