#!/usr/bin/env bash
#
# Installer for the Inky Impression MQTT listener.
#
# Assumes you've already run Pimoroni's official Inky installer and have a
# working venv at ~/.virtualenvs/pimoroni. This script just:
#
#   1. Adds paho-mqtt, requests, python-dotenv into that venv.
#   2. Installs inky_mqtt.py + inky_render.py into /opt/inky-mqtt/.
#   3. Creates /etc/inky-mqtt/.env (interactively, unless one already exists).
#   4. Registers and starts a systemd service that runs on boot.
#
# Usage:
#     sudo ./install.sh
#
# Override the venv location if yours lives somewhere else:
#     sudo PIMORONI_VENV=/path/to/venv ./install.sh
#
# Re-running is safe; it will update files in place and restart the service.

set -euo pipefail

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
APP_DIR="/opt/inky-mqtt"
CONFIG_DIR="/etc/inky-mqtt"
ENV_FILE="${CONFIG_DIR}/.env"
SERVICE_NAME="inky-mqtt.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
PIMORONI_VENV="${PIMORONI_VENV:-${RUN_HOME}/.virtualenvs/pimoroni}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------- #
# Pretty output                                                               #
# --------------------------------------------------------------------------- #
say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!! \033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31mxx \033[0m %s\n" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Pre-flight                                                                  #
# --------------------------------------------------------------------------- #
[[ $EUID -eq 0 ]] || fail "Run as root: sudo $0"

[[ -f "${SCRIPT_DIR}/inky_mqtt.py" ]] \
    || fail "inky_mqtt.py not found next to install.sh"

[[ -f "${SCRIPT_DIR}/inky_render.py" ]] \
    || fail "inky_render.py not found next to install.sh"

id "${RUN_USER}" &>/dev/null \
    || fail "User '${RUN_USER}' does not exist."

[[ -x "${PIMORONI_VENV}/bin/python" ]] \
    || fail "Pimoroni venv not found at ${PIMORONI_VENV}.
       Run Pimoroni's official Inky installer first, or pass the path:
           sudo PIMORONI_VENV=/path/to/venv ./install.sh"

"${PIMORONI_VENV}/bin/python" -c "import inky" 2>/dev/null \
    || fail "The venv at ${PIMORONI_VENV} does not have 'inky' installed.
       This doesn't look like the Pimoroni venv — double-check the path."

INKY_VERSION="$("${PIMORONI_VENV}/bin/pip" show inky 2>/dev/null | awk '/^Version:/ {print $2}')"
say "Using Pimoroni venv at ${PIMORONI_VENV} (inky ${INKY_VERSION})"

# --------------------------------------------------------------------------- #
# 1. SPI / I2C interfaces                                                     #
# --------------------------------------------------------------------------- #
if command -v raspi-config &>/dev/null; then
    say "Ensuring SPI and I2C interfaces are enabled..."
    raspi-config nonint do_spi 0 || warn "Could not toggle SPI automatically"
    raspi-config nonint do_i2c 0 || warn "Could not toggle I2C automatically"
fi

# --------------------------------------------------------------------------- #
# 2. Add our extra Python deps into the Pimoroni venv                         #
# --------------------------------------------------------------------------- #
# Run pip as the venv owner so we don't end up with root-owned files inside
# a user-owned virtualenv.
say "Installing paho-mqtt, requests, python-dotenv into the Pimoroni venv..."
sudo -u "${RUN_USER}" "${PIMORONI_VENV}/bin/pip" install --upgrade \
    paho-mqtt \
    requests \
    python-dotenv

# --------------------------------------------------------------------------- #
# 3. App directory                                                            #
# --------------------------------------------------------------------------- #
say "Installing inky_mqtt.py and inky_render.py to ${APP_DIR}..."
mkdir -p "${APP_DIR}"
install -m 0755 "${SCRIPT_DIR}/inky_mqtt.py"   "${APP_DIR}/inky_mqtt.py"
install -m 0755 "${SCRIPT_DIR}/inky_render.py" "${APP_DIR}/inky_render.py"

# Migrate from previous installs that used inky_display.py.
if [[ -f "${APP_DIR}/inky_display.py" ]]; then
    say "Removing legacy ${APP_DIR}/inky_display.py..."
    rm -f "${APP_DIR}/inky_display.py"
fi

chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"

# --------------------------------------------------------------------------- #
# 4. Configuration                                                            #
# --------------------------------------------------------------------------- #
mkdir -p "${CONFIG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
    say "Existing config found at ${ENV_FILE} — keeping it."
else
    say "Creating ${ENV_FILE}..."
    if [[ -t 0 ]]; then
        read -r -p "MQTT broker hostname [mqtt.local]: " broker
        broker="${broker:-mqtt.local}"
        read -r -p "MQTT broker port [1883]: " port
        port="${port:-1883}"
        read -r -p "MQTT username (blank for anonymous): " mqtt_user
        if [[ -n "${mqtt_user}" ]]; then
            read -r -s -p "MQTT password: " mqtt_pass; echo
        else
            mqtt_pass=""
        fi
        read -r -p "MQTT topic [inky/update]: " topic
        topic="${topic:-inky/update}"
    else
        broker="mqtt.local"; port="1883"; mqtt_user=""; mqtt_pass=""
        topic="inky/update"
        warn "Non-interactive install — using placeholder broker '${broker}'."
        warn "Edit ${ENV_FILE} before relying on the service."
    fi

    cat >"${ENV_FILE}" <<EOF
# Inky Impression MQTT listener config (managed by install.sh)
MQTT_BROKER=${broker}
MQTT_PORT=${port}
MQTT_USER=${mqtt_user}
MQTT_PASSWORD=${mqtt_pass}
MQTT_TOPIC=${topic}
MQTT_STATUS_TOPIC=inky/status
MQTT_CLIENT_ID=inky-impression
MQTT_TLS=false
LOG_LEVEL=INFO
EOF
fi

chown "${RUN_USER}:${RUN_USER}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# --------------------------------------------------------------------------- #
# 5. systemd service                                                          #
# --------------------------------------------------------------------------- #
say "Writing ${SERVICE_PATH}..."
cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=Inky Impression MQTT listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PIMORONI_VENV}/bin/python ${APP_DIR}/inky_mqtt.py --env-file ${ENV_FILE}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_PATH}"

# --------------------------------------------------------------------------- #
# 6. Start the service                                                        #
# --------------------------------------------------------------------------- #
say "Reloading systemd and (re)starting the service..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    say "Service is running."
else
    warn "Service is not active — check logs with:"
    warn "    journalctl -u ${SERVICE_NAME} -e"
fi

# --------------------------------------------------------------------------- #
# Done                                                                        #
# --------------------------------------------------------------------------- #
cat <<EOF

------------------------------------------------------------
Install complete.

Pimoroni venv:  ${PIMORONI_VENV}
App directory:  ${APP_DIR}
Config file:    ${ENV_FILE}
Service:        ${SERVICE_NAME}

Useful commands:
  sudo systemctl status ${SERVICE_NAME}
  sudo systemctl restart ${SERVICE_NAME}
  sudo journalctl -u ${SERVICE_NAME} -f
  sudoedit ${ENV_FILE}        # edit broker config, then restart

Test publish:
  mosquitto_pub -h <broker> -t inky/update \\
    -m '{"url":"https://picsum.photos/1600/1200","scale":"fill"}'

Watch live status:
  mosquitto_sub -h <broker> -t inky/status -v
------------------------------------------------------------
EOF
