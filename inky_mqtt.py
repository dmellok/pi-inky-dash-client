#!/usr/bin/env python3
"""
MQTT-driven image display coordinator for the Pimoroni Inky Impression 13.3".

Subscribes to inky/update for render jobs and publishes its own state to
inky/status so subscribers (e.g. a companion web app) can see live render
progress without polling. For each incoming job it spawns a fresh
inky_render.py subprocess, which does exactly one render and exits —
this avoids the long-running-process state degradation that the inky
library suffers from.

Run with the Pimoroni venv's Python:

    sudo ~/.virtualenvs/pimoroni/bin/pip install paho-mqtt python-dotenv
    ~/.virtualenvs/pimoroni/bin/python inky_mqtt.py

(`requests` and `Pillow` are needed by the renderer, not the listener.)

Configuration precedence (later wins):
    defaults  <  .env file  <  process env  <  command-line flags

Environment variables:
    MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_TOPIC, MQTT_STATUS_TOPIC, MQTT_CLIENT_ID, MQTT_TLS, LOG_LEVEL,
    INKY_RENDERER  (path to inky_render.py; default: alongside this file)
    INKY_RENDER_TIMEOUT  (seconds; default 120)

Job payload (JSON published to MQTT_TOPIC, default `inky/update`):
    {
        "url":        "https://example.com/photo.jpg",   // OR
        "path":       "/home/kayden/images/photo.jpg",
        "rotate":     0 | 90 | 180 | 270,
        "scale":      "fit" | "fill" | "stretch" | "center",
        "bg":         "white" | "black" | "red" | "green" | "blue" | "yellow" | "orange",
        "saturation": 0.0 - 1.0
    }

Status payload (JSON, retained on MQTT_STATUS_TOPIC, default `inky/status`):
    {"state": "idle",      "ts": "...", "last_url": "...", "last_result": "ok",
                                          "last_render_at": "...", "last_duration_s": 32.1}
    {"state": "rendering", "ts": "...", "url": "...", "started_at": "..."}
    {"state": "offline"}                  # set as LWT; broker auto-publishes
                                          # if the daemon disappears uncleanly
"""

import argparse
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


DEFAULT_RENDER_TIMEOUT = 120  # seconds; full refresh is ~30s

log = logging.getLogger("inky-mqtt")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Status tracking                                                             #
# --------------------------------------------------------------------------- #

class StatusTracker:
    """Holds the current and last-render state and publishes it to MQTT.

    Thread-safe: paho-mqtt's publish() is safe to call from any thread.
    """

    def __init__(self, client, topic):
        self.client = client
        self.topic = topic
        self._lock = threading.Lock()
        self.last_url = None
        self.last_result = None      # "ok" | "failed" | "timeout"
        self.last_render_at = None
        self.last_duration_s = None

    def _publish(self, payload):
        body = json.dumps(payload, separators=(",", ":"))
        log.debug("Publishing status: %s", body)
        # retain=True so a new subscriber immediately sees current state.
        self.client.publish(self.topic, body, qos=1, retain=True)

    def publish_idle(self):
        with self._lock:
            payload = {"state": "idle", "ts": _now_iso()}
            if self.last_url:
                payload.update({
                    "last_url": self.last_url,
                    "last_result": self.last_result,
                    "last_render_at": self.last_render_at,
                    "last_duration_s": self.last_duration_s,
                })
        self._publish(payload)

    def publish_rendering(self, url):
        ts = _now_iso()
        self._publish({
            "state": "rendering",
            "ts": ts,
            "url": url,
            "started_at": ts,
        })

    def record_result(self, url, result, duration_s):
        with self._lock:
            self.last_url = url
            self.last_result = result
            self.last_render_at = _now_iso()
            self.last_duration_s = round(duration_s, 2)


# --------------------------------------------------------------------------- #
# Display worker                                                              #
# --------------------------------------------------------------------------- #

