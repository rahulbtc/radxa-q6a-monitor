# Radxa Q6A System Monitor

Real-time hardware telemetry dashboard for the **Radxa Q6A** (Qualcomm QCS6490). Single-file Python server, zero dependencies beyond Alpine baseline.

Single HTML page, auto-refreshing every 2 seconds, dark-themed.

## What it monitors

| Category | Details |
|----------|---------|
| **CPU** | 8 cores (4× Silver A55, 3× Gold A78, 1× Prime A78+), per-core freq/temp/usage/governor |
| **GPU** | Adreno 643 — frequency, governor, dual thermal sensors |
| **NPU** | Hexagon v68 DSP — ADSP/CDSP state, FastRPC devices, thermal |
| **RAM** | Usage, cached, buffers, swap, zram compression ratios |
| **Disk** | NVMe health (SMART), I/O throughput, temperature sensors |
| **Network** | Per-interface throughput, IPs, DNS/ping connectivity checks |
| **Docker** | All containers with status |
| **Thermal Map** | 35+ thermal zones across CPU/GPU/NPU/DDR/PMIC/modem |
| **Voltages** | 20+ PMIC regulators |
| **Processes** | Top processes by memory, zombie count |

Port **3999**.

## Quick Start

```bash
git clone https://github.com/rahulbtc/radxa-q6a-monitor.git
cd radxa-q6a-monitor
docker compose up -d --build
```

Open `http://<radxa-ip>:3999/`

## Requirements

- Radxa Q6A board (QCS6490)
- Docker + Docker Compose
- NVMe drive (optional — gracefully degrades without it)

## Architecture

```
dashboard.py  ← single file, serves HTML + /api JSON endpoint
Dockerfile    ← Alpine + Python3 + nvme-cli + docker-cli (~30MB)
```

All hardware paths are probed dynamically — no hardcoded hwmon indices. Sensor discovery is lazy (built on first API call), so it survives container restarts cleanly.

## Disclaimer

This is an **unofficial community project**. Not affiliated with, endorsed by, or connected to Radxa Computer Co., Ltd. The Radxa logo and name are used for identification purposes only. All trademarks belong to their respective owners.

## License

MIT
