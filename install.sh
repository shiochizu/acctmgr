#!/usr/bin/env bash
# Proxmox LXC Installer
# bash <(curl -fsSL https://raw.githubusercontent.com/shiochizu/acctmgr/main/install.sh)

set -euo pipefail

GN="\e[1;92m"; YW="\e[33m"; RD="\e[1;31m"; BL="\e[96m"; DIM="\e[2m"; CL="\e[m"
BFR="\\r\\033[K"; CM="${GN}✔${CL}"; CROSS="${RD}✘${CL}"

msg_info()  { echo -ne "  ◈ ${YW}${1}${CL}"; }
msg_ok()    { echo -e "${BFR}${CM} ${GN}${1}${CL}"; }
msg_error() { echo -e "${BFR}${CROSS} ${RD}${1}${CL}"; exit 1; }

clear
echo -e "${GN}  Proxmox LXC Installer${CL}\n"

command -v pct &>/dev/null || msg_error "Must be run on a Proxmox VE host."
[[ $EUID -eq 0 ]]         || msg_error "Run as root."

TEMPLATE_URL="${TEMPLATE_URL:-https://github.com/shiochizu/acctmgr/releases/download/v1.0/template.tar.zst}"
TEMPLATE_FILE="/var/lib/vz/template/cache/acctmgr-template.tar.zst"

NEXTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
echo -e "${DIM}  Press Enter to accept defaults${CL}\n"
read -rp "  Container ID    [${NEXTID}]:   " VMID;    VMID=${VMID:-$NEXTID}
read -rp "  Hostname        [dashboard]: " HN;        HN=${HN:-dashboard}
read -rp "  Storage         [local-lvm]: " STORAGE;   STORAGE=${STORAGE:-local-lvm}
read -rp "  RAM MB          [512]:       " RAM;        RAM=${RAM:-512}
read -rp "  Bridge          [vmbr0]:     " BRIDGE;     BRIDGE=${BRIDGE:-vmbr0}
echo ""

if [[ -f "$TEMPLATE_FILE" ]]; then
  msg_ok "Template already cached"
else
  msg_info "Downloading template (≈1.4 GB)…"
  curl -fL --progress-bar -o "$TEMPLATE_FILE" "$TEMPLATE_URL"
  msg_ok "Template downloaded"
fi

msg_info "Creating LXC ${VMID} (${HN})…"
pct restore "${VMID}" "${TEMPLATE_FILE}" \
  --hostname    "${HN}" \
  --rootfs      "${STORAGE}:8" \
  --memory      "${RAM}" \
  --swap        512 \
  --net0        "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --unprivileged 1 \
  --features    nesting=1 \
  --start       0 \
  --ostype      debian \
  2>&1 | grep -v "^WARNING" || true
msg_ok "Container created"

msg_info "Starting container…"
pct set "${VMID}" --template 0
pct start "${VMID}"
sleep 6
msg_ok "Container started"

msg_info "Enabling service…"
pct exec "${VMID}" -- bash -c "systemctl enable ikea && systemctl start ikea" 2>/dev/null
msg_ok "Service started"

IP=$(pct exec "${VMID}" -- bash -c "hostname -I 2>/dev/null | awk '{print \$1}'" 2>/dev/null || echo "?")

echo -e "\n${GN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${CL}"
echo -e "${GN}  Done!${CL}  http://${IP}:8000"
echo -e "${DIM}  CT ${VMID} · ${HN} · ${STORAGE}${CL}\n"