def _renderer_path():
    override = os.environ.get("INKY_RENDERER")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "inky_render.py")


def display_worker(renderer_path, renderer_timeout, job_queue, stop_event, status):
    cmd = [sys.executable, renderer_path]
    log.info("Display worker ready; renderer=%s timeout=%ds",
             " ".join(cmd), renderer_timeout)

    while not stop_event.is_set():
        try:
            job = job_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        url = job.get("url") or job.get("path") or "?"
        started = time.monotonic()
        status.publish_rendering(url)

        try:
            payload = json.dumps(job)
            log.info("Spawning renderer for %s", url)

            proc = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=renderer_timeout,
                check=False,
            )

            for line in proc.stderr.splitlines():
                log.info("renderer: %s", line)

            duration = time.monotonic() - started
            if proc.returncode == 0:
                log.info("Render finished cleanly in %.1fs", duration)
                status.record_result(url, "ok", duration)
            else:
                log.error("Renderer exited with code %d", proc.returncode)
                status.record_result(url, "failed", duration)

        except subprocess.TimeoutExpired:
            duration = time.monotonic() - started
            log.error("Renderer exceeded %ds timeout; killed", renderer_timeout)
            status.record_result(url, "timeout", duration)
        except Exception:
            duration = time.monotonic() - started
            log.exception("Failed to invoke renderer for job %s", job)
            status.record_result(url, "failed", duration)
        finally:
            job_queue.task_done()
            status.publish_idle()

    log.info("Display worker shutting down")


# --------------------------------------------------------------------------- #
# MQTT                                                                        #
# --------------------------------------------------------------------------- #

