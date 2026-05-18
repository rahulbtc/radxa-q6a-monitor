# Radxa Q6A System Monitor

Real-time hardware telemetry dashboard for the **Radxa Q6A** (Qualcomm QCS6490). Single-file Python server, zero Python dependencies beyond the standard library. Built for headless deployments where you need at-a-glance system health without SSH.

OS Tested on:
Official Ubuntu - ARM from Radxa.

Single HTML page, real-time streaming via Server-Sent Events (SSE), dark-themed (with True AMOLED toggle), no heavy JavaScript frameworks.

## What it monitors

| Category | Details |
|----------|---------|
| **CPU** | 8 cores (4× Silver A55, 3× Gold A78, 1× Prime A78+), per-core freq/temp/usage/governor |
| **GPU** | Adreno 643 — frequency, governor, dual thermal sensors |
| **NPU** | Hexagon v68 DSP — ADSP/CDSP state, FastRPC devices, thermal |
| **AI Agent** | QNN/ONNX backend status, embedding throughput, ChromaDB metrics |
| **RAM** | Usage, cached, buffers, swap, zram compression ratios |
| **Disk** | NVMe health (SMART), I/O throughput, temperature sensors |
| **Network** | Per-interface throughput, IPs, DNS/ping connectivity checks |
| **Docker** | All containers with running/exited status |
| **Thermal Map** | 35+ thermal zones across CPU/GPU/NPU/DDR/PMIC/modem/board skin |
| **Voltages** | 20+ PMIC regulators (VPH PWR, BOB, core rails, peripheral rails) |
| **Processes** | Top processes by memory, zombie count, thread count |

Port **3999**.

## Screenshots

Desktop Homepage:
<img width="1919" height="1114" alt="image" src="https://github.com/user-attachments/assets/6b09786d-214e-4033-bc2e-a5baee79d32d" />


True Black (AMOLED Theme):
<img width="1916" height="1195" alt="image" src="https://github.com/user-attachments/assets/e82f34f6-8fac-4de4-84b3-56d6942d8f25" />


Mobile Homepage:

<img width="509" height="983" alt="image" src="https://github.com/user-attachments/assets/0ea14fa7-33c5-431e-aa19-61c6c379d206" />



## Quick Start

```bash
git clone https://github.com/rahulbtc/radxa-q6a-monitor.git
cd radxa-q6a-monitor
docker compose up -d --build
```

Open `http://<radxa-ip>:3999/`

That's it. No config files, no env vars, no setup wizard.

## Requirements

| Requirement | Notes |
|-------------|-------|
| Radxa Q6A (QCS6490) | The dashboard is designed for this board's hardware topology |
| Docker + Docker Compose | Standard `docker compose` |
| Armbian (recommended) | Kernel ships with all Qualcomm drivers built-in |
| NVMe drive | Optional — NVMe health section gracefully hides without it |

**No pip packages.** No Node.js. No build step. The entire app is one Python file using only stdlib modules (`http.server`, `json`, `os`, `subprocess`, `collections`, `re`).

## Architecture

```
dashboard.py  ← single file: embedded HTML/CSS/JS + /api JSON endpoint
Dockerfile    ← Alpine 3.21 + python3 + nvme-cli + docker-cli (~30MB image)
docker-compose.yml
```

The dashboard has endpoints:
- `GET /` — serves the HTML UI (embedded in `dashboard.py`, no static files)
- `GET /stream` — real-time telemetry stream via Server-Sent Events (SSE)
- `GET /api` — returns the latest snapshot as JSON

### Hardware Discovery

All hardware paths are probed dynamically at runtime — no hardcoded sensor indices:

- **Thermal sensors:** Scans `/sys/class/hwmon/` by name (not index), so hwmon renumbering after kernel updates won't break anything
- **GPU:** Auto-detects devfreq path from `/sys/class/devfreq/`
- **NVMe:** Auto-detects block device from `/sys/block/`
- **Network:** Auto-discovers interfaces (filters out `lo`, `veth*`, `br-*`, `docker*`)
- **Disk I/O:** Auto-detects root block device (NVMe, MMC, SATA)

Sensor discovery is lazy (built on first API call), so it survives container restarts and kernel module load ordering.

### Container Privileges

The container requires `privileged: true` and mounts `/sys`, `/proc`, `/dev` read-only to read hardware sensors. It also mounts the Docker socket to enumerate containers. No data leaves the machine — everything is served locally on port 3999.

## Customization

The entire UI is embedded as a Python string in `dashboard.py`. Edit the `DASHBOARD` variable to change:
- Colors/theme (CSS variables at the top)
- Thermal zone labels
- Voltage regulator display names
- Refresh interval (default 2000ms)

No rebuild needed for UI changes — the volume mount serves edits live.

## Disclaimer

This is an **unofficial community project** developed individually, can be freely used or repurposed. This is specifically developed to be leveraged on the Radxa Q6A SBC, especially if you are using it on a headless mode. Not affiliated with, endorsed by, or connected to Radxa Computer Co., Ltd. The Radxa logo and name are used for identification purposes only. All trademarks belong to their respective owners.

## License

MIT
