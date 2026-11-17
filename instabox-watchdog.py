#!/usr/bin/env python3
"""instabox watchdog — keeps a sandbox running around the free-plan runtime cap.

The guest keepawake daemon removes the 30-minute idle pause. It cannot touch
the runtime cap: on the free plan a run grows to 480 minutes and then the
machine pauses with a cooldown. This script runs outside the sandbox and, on
every tick:

  * resumes a machine paused on the runtime cap, once its cooldown is over
  * wakes a machine that paused idle (the keepawake daemon may be down)
  * optionally ends the fresh run with a clean suspend, so the next cap pause
    is a 5-20 minute rest instead of 60-75 or 120-135

One tick per invocation (systemd timer or cron), or --loop N to stay resident.

Config: /etc/instabox-keepawake-watchdog.conf
  MACHINE_ID=m_...
  API_KEY=ibk_...
  RESET_RUN_COUNT=1
  RESUME_MANUAL=0
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

CONF = "/etc/instabox-keepawake-watchdog.conf"
DEFAULT_BASE = "https://api.instabox.sh/v1"
DEFAULT_LOG = "/var/log/instabox-watchdog.log"
DEFAULT_STATE = "/var/lib/instabox-watchdog"


def read_conf(path=CONF):
    conf = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Watchdog:
    def __init__(self, args, conf):
        self.machine = args.machine_id or conf.get("MACHINE_ID") or os.environ.get("INSTABOX_MACHINE_ID")
        self.key = args.api_key or conf.get("API_KEY") or os.environ.get("INSTABOX_API_KEY")
        self.base = (args.api_base or conf.get("API_BASE") or DEFAULT_BASE).rstrip("/")
        self.reset = args.reset_run_count or truthy(conf.get("RESET_RUN_COUNT", "0"))
        self.manual = args.resume_manual or truthy(conf.get("RESUME_MANUAL", "0"))
        self.wait_seconds = args.wait_seconds
        self.log_path = args.log or conf.get("LOG") or DEFAULT_LOG
        self.state_dir = args.state_dir or conf.get("STATE_DIR") or DEFAULT_STATE
        if not self.machine:
            raise SystemExit("machine id missing: pass --machine-id or set MACHINE_ID in " + CONF)
        if not self.key:
            raise SystemExit("api key missing: pass --api-key or set API_KEY in " + CONF)

    def log(self, message):
        line = "%s %s" % (dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"), message)
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print("log write failed: %s" % exc, flush=True)

    def api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-API-Key": self.key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
            "User-Agent": "instabox-watchdog/1.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                doc = json.loads(raw) if raw else {}
                return resp.status, doc
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                doc = json.loads(raw)
            except Exception:                                  # noqa: BLE001
                doc = {"raw": raw.decode("utf-8", "replace")}
            return err.code, doc
        except Exception as exc:                               # noqa: BLE001
            return 0, {"error": {"code": "network", "message": str(exc)}}

    def machine_state(self):
        status, doc = self.api("GET", "/machines/%s" % self.machine)
        if status != 200:
            self.log("machine read failed: status=%s body=%s" % (status, json.dumps(doc)[:300]))
            return None
        return doc

    @staticmethod
    def state_of(machine):
        for name in ("state", "status", "desiredState"):
            value = machine.get(name)
            if value:
                return str(value).lower()
        return ""

    def matches(self, state, want):
        if want == "paused":
            return state in ("paused", "suspended")
        return state == want

    def action(self, name):
        status, doc = self.api("POST", "/machines/%s/%s" % (self.machine, name), {})
        if 200 <= status < 300:
            self.log("%s: accepted status=%s" % (name, status))
            return True
        error = (doc or {}).get("error", {})
        self.log("%s: refused status=%s code=%s retryAfter=%ss" % (
            name, status, error.get("code"), doc.get("retryAfterSeconds")))
        return False

    def wait_for(self, want, seconds=None):
        limit = time.time() + (seconds or self.wait_seconds)
        while time.time() < limit:
            time.sleep(3)
            machine = self.machine_state()
            if not machine:
                continue
            if self.matches(self.state_of(machine), want):
                return machine
        self.log("timed out waiting for state=%s" % want)
        return None

    def tick(self):
        machine = self.machine_state()
        if not machine:
            return 1
        state = self.state_of(machine)
        self.log("state=%s maxRun=%s runStartedAt=%s cooldownUntil=%s autoSuspendedAt=%s" % (
            state, machine.get("maxRunMinutes"), machine.get("runStartedAt"),
            machine.get("cooldownUntil"), machine.get("autoSuspendedAt")))

        if state == "running":
            return 0
        if state == "stopped":
            self.log("machine stopped; starting")
            if self.action("start"):
                self.wait_for("running")
            return 0
        if not self.matches(state, "paused"):
            self.log("transient state %s; leaving it alone" % state)
            return 0

        if machine.get("autoSuspendedAt"):
            self.log("idle pause detected; waking (keepawake may be down)")
            self.action("wake")
            return 0

        cooldown = machine.get("cooldownUntil")
        if not cooldown and not self.manual:
            self.log("paused by hand; leaving it alone (set RESUME_MANUAL=1 to override)")
            return 0
        if cooldown:
            try:
                until = dt.datetime.fromisoformat(str(cooldown).replace("Z", "+00:00"))
                if until > dt.datetime.now(dt.timezone.utc):
                    self.log("cooldown until %s; nothing to do" % until.isoformat())
                    return 0
            except ValueError:
                pass

        self.log("runtime-cap pause; resuming")
        if not self.action("resume"):
            return 0
        if not self.wait_for("running"):
            return 0
        if self.reset:
            self.log("clean suspend to reset the free-plan cooldown step")
            if self.action("suspend") and self.wait_for("paused", 90):
                if self.action("resume"):
                    self.wait_for("running")
        final = self.machine_state()
        if final:
            self.log("done: state=%s maxRun=%s" % (self.state_of(final), final.get("maxRunMinutes")))
        return 0


def main():
    parser = argparse.ArgumentParser(description="resume an instabox sandbox after runtime pauses")
    parser.add_argument("-m", "--machine-id", default="")
    parser.add_argument("-k", "--api-key", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--log", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--reset-run-count", action="store_true")
    parser.add_argument("--resume-manual", action="store_true")
    parser.add_argument("--loop", type=int, default=0, help="stay resident, tick every N seconds")
    parser.add_argument("--conf", default=CONF)
    args = parser.parse_args()

    conf = read_conf(args.conf)
    dog = Watchdog(args, conf)
    os.makedirs(dog.state_dir, exist_ok=True)

    lock_path = os.path.join(dog.state_dir, "watchdog.lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            dog.log("another watchdog run holds the lock; skipping this tick")
            return 0
        if args.loop:
            while True:
                dog.tick()
                time.sleep(args.loop)
        return dog.tick()


if __name__ == "__main__":
    sys.exit(main())