def make_mqtt_client(args, job_queue):
    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=args.client_id,
            clean_session=True,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=args.client_id, clean_session=True)

    if args.user:
        client.username_pw_set(args.user, args.password or None)

    if args.tls:
        client.tls_set()

    # Last Will: if we drop off the broker uncleanly, the broker auto-
    # publishes this offline message on our status topic so subscribers
    # know the daemon is gone.
    client.will_set(
        args.status_topic,
        json.dumps({"state": "offline"}, separators=(",", ":")),
        qos=1,
        retain=True,
    )

    def on_connect(c, _userdata, _flags, rc):
        if rc == 0:
            log.info("Connected to MQTT %s:%d, subscribing to %s",
                     args.broker, args.port, args.topic)
            c.subscribe(args.topic, qos=1)
            # Initial idle status; status object is attached to the client
            # by main() before the loop starts.
            status = getattr(c, "_status", None)
            if status is not None:
                status.publish_idle()
        else:
            log.error("MQTT connection failed (rc=%d)", rc)

    def on_disconnect(_c, _userdata, rc, *_):
        if rc != 0:
            log.warning("MQTT unexpectedly disconnected (rc=%d); will reconnect", rc)

    def on_message(_c, _userdata, msg):
        raw = msg.payload.decode("utf-8", errors="replace").strip()
        log.debug("Message on %s: %s", msg.topic, raw)

        if not raw:
            log.warning("Empty payload, ignoring")
            return

        if raw.startswith("{"):
            try:
                job = json.loads(raw)
            except json.JSONDecodeError as e:
                log.error("Invalid JSON payload: %s", e)
                return
            if not isinstance(job, dict):
                log.error("JSON payload must be an object, got %s",
                          type(job).__name__)
                return
        else:
            job = {"url" if "://" in raw else "path": raw}

        if not (job.get("url") or job.get("path")):
            log.error("Payload missing 'url' or 'path' field")
            return

        job_queue.put(job)
        log.info("Queued job (queue depth: %d)", job_queue.qsize())

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", default=os.environ.get("INKY_ENV_FILE", ".env"),
                        help="Path to .env file (default: ./.env)")
    parser.add_argument("--broker", default=os.environ.get("MQTT_BROKER", "localhost"),
                        help="MQTT broker hostname (env: MQTT_BROKER)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MQTT_PORT", "1883")),
                        help="MQTT broker port (env: MQTT_PORT, default 1883)")
    parser.add_argument("--user", default=os.environ.get("MQTT_USER"),
                        help="MQTT username (env: MQTT_USER)")
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"),
                        help="MQTT password (env: MQTT_PASSWORD)")
    parser.add_argument("--topic", default=os.environ.get("MQTT_TOPIC", "inky/update"),
                        help="Topic to subscribe to for jobs (env: MQTT_TOPIC)")
    parser.add_argument("--status-topic",
                        default=os.environ.get("MQTT_STATUS_TOPIC", "inky/status"),
                        help="Topic to publish state to (env: MQTT_STATUS_TOPIC)")
    parser.add_argument("--client-id",
                        default=os.environ.get("MQTT_CLIENT_ID",
                                               f"inky-impression-{os.getpid()}"),
                        help="MQTT client ID (env: MQTT_CLIENT_ID)")
    parser.add_argument("--tls", action="store_true",
                        default=_truthy(os.environ.get("MQTT_TLS", "")),
                        help="Enable TLS for the MQTT connection (env: MQTT_TLS)")
    parser.add_argument("--log-level",
                        default=os.environ.get("LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity (env: LOG_LEVEL)")
    parser.add_argument("--render-timeout", type=int,
                        default=int(os.environ.get("INKY_RENDER_TIMEOUT",
                                                   str(DEFAULT_RENDER_TIMEOUT))),
                        help="Seconds before a stuck renderer is killed "
                             "(env: INKY_RENDER_TIMEOUT)")
    return parser


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=os.environ.get("INKY_ENV_FILE", ".env"))
    pre_args, _ = pre.parse_known_args()

    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit(
            "The 'python-dotenv' package is not installed in this venv.\n"
            "Install it with: ~/.virtualenvs/pimoroni/bin/pip install python-dotenv"
        )

    loaded_env = None
    if os.path.isfile(pre_args.env_file):
        load_dotenv(pre_args.env_file, override=False)
        loaded_env = pre_args.env_file

    args = build_parser().parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if loaded_env:
        log.info("Loaded config from %s", loaded_env)
    else:
        log.info("No .env file at %s; using environment + flags only",
                 pre_args.env_file)

    renderer_path = _renderer_path()
    if not os.path.isfile(renderer_path):
        sys.exit(
            f"Renderer not found at {renderer_path}.\n"
            "Set INKY_RENDERER to its path, or place inky_render.py "
            "next to inky_mqtt.py."
        )
    log.info("Using renderer: %s", renderer_path)
    log.info("Status topic:   %s", args.status_topic)

    job_queue = queue.Queue()
    stop_event = threading.Event()

    client = make_mqtt_client(args, job_queue)
    status = StatusTracker(client, args.status_topic)
    # Stash on the client so on_connect can find it without globals.
    client._status = status

    worker = threading.Thread(
        target=display_worker,
        args=(renderer_path, args.render_timeout, job_queue, stop_event, status),
        daemon=True,
    )
    worker.start()

    def shutdown(signum, _frame):
        log.info("Caught signal %s, shutting down", signum)
        stop_event.set()
        # Best-effort clean offline marker. If we crash, the LWT covers us.
        try:
            client.publish(
                args.status_topic,
                json.dumps({"state": "offline", "ts": _now_iso()},
                           separators=(",", ":")),
                qos=1, retain=True,
            ).wait_for_publish(timeout=2)
        except Exception:
            pass
        client.disconnect()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client.connect_async(args.broker, args.port, keepalive=60)
    log.info("Starting MQTT loop (Ctrl+C to exit)")
    try:
        client.loop_forever(retry_first_connection=True)
    finally:
        stop_event.set()
        worker.join(timeout=5)
        log.info("Goodbye")


if __name__ == "__main__":
    main()
