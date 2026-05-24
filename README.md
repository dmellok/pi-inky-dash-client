# Pi Inky Dash Client

The Raspberry Pi side of [Inky Dash](https://github.com/dmellok/inky-dash) — a small MQTT daemon that listens for render jobs and paints them onto a [Pimoroni Inky Impression](https://shop.pimoroni.com/products/inky-impression-7-3) e-ink panel.

It subscribes to `inky/update`, fetches the image referenced in each job (URL or local path), prepares it for the panel, and pushes it to the display. Live render state is published back to `inky/status` as a retained message so the companion web app can show "rendering / idle / offline" without polling.

You can run this without the companion — anything that can publish JSON to MQTT can drive the panel.

## What's in the box

| File | Role |
|---|---|
| `inky_mqtt.py` | The long-running daemon. Connects to MQTT, queues incoming jobs, supervises a subprocess per render, publishes status |
| `inky_render.py` | Single-shot renderer. Reads one JSON job from stdin, renders one image, exits. Spawned fresh per job — works around inky-driver state degradation across many renders in a single process |
| `install.sh` | Installer: drops the scripts into `/opt/inky-mqtt/`, writes `/etc/inky-mqtt/.env`, registers a systemd service, starts it |

## Requirements

- A Raspberry Pi with a Pimoroni Inky Impression panel attached and the [official Pimoroni Inky software](https://github.com/pimoroni/inky) installed. The installer expects the venv at `~/.virtualenvs/pimoroni` (override with `PIMORONI_VENV=/path/to/venv`).
- An MQTT broker reachable from the Pi. Mosquitto running on the same LAN is the simplest setup.
- Python 3.9+ (anything Pimoroni's installer ships will do).

## Install

```bash
git clone https://github.com/dmellok/pi-inky-dash-client.git
cd pi-inky-dash-client
sudo ./install.sh
```

The installer will:

1. Make sure SPI + I²C are enabled (via `raspi-config nonint`).
2. `pip install paho-mqtt requests python-dotenv` into the Pimoroni venv.
3. Copy `inky_mqtt.py` and `inky_render.py` to `/opt/inky-mqtt/`.
4. Prompt you for broker / port / user / password / topic and write `/etc/inky-mqtt/.env` (mode 0600).
5. Register `inky-mqtt.service` and start it.

Re-running is safe — it updates files in place and restarts the service.

For a non-interactive install, copy `.env.example` to `/etc/inky-mqtt/.env` and fill it in *before* running the installer — it will keep an existing config rather than prompting.

## Smoke test

```bash
# From any machine with mosquitto-clients installed:
mosquitto_pub -h <broker> -t inky/update \
  -m '{"url":"https://picsum.photos/1600/1200","scale":"fill"}'

# Watch live state:
mosquitto_sub -h <broker> -t inky/status -v
```

If everything's wired up you should see a `rendering` message immediately, then `idle` ~30 s later when the e-ink refresh completes, and the picture appears on the panel.

## MQTT contract

### Job payload — published to `inky/update`

```jsonc
{
  "url":        "https://example.com/photo.jpg",   // or
  "path":       "/home/pi/images/photo.jpg",
  "rotate":     0,        // 0 | 90 | 180 | 270
  "scale":      "fit",    // fit | fill | stretch | center
  "bg":         "white",  // white | black | red | green | blue | yellow | orange
  "saturation": 0.5       // 0.0 – 1.0, only meaningful for 7-colour panels
}
```

Only `url` *or* `path` is required. The listener also accepts a bare URL or path string for convenience — anything that doesn't start with `{` is treated as a single-source job.

### Status payload — retained on `inky/status`

```jsonc
// While rendering
{"state": "rendering", "ts": "2026-05-07T10:14:02Z",
 "url": "https://...", "started_at": "2026-05-07T10:14:02Z"}

// When idle (last render's outcome is included for context)
{"state": "idle", "ts": "2026-05-07T10:14:34Z",
 "last_url": "https://...", "last_result": "ok",
 "last_render_at": "2026-05-07T10:14:34Z", "last_duration_s": 32.1}

// Offline — set as the MQTT Last Will, broker auto-publishes if the daemon dies
{"state": "offline"}
```

`last_result` is `"ok"`, `"failed"`, or `"timeout"` (see `INKY_RENDER_TIMEOUT` below).

## Configuration

`/etc/inky-mqtt/.env` (created by the installer):

| Var | Default | Notes |
|---|---|---|
| `MQTT_BROKER` | `localhost` | Broker hostname |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` | — | Optional username |
| `MQTT_PASSWORD` | — | Optional password |
| `MQTT_TOPIC` | `inky/update` | Job topic the listener subscribes to |
| `MQTT_STATUS_TOPIC` | `inky/status` | State topic the listener publishes on (retained) |
| `MQTT_CLIENT_ID` | `inky-impression-<pid>` | MQTT client ID |
| `MQTT_TLS` | `false` | Enable TLS (`paho.tls_set()` with system CA roots) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `INKY_RENDERER` | `<dir>/inky_render.py` | Path to the single-shot renderer |
| `INKY_RENDER_TIMEOUT` | `120` | Seconds before a stuck render is killed |

Every var also has a matching `--flag` if you'd rather pass them directly. CLI flags override env, env overrides `.env`, `.env` overrides defaults.

## Operational tips

```bash
# Service management
sudo systemctl status inky-mqtt
sudo systemctl restart inky-mqtt
sudo journalctl -u inky-mqtt -f      # follow live logs

# Edit config and restart
sudoedit /etc/inky-mqtt/.env
sudo systemctl restart inky-mqtt
```

Each render is logged to the journal with the source URL, the outcome, and the duration in seconds. If `last_result` is `"timeout"` repeatedly, the panel may have come unseated — power-cycle the Pi.

## Why a fresh subprocess per render?

The `inky` driver gradually accumulates GPIO / SPI state inside a single process; after enough consecutive renders, `set_image()` and `show()` silently stop actually updating the panel. Running the renderer as a subprocess that exits after one render gives the kernel an unconditional reset between jobs. The cost is ~1 second of import overhead, which is invisible against the ~30-second e-ink refresh.

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [Pimoroni](https://shop.pimoroni.com) for the Inky Impression hardware and the [`inky`](https://github.com/pimoroni/inky) Python driver this stands on top of.
- [Eclipse paho-mqtt](https://github.com/eclipse/paho.mqtt.python) for the MQTT client.
