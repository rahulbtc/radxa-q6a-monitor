#!/usr/bin/env python3
"""Radxa Q6A Monitor — Qualcomm QCS6490 — Port 3999."""
import json, subprocess, os, time, re, signal, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from collections import deque

signal.signal(signal.SIGCHLD, signal.SIG_IGN)
PORT = 3999
HISTORY = 120  # ~4min at 2s intervals

LATEST_STATS = "{}"
STATS_EVENT = threading.Event()

def stats_loop():
    global LATEST_STATS
    while True:
        try:
            LATEST_STATS = json.dumps(get_stats())
            STATS_EVENT.set()
            STATS_EVENT.clear()
        except Exception:
            pass
        time.sleep(2)


# ── History buffers ──
cpu_hist = deque(maxlen=HISTORY)
ram_hist = deque(maxlen=HISTORY)
gpu_hist = deque(maxlen=HISTORY)
net_rx_hist = deque(maxlen=HISTORY)
net_tx_hist = deque(maxlen=HISTORY)
disk_r_hist = deque(maxlen=HISTORY)
disk_w_hist = deque(maxlen=HISTORY)
temp_hist = deque(maxlen=HISTORY)
npu_util_hist = deque(maxlen=HISTORY)
npu_latency_hist = deque(maxlen=HISTORY)

prev_net = {"rx": 0, "tx": 0, "ts": 0}
prev_disk = {"r": 0, "w": 0, "ts": 0}
_nvme_cache = {"ts": 0, "data": {}}
_prev_cpu_core = {}  # per-core jiffies for pct calc

# ── Dynamic hwmon discovery (lazy — built on first use) ──
_hwmon = None

def _ensure_hwmon():
    global _hwmon
    if _hwmon is None:
        _hwmon = {}
        try:
            for d in sorted(os.listdir("/sys/class/hwmon/")):
                name = read_file(f"/sys/class/hwmon/{d}/name")
                if name:
                    _hwmon[name] = d
        except Exception:
            pass

def hwmon_int(name, file="temp1_input"):
    """Read int from a named hwmon sensor, or None."""
    _ensure_hwmon()
    h = _hwmon.get(name)
    return read_int(f"/sys/class/hwmon/{h}/{file}") if h else None


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def read_int(path):
    v = read_file(path)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            pass
    return None


def run(cmd, timeout=5):
    try:
        return subprocess.check_output(
            cmd, shell=True, timeout=timeout, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return ""


def get_nvme_smart():
    now = time.time()
    if now - _nvme_cache["ts"] < 30:
        return _nvme_cache["data"]
    d = {}
    # Auto-detect NVMe device
    _nvme_dev = None
    try:
        for _blk in os.listdir("/sys/block/"):
            if _blk.startswith("nvme"):
                _nvme_dev = _blk
                break
    except Exception:
        pass
    out = run(f"nvme smart-log /dev/{_nvme_dev} 2>/dev/null") if _nvme_dev else ""
    if out:
        for line in out.split("\n"):
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            d[k.strip().lower().replace(" ", "_")] = v.strip()
    _nvme_cache["ts"] = now
    _nvme_cache["data"] = d
    return d


# ── Core map: QCS6490 has 8 cores with specific names ──
# CPU0-3: Kryo Silver (Cortex-A55) @ up to 1.9 GHz
# CPU4-6: Kryo Gold (Cortex-A78) @ up to 2.4 GHz
# CPU7:   Kryo Prime (Cortex-A78+) @ up to 2.7 GHz
# Thermal zones: cpu0-6 = cores 0-6, cpu7-11 = ???
# From hwmon: cpu0..cpu11 — but we have 8 CPUs. Mapping:
# hwmon1=cpu0, hwmon2=cpu1, ..., hwmon9=cpu6, hwmon10=cpu7
# Plus cpuss0 (hwmon5), cpuss1 (hwmon6) — these are CPU subsystem temps
# And cpu8-11 thermal zones exist but no cpu8-11 — these are cluster/CPSS zones

CORE_NAMES = {
    0: ("Silver", "Cortex-A55"),
    1: ("Silver", "Cortex-A55"),
    2: ("Silver", "Cortex-A55"),
    3: ("Silver", "Cortex-A55"),
    4: ("Gold", "Cortex-A78"),
    5: ("Gold", "Cortex-A78"),
    6: ("Gold", "Cortex-A78"),
    7: ("Prime", "Cortex-A78+"),
}


def get_stats():
    s = {}
    now = time.time()

    # ═══ CPU ═══
    # Overall CPU pct
    try:
        with open("/proc/stat") as f:
            line = f.readline().split()
        total = sum(int(x) for x in line[1:])
        idle = int(line[4])
        s["cpu_total"] = total
        s["cpu_idle"] = idle
    except Exception:
        s["cpu_total"] = 0
        s["cpu_idle"] = 0

    try:
        loads = read_file("/proc/loadavg").split()
        s["load"] = [float(loads[0]), float(loads[1]), float(loads[2])]
    except Exception:
        s["load"] = [0, 0, 0]

    num_cores = os.cpu_count() or 8
    s["cores"] = num_cores

    # Per-core stats
    cores = []
    try:
        with open("/proc/stat") as f:
            lines = f.readlines()
        for i in range(num_cores):
            core = {"id": i, "name": CORE_NAMES.get(i, ("Core", ""))}
            # Frequency
            freq = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
            max_freq = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq")
            min_freq = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_min_freq")
            governor = read_file(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor")
            online = read_file(f"/sys/devices/system/cpu/cpu{i}/online")

            core["freq_mhz"] = round(freq / 1000) if freq else None
            core["max_mhz"] = round(max_freq / 1000) if max_freq else None
            core["min_mhz"] = round(min_freq / 1000) if min_freq else None
            core["governor"] = governor or ""
            core["online"] = online != "0"

            # Temperature — dynamic hwmon lookup by cpuN_thermal
            temp = hwmon_int(f"cpu{i}_thermal")
            core["temp"] = round(temp / 1000, 1) if temp else None

            # Per-core usage from /proc/stat
            for line in lines:
                if line.startswith(f"cpu{i} "):
                    parts = line.split()
                    jiffies = sum(int(x) for x in parts[1:])
                    j_idle = int(parts[4])
                    prev = _prev_cpu_core.get(i)
                    if prev:
                        d_total = jiffies - prev["total"]
                        d_idle = j_idle - prev["idle"]
                        core["pct"] = round((1 - d_idle / d_total) * 100, 1) if d_total > 0 else 0
                    else:
                        core["pct"] = 0
                    _prev_cpu_core[i] = {"total": jiffies, "idle": j_idle}
                    break

            cores.append(core)
    except Exception:
        pass
    s["cpu_cores"] = cores

    # Overall CPU pct from delta
    if len(cpu_hist) > 0:
        prev = cpu_hist[-1]
        d_total = s["cpu_total"] - prev.get("total", 0)
        d_idle = s["cpu_idle"] - prev.get("idle", 0)
        s["cpu_pct"] = round((1 - d_idle / d_total) * 100, 1) if d_total > 0 else 0
    else:
        s["cpu_pct"] = 0
    cpu_hist.append({"total": s["cpu_total"], "idle": s["cpu_idle"]})

    # ═══ GPU — Adreno 643 ═══
    gpu = {}
    try:
        # Auto-detect GPU devfreq path
        _gpu_devfreq = None
        try:
            for d in os.listdir("/sys/class/devfreq/"):
                if "gpu" in d.lower() or "3d" in d:
                    _gpu_devfreq = f"/sys/class/devfreq/{d}"
                    break
        except Exception:
            pass
        if not _gpu_devfreq:
            _gpu_devfreq = "/sys/class/devfreq/3d00000.gpu"
        gpu_freq = read_int(f"{_gpu_devfreq}/cur_freq")
        gpu_max = read_int(f"{_gpu_devfreq}/max_freq")
        gpu_min = read_int(f"{_gpu_devfreq}/min_freq")
        gpu_gov = read_file(f"{_gpu_devfreq}/governor")
        gpu["freq_mhz"] = round(gpu_freq / 1_000_000) if gpu_freq else None
        gpu["max_mhz"] = round(gpu_max / 1_000_000) if gpu_max else 812
        gpu["min_mhz"] = round(gpu_min / 1_000_000) if gpu_min else None
        gpu["governor"] = gpu_gov or ""
        gpu["name"] = "Adreno 643"

        # GPU temps — dynamic hwmon lookup
        t0 = hwmon_int("gpuss0_thermal")
        t1 = hwmon_int("gpuss1_thermal")
        gpu["temp"] = round(t0 / 1000, 1) if t0 else None
        gpu["temp2"] = round(t1 / 1000, 1) if t1 else None

        gpu_hist.append(gpu.get("freq_mhz", 0) or 0)
    except Exception:
        gpu_hist.append(0)
    s["gpu"] = gpu

    # ═══ NPU — Hexagon DSP ═══
    npu = {"name": "Hexagon v68 (12 TOPS)"}
    try:
        # remoteproc0 = ADSP, remoteproc1 = CDSP
    # But the names inside are swapped on this platform
        for i, key in [(0, "adsp"), (1, "cdsp")]:
            state = read_file(f"/sys/class/remoteproc/remoteproc{i}/state")
            name = read_file(f"/sys/class/remoteproc/remoteproc{i}/name")
            npu[key] = {"state": state or "unknown", "name": name or ""}

        # FastRPC devices
        fastrpc = []
        for d in ["/dev/fastrpc-cdsp", "/dev/fastrpc-adsp",
                   "/dev/fastrpc-cdsp-secure", "/dev/fastrpc-adsp-secure"]:
            if os.path.exists(d):
                fastrpc.append(d.replace("/dev/", ""))
        npu["fastrpc"] = fastrpc

        # NPU temps — dynamic hwmon lookup
        t0 = hwmon_int("nspss0_thermal")
        t1 = hwmon_int("nspss1_thermal")
        npu["temp"] = round(t0 / 1000, 1) if t0 else None
        npu["temp2"] = round(t1 / 1000, 1) if t1 else None

        # FastRPC process tracking — who has the DSP open
        fastrpc_procs = []
        dsp_active = False
        try:
            for pid_s in os.listdir("/proc"):
                if not pid_s.isdigit() or int(pid_s) == os.getpid():
                    continue
                try:
                    fd_dir = f"/proc/{pid_s}/fd"
                    for fd in os.listdir(fd_dir):
                        link = os.readlink(f"{fd_dir}/{fd}")
                        if "fastrpc-cdsp" in link:
                            comm = read_file(f"/proc/{pid_s}/comm") or "?"
                            fastrpc_procs.append({"pid": int(pid_s), "comm": comm})
                            dsp_active = True
                            break
                except (PermissionError, OSError):
                    pass
        except Exception:
            pass
        npu["fastrpc_procs"] = fastrpc_procs
        npu["dsp_active"] = dsp_active

        # AI Agent inference metrics (port 4210)
        agent = {"available": False}
        try:
            import urllib.request
            with urllib.request.urlopen("http://localhost:4210/api/health", timeout=2) as resp:
                ah = json.loads(resp.read())
                agent["available"] = ah.get("status") == "ok"
                agent["npu_enabled"] = ah.get("npu", False)
            with urllib.request.urlopen("http://localhost:4210/api/kb/status", timeout=2) as resp:
                kb = json.loads(resp.read())
                agent["total_chunks"] = kb.get("total_documents", 0)
                agent["unique_symbols"] = kb.get("unique_symbols", 0)
                job = kb.get("job", {})
                agent["indexer_running"] = job.get("status") in ("running", "paused")
                agent["processed"] = job.get("processed", 0)
                agent["total_files"] = job.get("total_files", 0)
                agent["chunks_per_min"] = round(job.get("throughput_chunks_per_min", 0), 1)
            with urllib.request.urlopen("http://localhost:4210/api/kb/metrics", timeout=2) as resp:
                m = json.loads(resp.read())
                agent["embed_per_sec"] = m.get("throughput", {}).get("embeddings_per_sec", 0)
                agent["processed_today"] = m.get("throughput", {}).get("processed_today", 0)
                agent["processed_hour"] = m.get("throughput", {}).get("processed_last_hour", 0)
                st = m.get("stage_timing", {})
                agent["embed_avg_ms"] = st.get("embed", {}).get("avg_ms", 0)
                agent["extract_avg_ms"] = st.get("extract", {}).get("avg_ms", 0)
        except Exception:
            pass
        npu["agent"] = agent

        # Synthetic utilization estimate
        # Embedding throughput → how busy is the inference pipeline
        cpm = agent.get("chunks_per_min", 0)
        embed_ms = agent.get("embed_avg_ms", 0) or 46  # default CPU estimate
        if cpm > 0:
            busy_frac = min(1.0, (cpm * embed_ms) / 60000)
            npu["inferred_util"] = round(busy_frac * 100, 1)
        else:
            npu["inferred_util"] = 0
        npu_util_hist.append(npu["inferred_util"])
        npu_latency_hist.append(embed_ms if embed_ms and cpm > 0 else 0)
    except Exception:
        pass
    s["npu"] = npu

    # ═══ RAM ═══
    try:
        mi = {}
        for line in read_file("/proc/meminfo").split("\n"):
            parts = line.split()
            if len(parts) >= 2:
                mi[parts[0].rstrip(":")] = int(parts[1])
        total = mi["MemTotal"]
        avail = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0))
        used = total - avail
        cached = mi.get("Cached", 0) + mi.get("Buffers", 0)
        s["ram_total"] = total
        s["ram_used"] = used
        s["ram_cached"] = cached
        s["ram_avail"] = avail
        s["ram_buffers"] = mi.get("Buffers", 0)
        s["ram_free"] = mi.get("MemFree", 0)
        s["ram_shared"] = mi.get("Shmem", 0)
        s["ram_pct"] = round(used / total * 100, 1)
        stotal = mi.get("SwapTotal", 0)
        sfree = mi.get("SwapFree", stotal)
        sused = stotal - sfree
        s["swap_total"] = stotal
        s["swap_used"] = sused
        s["swap_free"] = sfree
        s["swap_pct"] = round(sused / stotal * 100, 1) if stotal else 0
        ram_hist.append(s["ram_pct"])
    except Exception:
        s["ram_pct"] = 0
        ram_hist.append(0)

    # ═══ DISK / NVMe ═══
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        s["disk_total"] = total
        s["disk_used"] = used
        s["disk_free"] = total - used
        s["disk_pct"] = round(used / total * 100, 1)
    except Exception:
        s["disk_pct"] = 0

    # Disk I/O — auto-detect root block device
    _root_disk = "nvme0n1"
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                dev = parts[2] if len(parts) > 2 else ""
                if dev and re.match(r"(nvme\d+n\d+|mmcblk\d+|sd[a-z])$", dev):
                    _root_disk = dev
                    break
    except Exception:
        pass

    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if parts[2] == _root_disk:
                    r_bytes = int(parts[5]) * 512
                    w_bytes = int(parts[9]) * 512
                    dt = now - prev_disk["ts"] if prev_disk["ts"] > 0 else 1
                    dr = max(0, (r_bytes - prev_disk["r"]) / dt) if prev_disk["ts"] > 0 else 0
                    dw = max(0, (w_bytes - prev_disk["w"]) / dt) if prev_disk["ts"] > 0 else 0
                    prev_disk.update({"r": r_bytes, "w": w_bytes, "ts": now})
                    s["disk_r_speed"] = dr
                    s["disk_w_speed"] = dw
                    disk_r_hist.append(dr)
                    disk_w_hist.append(dw)
                    break
    except Exception:
        s["disk_r_speed"] = 0
        s["disk_w_speed"] = 0
        disk_r_hist.append(0)
        disk_w_hist.append(0)

    # NVMe SMART
    nvme = get_nvme_smart()
    nvme_data = {"temps": [], "health": {}}
    # NVMe temp sensors — dynamic hwmon lookup
    _nvme_hwmon = _hwmon.get("nvme")
    for i in range(1, 4):
        t = read_int(f"/sys/class/hwmon/{_nvme_hwmon}/temp{i}_input") if _nvme_hwmon else None
        if t is not None:
            nvme_data["temps"].append(round(t / 1000, 1))

    if nvme:
        def extract_pct(v):
            try:
                return int(re.search(r'(\d+)', v).group(1))
            except Exception:
                return None

        def extract_float(v):
            try:
                return float(re.search(r'([\d.]+)', v).group(1))
            except Exception:
                return None

        nvme_data["health"] = {
            "wear": extract_pct(nvme.get("percentage_used", "0%")),
            "spare": extract_pct(nvme.get("available_spare", "100%")),
            "spare_thresh": extract_pct(nvme.get("available_spare_threshold", "1%")),
            "critical_warning": nvme.get("critical_warning", "0"),
            "media_errors": nvme.get("media_errors", "0"),
            "power_cycles": nvme.get("power_cycles", "?"),
            "power_hours": nvme.get("power_on_hours", "?"),
            "unsafe_shutdowns": nvme.get("unsafe_shutdowns", "0"),
            "controller_busy_h": nvme.get("controller_busy_time", "?"),
            "errors": nvme.get("num_err_log_entries", "0"),
        }
        # Extract NVMe data unit GB values from smart-log output like "890254 (455.81 GB)"
        du_read = nvme.get("data_units_read", "0")
        du_written = nvme.get("data_units_written", "0")
        gb_read_m = re.search(r'\(([\d.]+)\s*GB\)', du_read)
        gb_written_m = re.search(r'\(([\d.]+)\s*GB\)', du_written)
        nvme_data["health"]["total_read_gb"] = round(float(gb_read_m.group(1)), 1) if gb_read_m else None
        nvme_data["health"]["total_written_gb"] = round(float(gb_written_m.group(1)), 1) if gb_written_m else None
    s["nvme"] = nvme_data

    # ═══ VOLTAGES ═══
    regulators = []
    try:
        for r in sorted(os.listdir("/sys/class/regulator/")):
            rpath = f"/sys/class/regulator/{r}"
            status = read_file(f"{rpath}/status") or ""
            if status == "off":
                continue
            name = read_file(f"{rpath}/name") or r
            uv = read_int(f"{rpath}/microvolts")
            regulators.append({
                "name": name,
                "voltage_v": round(uv / 1_000_000, 3) if uv else None,
                "status": status,
            })
    except Exception:
        pass
    s["regulators"] = regulators

    # ═══ THERMALS — Full Map ═══
    thermals = {}
    # Map thermal zone names to friendly labels
    THERMAL_LABELS = {
        "cpu0-thermal": "CPU Core 0",
        "cpu1-thermal": "CPU Core 1",
        "cpu2-thermal": "CPU Core 2",
        "cpu3-thermal": "CPU Core 3",
        "cpuss0-thermal": "CPU Subsystem 0",
        "cpuss1-thermal": "CPU Subsystem 1",
        "cpu4-thermal": "CPU Core 4",
        "cpu5-thermal": "CPU Core 5",
        "cpu6-thermal": "CPU Core 6",
        "cpu7-thermal": "CPU Core 7",
        "cpu8-thermal": "CPU Cluster 0",
        "cpu9-thermal": "CPU Cluster 1",
        "cpu10-thermal": "CPU Cluster 2",
        "cpu11-thermal": "CPU Cluster 3",
        "aoss0-thermal": "AOSS 0",
        "aoss1-thermal": "AOSS 1",
        "gpuss0-thermal": "GPU Shader 0",
        "gpuss1-thermal": "GPU Shader 1",
        "nspss0-thermal": "NPU/DSP 0",
        "nspss1-thermal": "NPU/DSP 1",
        "video-thermal": "VPU (Adreno 633)",
        "ddr-thermal": "DDR Memory",
        "mdmss0-thermal": "Modem 0",
        "mdmss1-thermal": "Modem 1",
        "mdmss2-thermal": "Modem 2",
        "mdmss3-thermal": "Modem 3",
        "camera0-thermal": "Camera",
        "pm8350c-thermal": "PMIC PM8350C",
        "pm7250b-thermal": "PMIC PM7250B",
        "pm7325-thermal": "PMIC PM7325",
        "xo-thermal": "Crystal Oscillator",
        "quiet-thermal": "Quiet Zone",
        "msm-skin-thermal": "Board Skin",
        "ufs-thermal": "UFS Controller",
    }
    try:
        for tz in sorted(os.listdir("/sys/class/thermal/")):
            if not tz.startswith("thermal_zone"):
                continue
            tzpath = f"/sys/class/thermal/{tz}"
            tz_type = read_file(f"{tzpath}/type") or ""
            tz_temp = read_int(f"{tzpath}/temp")
            if tz_temp is not None:
                label = THERMAL_LABELS.get(tz_type, tz_type)
                thermals[tz_type] = {"label": label, "temp": round(tz_temp / 1000, 1)}
    except Exception:
        pass
    s["thermals"] = thermals
    if thermals:
        cpu_temps = [v["temp"] for k, v in thermals.items() if k.startswith("cpu") and "cluster" not in v["label"].lower() and "subsystem" not in v["label"].lower()]
        s["temp_cpu_max"] = round(max(cpu_temps), 1) if cpu_temps else None
        avg_all = sum(v["temp"] for v in thermals.values()) / len(thermals)
        temp_hist.append(round(avg_all, 1))

    # ═══ NETWORK ═══
    net = {}
    # Auto-detect real network interfaces (skip lo, docker, veth, br-)
    interfaces = []
    try:
        for iface in sorted(os.listdir("/sys/class/net/")):
            if iface == "lo" or iface.startswith(("veth", "br-", "docker")):
                continue
            interfaces.append(iface)
    except Exception:
        interfaces = ["enp1s0", "wlan0", "tailscale0"]
    net["interfaces"] = {}
    total_rx = total_tx = 0
    primary_speed_rx = 0
    primary_speed_tx = 0
    primary_iface = None

    try:
        with open("/proc/net/dev") as f:
            f.readline()
            f.readline()
            for line in f:
                parts = line.split()
                iface = parts[0].rstrip(":")
                rx = int(parts[1])
                tx = int(parts[9])
                total_rx += rx
                total_tx += tx
                if iface in interfaces:
                    speed_rx = max(0, (rx - prev_net.get(iface + "_rx", 0)) / max(now - prev_net.get("ts", 1), 0.001)) if prev_net.get("ts") > 0 else 0
                    speed_tx = max(0, (tx - prev_net.get(iface + "_tx", 0)) / max(now - prev_net.get("ts", 1), 0.001)) if prev_net.get("ts") > 0 else 0
                    # Get operational state
                    op_state = read_file(f"/sys/class/net/{iface}/operstate") or "unknown"
                    net["interfaces"][iface] = {
                        "rx": rx, "tx": tx,
                        "speed_rx": speed_rx, "speed_tx": speed_tx,
                        "state": op_state,
                    }
                    prev_net[iface + "_rx"] = rx
                    prev_net[iface + "_tx"] = tx
                    if iface == "enp1s0":
                        primary_speed_rx = speed_rx
                        primary_speed_tx = speed_tx
                        primary_iface = iface

        prev_net["ts"] = now
        net["total_rx"] = total_rx
        net["total_tx"] = total_tx
        s["net_rx_speed"] = primary_speed_rx
        s["net_tx_speed"] = primary_speed_tx
        net_rx_hist.append(primary_speed_rx)
        net_tx_hist.append(primary_speed_tx)
    except Exception:
        net_rx_hist.append(0)
        net_tx_hist.append(0)
    s["net"] = net

    # IPs
    s["ip_lan"] = run("ip route get 1.1.1.1 2>/dev/null | awk '{print $7}'") or run("hostname -I 2>/dev/null | awk '{print $1}'") or ""
    s["ip_tailscale"] = run("tailscale ip -4 2>/dev/null") or "—"

    # DNS checks
    s["dns_pi"] = run("getent hosts test.local 2>/dev/null || timeout 2 nslookup google.com 192.168.1.69 2>/dev/null | grep -q 'Address' && echo OK || echo FAIL") == "OK"
    # Simpler: can we reach the Pi DNS?
    pi_dns = run("timeout 2 nslookup google.com 192.168.1.69 2>/dev/null")
    s["dns_pi"] = "Server:" in pi_dns or "Address" in pi_dns
    s["dns_internet"] = run("getent hosts google.com 2>/dev/null") != ""

    # Pings
    for target, key in [("192.168.1.69", "ping_pi"), ("1.1.1.1", "ping_internet")]:
        ping = run(f"ping -c 1 -W 2 {target} 2>/dev/null | grep 'time='")
        try:
            s[key] = float(re.search(r'time=([\d.]+)', ping).group(1)) if ping else None
        except Exception:
            s[key] = None

    # ═══ UPTIME ═══
    try:
        s["uptime"] = round(float(read_file("/proc/uptime").split()[0]))
    except Exception:
        s["uptime"] = 0

    # ═══ DOCKER ═══
    try:
        out = run("docker ps -a --format '{{.Names}}\\t{{.Status}}\\t{{.State}}'", timeout=5)
        containers = []
        for line in out.split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                clean_name = parts[0].replace("stonk_v3_", "")
                containers.append({
                    "name": clean_name,
                    "raw_name": parts[0],
                    "status": parts[1],
                    "state": parts[2],
                })
        s["containers"] = containers
        s["container_up"] = sum(1 for c in containers if c["state"] == "running")
        s["container_total"] = len(containers)
    except Exception:
        s["containers"] = []
        s["container_up"] = 0
        s["container_total"] = 0

    # ═══ PROCESSES ═══
    procs = []
    try:
        # Read top processes from /proc directly (BusyBox ps lacks cpu/mem columns)
        import heapq
        pid_list = []
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            try:
                stat = read_file(f"/proc/{pid_s}/stat")
                if not stat:
                    continue
                fields = stat.split()
                # fields[1] = comm (in parens), fields[17] = starttime, fields[13] = utime, fields[14] = stime
                # fields[23] = rss (pages)
                comm = fields[1].strip("()")
                state = fields[2]
                if state in ("I", "S", "D", "R", "Z"):
                    try:
                        rss_pages = int(fields[23])
                        rss_mb = round(rss_pages * 4 / 1024, 1)  # 4KB pages
                        pid_list.append((rss_mb, int(pid_s), comm, state))
                    except (ValueError, IndexError):
                        pass
            except (PermissionError, OSError):
                pass
        # Sort by RSS (memory) descending, take top 8
        pid_list.sort(key=lambda x: x[0], reverse=True)
        for rss_mb, pid, comm, state in pid_list[:8]:
            procs.append({
                "pid": pid,
                "cpu": 0,  # CPU% needs delta calculation, show RSS-sorted instead
                "mem": round(rss_mb / (s["ram_total"] / 1024) * 100, 1) if s.get("ram_total") else 0,
                "rss_mb": rss_mb,
                "cmd": comm,
                "state": state,
            })
    except Exception:
        pass

    # System stats — use /proc directly since BusyBox ps is limited
    proc_count = 0
    thread_count = 0
    zombies = 0
    try:
        # Count directories in /proc that are numeric = process count
        proc_count = sum(1 for d in os.listdir("/proc") if d.isdigit())
        # Count threads via /proc/<pid>/task
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    stat = read_file(f"/proc/{pid}/stat")
                    if stat:
                        # 3rd field is state, Z = zombie
                        fields = stat.split()
                        if len(fields) > 2 and fields[2] == "Z":
                            zombies += 1
                    task_dir = f"/proc/{pid}/task"
                    if os.path.isdir(task_dir):
                        thread_count += len(os.listdir(task_dir))
                except (PermissionError, OSError):
                    pass
    except Exception:
        pass

    # Zram compression
    zram = []
    try:
        for i in range(3):
            ds = read_int(f"/sys/block/zram{i}/disksize")
            mm = read_file(f"/sys/block/zram{i}/mm_stat")
            if ds and mm:
                parts = mm.split()
                orig = int(parts[0]) if len(parts) > 0 else 0
                comp = int(parts[1]) if len(parts) > 1 else 0
                if orig > 0:
                    zram.append({
                        "dev": f"zram{i}",
                        "size_mb": round(ds / 1048576),
                        "orig_mb": round(orig / 1048576),
                        "comp_mb": round(comp / 1048576),
                        "ratio": round((1 - comp / orig) * 100, 1) if orig > 0 else 0,
                    })
    except Exception:
        pass

    # OS info
    os_info = {}
    try:
        # Read from host's /proc which is bind-mounted
        os_info["kernel"] = read_file("/proc/sys/kernel/osrelease") or ""
        # Host OS is Armbian — check /proc/version or /etc/os-release from host
        # Since /etc is container's, parse /proc/version instead
        proc_version = read_file("/proc/version") or ""
        if "Ubuntu" in proc_version or "ubuntu" in proc_version:
            os_info["distro"] = "Armbian 26.2.4 (Ubuntu Noble)"
        else:
            os_info["distro"] = "Armbian"
        # Boot time from /proc/stat btime
        for line in read_file("/proc/stat").split("\n"):
            if line.startswith("btime"):
                import datetime
                btime = int(line.split()[1])
                os_info["boot_time"] = datetime.datetime.fromtimestamp(btime).strftime("%Y-%m-%d %H:%M")
                break
    except Exception:
        pass

    s["procs"] = procs[:6]
    s["proc_count"] = proc_count
    s["thread_count"] = thread_count
    s["zombies"] = zombies
    s["zram"] = zram
    s["os_info"] = os_info

    # ═══ HEALTH ═══
    issues = []
    if s.get("cpu_pct", 0) > 80:
        issues.append(f"CPU high: {s['cpu_pct']}%")
    if s.get("ram_pct", 0) > 85:
        issues.append(f"RAM high: {s['ram_pct']}%")
    if s.get("disk_pct", 0) > 85:
        issues.append(f"Disk low: {100 - s['disk_pct']:.0f}% free")
    if s.get("swap_pct", 0) > 50:
        issues.append(f"Swap high: {s['swap_pct']}%")
    if thermals:
        for k, v in thermals.items():
            if v["temp"] > 75:
                issues.append(f"{v['label']} hot: {v['temp']}°C")
    if not s.get("dns_internet"):
        issues.append("Internet DNS failing")
    if s.get("ping_internet") is None:
        issues.append("No internet connectivity")
    if s.get("container_up", 0) < s.get("container_total", 0):
        down = s["container_total"] - s["container_up"]
        issues.append(f"{down} container(s) down")
    nvme_h = s.get("nvme", {}).get("health", {})
    if nvme_h.get("wear") and nvme_h["wear"] > 80:
        issues.append(f"NVMe wear: {nvme_h['wear']}%")
    s["health"] = "OK" if not issues else "ISSUES"
    s["issues"] = issues

    # History
    s["hist"] = {
        "cpu": list(cpu_hist),
        "ram": list(ram_hist),
        "gpu": [g for g in gpu_hist],
        "net_rx": list(net_rx_hist),
        "net_tx": list(net_tx_hist),
        "disk_r": list(disk_r_hist),
        "disk_w": list(disk_w_hist),
        "temp": list(temp_hist),
        "npu_util": list(npu_util_hist),
        "npu_latency": list(npu_latency_hist),
    }
    return s

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Radxa Q6A System Monitor</title>
<style>
:root{--bg:#0a0e14;--surface:#111921;--border:#1a2332;--text:#c5cdd8;--dim:#4a5568;--accent:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--orange:#f0883e;--cyan:#39c5cf;--purple:#bc8cff}
:root.amoled{--bg:#000;--surface:#080808;--border:#151515;--text:#d0d0d0;--dim:#555;--accent:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--orange:#f0883e;--cyan:#39c5cf;--purple:#bc8cff}
.theme-btn{background:none;border:1px solid var(--border);color:var(--dim);border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;vertical-align:middle;margin-left:8px}
.theme-btn:hover{color:var(--text);border-color:var(--accent)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.4;-webkit-font-smoothing:antialiased}
.wrap{max-width:100%;margin:0 auto;padding:0 12px 40px}
/* Header */
header{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:10}
header h1{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:700;color:var(--accent);letter-spacing:-.3px}header h1 svg{height:22px;width:auto;flex-shrink:0}header h1 .dev-name{color:var(--text);font-weight:600;font-size:14px;letter-spacing:-.2px}header h1 .dev-sub{color:var(--dim);font-weight:400;font-size:11px;margin-left:2px}
header .meta{font-size:11px;color:var(--dim);text-align:right}
header .meta span{margin-left:10px}
/* Pills */
.pills{display:flex;gap:6px;padding:8px 0;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.pills::-webkit-scrollbar{display:none}
.pill{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 12px;text-align:center;min-width:72px;flex-shrink:0}
.pill .v{font-size:18px;font-weight:700;line-height:1.2}
.pill .l{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:1px}
/* Sections */
.grid{display:grid;grid-template-columns:1fr;gap:8px;margin-top:8px}
@media(min-width:768px){.grid{grid-template-columns:1fr 1fr;gap:10px}}
@media(min-width:1200px){.grid{grid-template-columns:1fr 1fr 1fr 1fr}}
.sec{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;overflow:hidden}
.sec.w2{grid-column:span 1}
@media(min-width:768px){.sec.w2{grid-column:span 2}}
@media(min-width:1200px){.sec.w2{grid-column:span 2}}
.sec.full{grid-column:1/-1}
.sec h2{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.sec h2 .badge{background:var(--border);color:var(--text);font-size:9px;padding:1px 5px;border-radius:3px;font-weight:500}
canvas{width:100%;height:48px;display:block;margin:4px 0}
/* Rows */
.row{display:flex;justify-content:space-between;align-items:center;padding:2px 0;font-size:12px}
.row .k{color:var(--dim);min-width:110px}
.row .val{font-weight:600;text-align:right;flex:1}
/* Bars */
.bar{height:5px;background:var(--border);border-radius:3px;margin:3px 0 5px;overflow:hidden}
.bar div{height:100%;border-radius:3px;transition:width .5s ease}
/* Colors */
.ok{color:var(--green)}.wn{color:var(--yellow)}.cr{color:var(--red)}.ac{color:var(--accent)}.cy{color:var(--cyan)}.pu{color:var(--purple)}
/* Core grid */
.core-grid{display:grid;grid-template-columns:1fr;gap:4px}
@media(min-width:500px){.core-grid{grid-template-columns:1fr 1fr}}
.core-item{background:var(--bg);border-radius:5px;padding:5px 7px;font-size:11px}
.core-item .core-name{font-weight:600;color:var(--accent);font-size:10px}
.core-item .core-row{display:flex;justify-content:space-between;color:var(--dim);font-size:10px;margin-top:1px}
.core-item .core-row .cv{color:var(--text);font-weight:500}
/* Thermal grid */
.thermal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:3px}
.t-item{background:var(--bg);border-radius:4px;padding:3px 6px;display:flex;justify-content:space-between;align-items:center;font-size:11px}
.t-item .tn{color:var(--dim);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;margin-right:4px}
.t-item .tv{font-weight:700;font-size:11px;flex-shrink:0}
/* Network interfaces */
.iface-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;margin-top:4px}
.iface-box{background:var(--bg);border-radius:5px;padding:6px 8px}
.iface-box .if-name{font-weight:700;color:var(--accent);font-size:11px;margin-bottom:2px}
.iface-box .if-row{display:flex;justify-content:space-between;font-size:10px;color:var(--dim)}
.iface-box .if-row .iv{color:var(--text);font-weight:500}
/* NVMe health */
.nvme-health{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:3px;margin-top:4px}
.nvme-stat{background:var(--bg);border-radius:4px;padding:4px 6px;text-align:center}
.nvme-stat .ns-val{font-size:14px;font-weight:700}
.nvme-stat .ns-lbl{font-size:9px;color:var(--dim);text-transform:uppercase}
/* Voltage grid */
.volt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:3px}
.v-item{background:var(--bg);border-radius:4px;padding:3px 6px;display:flex;justify-content:space-between;font-size:10px}
.v-item .vn{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;margin-right:4px}
.v-item .vv{color:var(--text);font-weight:600;flex-shrink:0}
/* Container table */
.ctbl{width:100%;border-collapse:collapse;font-size:11px}
.ctbl th{text-align:left;color:var(--dim);font-size:9px;text-transform:uppercase;padding:3px 4px;position:sticky;top:0;background:var(--surface)}
.ctbl td{padding:3px 4px;border-bottom:1px solid var(--border)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dot.up{background:var(--green)}.dot.down{background:var(--red)}
.sep{border-top:1px solid var(--border);margin:4px 0}
/* Health */
.h-item{display:flex;align-items:center;gap:5px;padding:2px 0;font-size:11px}
.h-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.h-dot.g{background:var(--green)}.h-dot.r{background:var(--red)}.h-dot.y{background:var(--yellow)}
/* Scrollbar */
.sec-scroll{max-height:220px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.sec-scroll::-webkit-scrollbar{width:4px}
.sec-scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg viewBox='0 0 60 60' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M30.95 13.44l6.59 8.32-4.39 4.38-10.6-8.39a.14.14 0 010-.2l4.67-4.68a3.45 3.45 0 013.73-.57z' fill='%2374BC1F'/%3E%3Cpath d='M24.83 32.72l8.32-6.59 4.38 4.38-6.68 8.56a3.45 3.45 0 01-2.66 1.31 3.45 3.45 0 01-2.44-1.01l-4.73-4.72z' fill='%2374BC1F'/%3E%3Cpath d='M44.11 38.84l-6.59-8.32 4.38-4.38 10.61 8.39a.14.14 0 010 .2l-4.67 4.68a3.45 3.45 0 01-3.73.57z' fill='%2374BC1F'/%3E%3Cpath d='M50.22 19.56l-8.32 6.59-4.38-4.38 6.74-8.52a3.45 3.45 0 012.66-1.31c.87 0 1.7.35 2.31.96l4.67 4.69z' fill='%2374BC1F'/%3E%3C/svg%3E"></head>
<body>
<div class="wrap">
<header>
<h1><svg viewBox="0 0 150 40" fill="none" xmlns="http://www.w3.org/2000/svg"><g clip-path="url(#clip0_20334_14003)"><path d="M4.27206 39.9998H0V21.3617C0 19.036 0.923619 16.8056 2.56767 15.1611C4.21173 13.5166 6.44154 12.5928 8.76658 12.5928H15.8812C16.7957 12.5928 17.6727 12.9562 18.3194 13.603C18.966 14.2498 19.3293 15.1271 19.3293 16.0418V19.7134H8.76658C8.32955 19.7134 7.91041 19.8871 7.60137 20.1962C7.29234 20.5053 7.11873 20.9245 7.11873 21.3617V37.1524C7.11764 37.9072 6.81737 38.6308 6.28376 39.1646C5.75014 39.6984 5.02671 39.9987 4.27206 39.9998Z" fill="#74BC1F"/><path d="M68.7968 40H60.2156C59.0645 40 57.9248 39.7732 56.8614 39.3324C55.798 38.8917 54.8319 38.2457 54.0181 37.4314C53.2044 36.6171 52.5591 35.6504 52.1189 34.5865C51.6788 33.5226 51.4526 32.3824 51.4531 31.2311V21.3413C51.4526 20.1899 51.6788 19.0498 52.1189 17.9859C52.5591 16.922 53.2044 15.9553 54.0181 15.141C54.8319 14.3267 55.798 13.6807 56.8614 13.2399C57.9248 12.7992 59.0645 12.5724 60.2156 12.5724H64.1045C64.5599 12.5707 65.0111 12.6594 65.432 12.8334C65.8529 13.0073 66.2352 13.263 66.5566 13.5857C66.878 13.9084 67.1323 14.2916 67.3047 14.7133C67.4771 15.1349 67.5642 15.5865 67.5609 16.042V19.7136H60.2279C59.7909 19.7136 59.3718 19.8873 59.0627 20.1964C58.7537 20.5055 58.5801 20.9247 58.5801 21.3619V31.2517C58.5801 31.6888 58.7537 32.1081 59.0627 32.4172C59.3718 32.7263 59.7909 32.9 60.2279 32.9H68.7968C69.2338 32.9 69.653 32.7263 69.962 32.4172C70.271 32.1081 70.4446 31.6888 70.4446 31.2517V0H74.1152C75.0297 0 75.9068 0.363382 76.5534 1.01021C77.2001 1.65703 77.5634 2.53431 77.5634 3.44906V31.2311C77.5639 32.3828 77.3375 33.5233 76.8972 34.5874C76.4568 35.6516 75.8111 36.6185 74.9969 37.4328C74.1827 38.2472 73.2161 38.8931 72.1523 39.3336C71.0884 39.7741 69.9482 40.0005 68.7968 40Z" fill="#74BC1F"/><path d="M88.9473 13.4381L95.5387 21.7578L91.1472 26.1423L80.5432 17.7484C80.537 17.7426 80.532 17.7356 80.5286 17.7278C80.5252 17.72 80.5234 17.7115 80.5234 17.703C80.5234 17.6945 80.5252 17.6861 80.5286 17.6783C80.532 17.6705 80.537 17.6635 80.5432 17.6577L85.2149 12.9807C85.4509 12.7441 85.7344 12.5602 86.0467 12.4412C86.3589 12.3222 86.6928 12.2707 87.0264 12.2901C87.36 12.3096 87.6857 12.3996 87.982 12.5541C88.2783 12.7087 88.5385 12.9243 88.7454 13.1867L88.9473 13.4381Z" fill="#74BC1F"/><path d="M82.8321 32.7231L91.1497 26.1299L95.533 30.5143C93.8851 32.5747 90.9478 36.2834 88.7438 39.0937C88.537 39.3545 88.2773 39.5685 87.982 39.7218C87.6866 39.8751 87.3622 39.9642 87.03 39.9833C86.6977 40.0023 86.3653 39.9509 86.0543 39.8324C85.7433 39.7139 85.4609 39.531 85.2256 39.2956L80.4922 34.5733L82.8321 32.7231Z" fill="#74BC1F"/><path d="M102.107 38.8417L95.5156 30.5261L99.8989 26.1416L110.511 34.5314C110.517 34.5376 110.522 34.5449 110.526 34.5531C110.529 34.5612 110.531 34.57 110.531 34.5788C110.531 34.5877 110.529 34.5964 110.526 34.6045C110.522 34.6127 110.517 34.6201 110.511 34.6262L105.839 39.3032C105.603 39.5392 105.319 39.7226 105.007 39.8412C104.695 39.9599 104.361 40.0111 104.028 39.9917C103.695 39.9722 103.369 39.8825 103.073 39.7283C102.777 39.5742 102.516 39.3591 102.309 39.0972L102.107 38.8417Z" fill="#74BC1F"/><path d="M108.224 19.5573L99.9067 26.1504L95.5234 21.7577L102.267 13.2402C102.474 12.9643 102.739 12.737 103.043 12.5739C103.347 12.4108 103.683 12.316 104.027 12.296C104.371 12.276 104.716 12.3313 105.037 12.4581C105.357 12.5848 105.647 12.78 105.884 13.03L110.56 17.7194L108.224 19.5573Z" fill="#74BC1F"/><path d="M129.766 12.5928H117.514C116.599 12.5928 115.722 12.9562 115.076 13.603C114.429 14.2498 114.066 15.1271 114.066 16.0418V19.7134H129.766C130.203 19.7134 130.622 19.8871 130.931 20.1962C131.24 20.5053 131.413 20.9245 131.413 21.3617V31.2515C131.413 31.6886 131.24 32.1079 130.931 32.417C130.622 32.7261 130.203 32.8998 129.766 32.8998H121.526C121.259 32.912 120.993 32.8699 120.742 32.7761C120.492 32.6823 120.263 32.5387 120.07 32.3541C119.877 32.1694 119.723 31.9474 119.618 31.7016C119.513 31.4558 119.459 31.1912 119.459 30.9239C119.459 30.6565 119.513 30.392 119.618 30.1461C119.723 29.9003 119.877 29.6784 120.07 29.4937C120.263 29.309 120.492 29.1654 120.742 29.0716C120.993 28.9779 121.259 28.9358 121.526 28.948H128.53V26.2489C128.529 25.5644 128.256 24.9083 127.772 24.4246C127.287 23.941 126.631 23.6693 125.947 23.6693H120.591C118.458 23.7173 116.428 24.5986 114.936 26.1246C113.445 27.6507 112.609 29.7002 112.609 31.8346C112.609 33.9689 113.445 36.0184 114.936 37.5445C116.428 39.0705 118.458 39.9519 120.591 39.9998H129.766C132.091 39.9998 134.32 39.0759 135.964 37.4314C137.608 35.7869 138.532 33.5565 138.532 31.2309V21.3411C138.527 19.019 137.601 16.7939 135.957 15.1538C134.314 13.5138 132.087 12.5928 129.766 12.5928Z" fill="#74BC1F"/><path d="M38.312 12.5928H26.0973C25.1842 12.595 24.3093 12.9593 23.6644 13.6059C23.0195 14.2525 22.6574 15.1285 22.6574 16.0418V19.7134H38.312C38.749 19.7134 39.1682 19.8871 39.4772 20.1962C39.7862 20.5053 39.9598 20.9245 39.9598 21.3617V31.2515C39.9598 31.6886 39.7862 32.1079 39.4772 32.417C39.1682 32.7261 38.749 32.8998 38.312 32.8998H30.0727C29.8057 32.912 29.539 32.8699 29.2887 32.7761C29.0385 32.6823 28.8098 32.5387 28.6165 32.3541C28.4233 32.1694 28.2694 31.9474 28.1643 31.7016C28.0593 31.4558 28.0051 31.1912 28.0051 30.9239C28.0051 30.6565 28.0593 30.392 28.1643 30.1461C28.2694 29.9003 28.4233 29.6784 28.6165 29.4937C28.8098 29.309 29.0385 29.1654 29.2887 29.0716C29.539 28.9779 29.8057 28.9358 30.0727 28.948H37.0761V26.2489C37.075 25.5644 36.8024 24.9083 36.3181 24.4246C35.8338 23.941 35.1774 23.6693 34.4931 23.6693H29.1376C28.0502 23.6449 26.9688 23.838 25.957 24.2374C24.9453 24.6367 24.0235 25.2343 23.2458 25.9949C22.4681 26.7555 21.8501 27.6639 21.4283 28.6667C21.0064 29.6696 20.7891 30.7466 20.7891 31.8346C20.7891 32.9225 21.0064 33.9996 21.4283 35.0024C21.8501 36.0052 22.4681 36.9136 23.2458 37.6742C24.0235 38.4348 24.9453 39.0324 25.957 39.4317C26.9688 39.8311 28.0502 40.0242 29.1376 39.9998H38.312C39.463 39.9998 40.6028 39.773 41.6662 39.3322C42.7296 38.8915 43.6957 38.2455 44.5094 37.4312C45.3232 36.6169 45.9685 35.6502 46.4086 34.5863C46.8487 33.5224 47.075 32.3822 47.0745 31.2309V21.3411C47.0701 19.0194 46.145 16.7943 44.5022 15.1541C42.8594 13.5139 40.6331 12.5928 38.312 12.5928Z" fill="#74BC1F"/><path d="M150.001 11.4966C150.002 12.4023 149.737 13.2883 149.237 14.0441C148.738 14.7998 148.028 15.3917 147.194 15.746C146.361 16.1003 145.442 16.2013 144.551 16.0363C143.661 15.8713 142.839 15.4477 142.188 14.8183C141.537 14.1889 141.085 13.3817 140.89 12.4973C140.694 11.6129 140.764 10.6905 141.089 9.84525C141.415 9.00002 141.982 8.26935 142.72 7.74446C143.458 7.21956 144.334 6.92368 145.239 6.89373C145.856 6.87501 146.47 6.98009 147.046 7.20275C147.621 7.42542 148.147 7.76116 148.591 8.19018C149.035 8.6192 149.388 9.13279 149.63 9.70067C149.872 10.2685 149.999 10.8792 150.001 11.4966ZM141.379 11.4966C141.365 12.0347 141.46 12.57 141.657 13.0708C141.854 13.5715 142.15 14.0276 142.527 14.4118C142.904 14.796 143.354 15.1006 143.851 15.3074C144.347 15.5142 144.881 15.6191 145.419 15.6158C145.957 15.6124 146.489 15.501 146.983 15.2881C147.477 15.0752 147.924 14.7651 148.296 14.3763C148.668 13.9874 148.958 13.5278 149.149 13.0246C149.34 12.5214 149.428 11.985 149.408 11.4471C149.402 10.9185 149.292 10.3962 149.084 9.91057C148.875 9.4249 148.572 8.98551 148.192 8.61789C147.812 8.25028 147.363 7.96175 146.871 7.76906C146.379 7.57636 145.854 7.48333 145.325 7.49535C144.276 7.522 143.279 7.95488 142.542 8.7028C141.806 9.45072 141.389 10.4553 141.379 11.5048V11.4966Z" fill="#74BC1F"/><path d="M144.523 11.7935V14.0105H143.547V8.80599H143.662C144.346 8.80599 145.026 8.80599 145.722 8.80599C146.136 8.79992 146.547 8.87845 146.929 9.03675C147.215 9.14651 147.45 9.35864 147.589 9.63199C147.727 9.90534 147.759 10.2204 147.679 10.5161C147.636 10.7921 147.507 11.0475 147.31 11.2458C147.113 11.4441 146.859 11.5752 146.583 11.6205H146.538L146.517 11.6411L147.662 14.0105C147.304 14.0105 146.958 14.0105 146.612 14.0105C146.579 14.0105 146.538 13.961 146.517 13.9239C146.258 13.3841 146.002 12.8402 145.743 12.3004C145.673 12.152 145.607 12.0037 145.528 11.8595C145.518 11.8411 145.504 11.8253 145.487 11.8132C145.47 11.8011 145.45 11.7929 145.43 11.7894C145.133 11.7894 144.836 11.7935 144.523 11.7935ZM144.523 9.43646V11.1713C144.791 11.1713 145.051 11.1713 145.31 11.1713C145.529 11.1685 145.748 11.1534 145.965 11.126C146.074 11.117 146.18 11.0865 146.277 11.0363C146.374 10.9861 146.459 10.9171 146.529 10.8334C146.616 10.7224 146.672 10.5912 146.694 10.4522C146.715 10.3132 146.7 10.171 146.651 10.0393C146.602 9.90752 146.52 9.79053 146.413 9.69944C146.306 9.60836 146.177 9.5462 146.039 9.51888C145.54 9.43803 145.033 9.4104 144.527 9.43646H144.523Z" fill="#74BC1F"/></g><defs><clipPath id="clip0_20334_14003"><rect width="150" height="40" fill="white"/></clipPath></defs></svg><span class="dev-name">Dragon Q6A</span><span class="dev-sub">QCS6490</span></h1>
<div class="meta" id="meta"><span id="meta-text">loading...</span><button class="theme-btn" id="theme-btn" onclick="toggleTheme()">☀ AMOLED</button></div>
</header>
<div class="pills" id="pills"></div>
<div class="grid" id="grid"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function toggleTheme(){
  const r=document.documentElement;
  const btn=$('theme-btn');
  if(r.classList.contains('amoled')){r.classList.remove('amoled');btn.textContent='☀ AMOLED';localStorage.setItem('theme','dark')}
  else{r.classList.add('amoled');btn.textContent='☀ Dark';localStorage.setItem('theme','amoled')}
}
if(localStorage.getItem('theme')==='amoled'){document.documentElement.classList.add('amoled');document.addEventListener('DOMContentLoaded',function(){const b=$('theme-btn');if(b)b.textContent='☀ Dark'})}
function tc(v,g,w){return v>w?'cr':v>g?'wn':'ok'}
function fmtB(b){if(!b||b<0)return '0 B';if(b<1024)return b.toFixed(0)+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB'}
function fmtUp(s){if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';if(s<86400)return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';return Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h'}
function fmtMHz(m){return m?m+' MHz':'—'}
function bar(pct,max_color){const c=pct>85?'var(--red)':pct>65?'var(--yellow)':'var(--green)';return '<div class="bar"><div style="width:'+Math.min(pct,100)+'%;background:'+c+'"></div></div>'}
function spark(id,data,color,fixed_max){
  const c=$(id);if(!c||!data||!data.length)return;
  const ctx=c.getContext('2d'),dpr=window.devicePixelRatio||1;
  const w=c.offsetWidth*dpr,h=c.offsetHeight*dpr;
  c.width=w;c.height=h;ctx.clearRect(0,0,w,h);
  let mx=fixed_max||0;
  if(!fixed_max){for(let i=0;i<data.length;i++){if(data[i]!=null&&data[i]>mx)mx=data[i]}}
  if(mx<=0)mx=1;
  const p=2*dpr,gw=w-p*2,gh=h-p*2;
  // grid lines
  ctx.strokeStyle='#1a2332';ctx.lineWidth=dpr*.5;
  for(let i=0;i<3;i++){let y=p+gh*i/2;ctx.beginPath();ctx.moveTo(p,y);ctx.lineTo(w-p,y);ctx.stroke()}
  // line
  ctx.beginPath();const step=gw/(data.length-1||1);
  for(let i=0;i<data.length;i++){if(data[i]==null)continue;let x=p+i*step,y=p+gh-((data[i])/mx)*gh;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)}
  ctx.strokeStyle=color;ctx.lineWidth=dpr*1.5;ctx.stroke();
  // fill
  const last=data.length-1;
  ctx.lineTo(p+last*step,h-p);ctx.lineTo(p,h-p);ctx.closePath();
  ctx.fillStyle=color.replace('rgb','rgba').replace(')',',0.06)');ctx.fill()
}
function dualSpark(id,d1,d2,c1,c2,fixed_max){
  const c=$(id);if(!c)return;
  const ctx=c.getContext('2d'),dpr=window.devicePixelRatio||1;
  const w=c.offsetWidth*dpr,h=c.offsetHeight*dpr;
  c.width=w;c.height=h;ctx.clearRect(0,0,w,h);
  let mx=fixed_max||0;
  if(!fixed_max){const all=d1.concat(d2);for(let i=0;i<all.length;i++){if(all[i]!=null&&all[i]>mx)mx=all[i]}}
  if(mx<=0)mx=1;
  const p=2*dpr,gw=w-p*2,gh=h-p*2,step=gw/(d1.length-1||1);
  ctx.strokeStyle='#1a2332';ctx.lineWidth=dpr*.5;
  for(let i=0;i<3;i++){let y=p+gh*i/2;ctx.beginPath();ctx.moveTo(p,y);ctx.lineTo(w-p,y);ctx.stroke()}
  function drawLine(data,color){ctx.beginPath();for(let i=0;i<data.length;i++){if(data[i]==null)continue;let x=p+i*step,y=p+gh-(data[i]/mx)*gh;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)}ctx.strokeStyle=color;ctx.lineWidth=dpr*1.5;ctx.stroke()}
  drawLine(d1,c1);drawLine(d2,c2)
}

function render(d){
  // Header
  const ipStr = d.ip_lan ? '<span>'+d.ip_lan+'</span>' : '';
  $('meta-text').innerHTML = ipStr + '<span>'+new Date().toLocaleTimeString()+'</span><span>↑ '+fmtUp(d.uptime)+'</span>';

  // Pills
  const cpuC=tc(d.cpu_pct,60,80),ramC=tc(d.ram_pct,75,90),diskC=tc(d.disk_pct,80,90);
  const tempMax=d.temp_cpu_max||0,tempC=tempMax>75?'cr':tempMax>60?'wn':'ok';
  const hc=d.health==='OK'?'ok':'wn';
  $('pills').innerHTML=[
    {l:'CPU',v:d.cpu_pct.toFixed(0)+'%',c:cpuC},
    {l:'RAM',v:d.ram_pct.toFixed(0)+'%',c:ramC},
    {l:'DISK',v:d.disk_pct.toFixed(0)+'%',c:diskC},
    {l:'LOAD',v:d.load[0].toFixed(2),c:tc(d.load[0],4,7)},
    {l:'TEMP',v:(tempMax||'—')+'°',c:tempC},
    {l:'DOCKER',v:d.container_up+'/'+d.container_total,c:d.container_up===d.container_total?'ok':'cr'},
    {l:'HEALTH',v:d.health,c:hc},
  ].map(x=>'<div class="pill"><div class="v '+x.c+'">'+x.v+'</div><div class="l">'+x.l+'</div></div>').join('');

  let h='';

  // ═══ CPU ═══
  h+='<div class="sec w2"><h2>CPU — Qualcomm QCS6490 <span class="badge">8 Cores</span></h2>';
  h+=bar(d.cpu_pct);
  h+='<canvas id="ch-cpu"></canvas>';
  h+='<div class="row"><span class="k">Overall</span><span class="val '+cpuC+'">'+d.cpu_pct+'%</span></div>';
  h+='<div class="row"><span class="k">Load Avg</span><span class="val">'+d.load.map(l=>l.toFixed(2)).join(' / ')+'</span></div>';
  h+='<div class="sep"></div>';
  h+='<div class="core-grid">';
  // Group: 1 Prime, 3 Gold, 4 Silver — numbered 1-8 with proper names
  const coreLayout=[
    {idx:7, num:1, name:'Kryo Gold Plus', arch:'Cortex-A78+', tag:'prime'},
    {idx:4, num:2, name:'Kryo Gold', arch:'Cortex-A78', tag:'gold'},
    {idx:5, num:3, name:'Kryo Gold', arch:'Cortex-A78', tag:'gold'},
    {idx:6, num:4, name:'Kryo Gold', arch:'Cortex-A78', tag:'gold'},
    {idx:0, num:5, name:'Kryo Silver', arch:'Cortex-A55', tag:'silver'},
    {idx:1, num:6, name:'Kryo Silver', arch:'Cortex-A55', tag:'silver'},
    {idx:2, num:7, name:'Kryo Silver', arch:'Cortex-A55', tag:'silver'},
    {idx:3, num:8, name:'Kryo Silver', arch:'Cortex-A55', tag:'silver'},
  ];
  const tagColors={prime:'var(--accent)',gold:'var(--yellow)',silver:'var(--dim)'};
  for(let ci=0;ci<coreLayout.length;ci++){
    const cl=coreLayout[ci];
    const core=d.cpu_cores[cl.idx];
    if(!core)continue;
    const pctC=tc(core.pct||0,60,80);
    h+='<div class="core-item" style="border-left:2px solid '+tagColors[cl.tag]+'">';
    h+='<div class="core-name">#'+cl.num+' '+cl.name+' <span style="color:var(--dim);font-weight:400">('+cl.arch+')</span></div>';
    h+='<div class="core-row"><span>'+fmtMHz(core.freq_mhz)+' / '+fmtMHz(core.max_mhz)+'</span><span class="cv '+tc(core.temp,60,75)+'">'+(core.temp||'—')+'°C</span></div>';
    h+='<div class="core-row"><span>'+core.governor+'</span><span class="cv '+pctC+'">'+(core.pct||0).toFixed(0)+'%</span></div>';
    h+='</div>';
  }
  h+='</div></div>';

  // ═══ GPU ═══
  const g=d.gpu||{};
  const gFreq=g.freq_mhz||0,gMax=g.max_mhz||812;
  const gPct=gMax?Math.round(gFreq/gMax*100):0;
  h+='<div class="sec"><h2>GPU — Adreno 643</h2>';
  h+=bar(gPct);
  h+='<canvas id="ch-gpu"></canvas>';
  h+='<div class="row"><span class="k">Clock</span><span class="val ac">'+fmtMHz(g.freq_mhz)+'</span></div>';
  h+='<div class="row"><span class="k">Max Clock</span><span class="val">'+fmtMHz(g.max_mhz)+'</span></div>';
  h+='<div class="row"><span class="k">Governor</span><span class="val">'+(g.governor||'—')+'</span></div>';
  if(g.temp)h+='<div class="row"><span class="k">Temperature</span><span class="val '+tc(g.temp,55,70)+'">'+g.temp+'°C</span></div>';
  if(g.temp2)h+='<div class="row"><span class="k">Shader Temp 2</span><span class="val '+tc(g.temp2,55,70)+'">'+g.temp2+'°C</span></div>';
  h+='</div>';

  // ═══ NPU ═══
  const n=d.npu||{};
  h+='<div class="sec"><h2>NPU — Hexagon v68 <span class="badge">12 TOPS</span></h2>';
  
  // Compact Subsystem Status
  const cdsp = n.cdsp && n.cdsp.state==='running';
  const adsp = n.adsp && n.adsp.state==='running';
  h+='<div class="row"><span class="k">Subsystems</span><span class="val">';
  h+='<span class="'+(cdsp?'ok':'wn')+'">CDSP</span> / <span class="'+(adsp?'ok':'wn')+'">ADSP</span>';
  h+='</span></div>';

  // Compact FastRPC
  const fastrpc = (n.fastrpc&&n.fastrpc.length)?n.fastrpc.join(', '):'none';
  h+='<div class="row"><span class="k">FastRPC</span><span class="val" style="font-size:10px;color:var(--dim)">'+fastrpc+'</span></div>';

  // Temps
  if(n.temp || n.temp2) {
    h+='<div class="row"><span class="k">Temperatures</span><span class="val">';
    if(n.temp) h+='<span class="'+tc(n.temp,55,70)+'">'+n.temp+'°C</span>';
    if(n.temp && n.temp2) h+=' <span style="color:var(--dim)">/</span> ';
    if(n.temp2) h+='<span class="'+tc(n.temp2,55,70)+'">'+n.temp2+'°C</span>';
    h+='</span></div>';
  }

  h+='<div class="sep"></div>';
  
  // DSP Activity
  h+='<div class="row"><span class="k">Hardware Status</span><span class="val '+(n.dsp_active?'ok':'wn')+'">'+(n.dsp_active?'ACTIVE':'IDLE')+'</span></div>';
  if(n.fastrpc_procs&&n.fastrpc_procs.length){
    n.fastrpc_procs.forEach(function(p){h+='<div class="row"><span class="k">  → Process</span><span class="val">'+p.comm+' ('+p.pid+')</span></div>'})
  }

  // Synthetic utilization
  const util=n.inferred_util||0;
  const utilC=util>50?'wn':util>0?'ok':'';
  h+='<div class="row"><span class="k">Inferred Load</span><span class="val '+utilC+'">'+util+'%</span></div>';
  h+=bar(util);
  h+='<canvas id="ch-npu-util"></canvas>';
  h+='</div>';

  // ═══ AI AGENT (SOFTWARE) ═══
  const ag = d.npu && d.npu.agent ? d.npu.agent : {};
  h+='<div class="sec"><h2>AI Agent <span class="badge">Application Metrics</span></h2>';
  if(ag.available){
    h+='<div class="row"><span class="k">Status</span><span class="val ok">Connected</span></div>';
    h+='<div class="row"><span class="k">Backend</span><span class="val">'+(ag.npu_enabled?'NPU (QNN)':'CPU (ONNX)')+'</span></div>';
    h+='<div class="sep"></div>';
    const eps = ag.embed_per_sec ?? 0;
    const cpm = ag.chunks_per_min ?? 0;
    h+='<div class="row"><span class="k">Embed/s</span><span class="val pu">'+(eps > 0 ? eps : '—')+'</span></div>';
    h+='<div class="row"><span class="k">Chunks/min</span><span class="val ac">'+(cpm > 0 ? cpm : '—')+'</span></div>';
    if(ag.embed_avg_ms) h+='<div class="row"><span class="k">Avg Embed</span><span class="val">'+ag.embed_avg_ms.toFixed(0)+'ms</span></div>';
    if(ag.extract_avg_ms) h+='<div class="row"><span class="k">Avg Extract</span><span class="val">'+ag.extract_avg_ms.toFixed(0)+'ms</span></div>';
    h+='<div class="sep"></div>';
    h+='<div class="row"><span class="k">ChromaDB</span><span class="val ok">'+(ag.total_chunks ? ag.total_chunks.toLocaleString() : '0')+' chunks</span></div>';
    h+='<div class="row"><span class="k">Symbols</span><span class="val">'+(ag.unique_symbols ?? '—')+'</span></div>';
    h+='<div class="row"><span class="k">Processed</span><span class="val">'+(ag.processed_today ?? 0)+' (today) / '+(ag.processed_hour ?? 0)+' (hour)</span></div>';
  } else {
    h+='<div class="row"><span class="k">Status</span><span class="val wn">Offline</span></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">Backend</span><span class="val" style="color:var(--dim)">—</span></div>';
    h+='<div class="sep"></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">Embed/s</span><span class="val" style="color:var(--dim)">—</span></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">Chunks/min</span><span class="val" style="color:var(--dim)">—</span></div>';
    h+='<div class="sep"></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">ChromaDB</span><span class="val" style="color:var(--dim)">—</span></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">Symbols</span><span class="val" style="color:var(--dim)">—</span></div>';
    h+='<div class="row"><span class="k" style="color:var(--dim)">Processed</span><span class="val" style="color:var(--dim)">—</span></div>';
  }
  h+='</div>';

  // ═══ RAM ═══
  const ramGB=d.ram_used/1048576,ramTB=d.ram_total/1048576;
  h+='<div class="sec"><h2>Memory — '+(d.ram_total/1048576).toFixed(0)+' GB DDR</h2>';
  h+=bar(d.ram_pct);
  h+='<canvas id="ch-ram"></canvas>';
  h+='<div class="row"><span class="k">Used</span><span class="val '+ramC+'">'+ramGB.toFixed(1)+' / '+ramTB.toFixed(0)+' GB ('+d.ram_pct+'%)</span></div>';
  h+='<div class="row"><span class="k">Cached</span><span class="val">'+(d.ram_cached/1048576).toFixed(1)+' GB</span></div>';
  h+='<div class="row"><span class="k">Buffers</span><span class="val">'+(d.ram_buffers/1048576).toFixed(2)+' GB</span></div>';
  h+='<div class="row"><span class="k">Available</span><span class="val ok">'+(d.ram_avail/1048576).toFixed(1)+' GB</span></div>';
  h+='<div class="row"><span class="k">Free</span><span class="val">'+(d.ram_free/1048576).toFixed(1)+' GB</span></div>';
  h+='<div class="row"><span class="k">Shared</span><span class="val">'+(d.ram_shared/1048576).toFixed(2)+' GB</span></div>';
  h+='<div class="sep"></div>';
  h+='<div class="row"><span class="k">Swap (zram)</span><span class="val '+tc(d.swap_pct,30,60)+'">'+(d.swap_used/1048576).toFixed(0)+' / '+(d.swap_total/1048576).toFixed(0)+' MB ('+d.swap_pct+'%)</span></div>';
  h+='</div>';

  // ═══ NVMe ═══
  const nv=d.nvme||{},nh=nv.health||{};
  h+='<div class="sec"><h2>NVMe SSD — 238.5 GB</h2>';
  h+=bar(d.disk_pct);
  h+='<canvas id="ch-disk"></canvas>';
  h+='<div class="row"><span class="k">Used</span><span class="val '+diskC+'">'+(d.disk_used/1073741824).toFixed(1)+' / '+(d.disk_total/1073741824).toFixed(0)+' GB ('+d.disk_pct+'%)</span></div>';
  h+='<div class="row"><span class="k">Read</span><span class="val cy">'+fmtB(d.disk_r_speed)+'/s</span></div>';
  h+='<div class="row"><span class="k">Write</span><span class="val">'+fmtB(d.disk_w_speed)+'/s</span></div>';
  if(nv.temps&&nv.temps.length)h+='<div class="row"><span class="k">Temps</span><span class="val">'+nv.temps.map(t=>'<span class="'+tc(t,40,50)+'">'+t+'°C</span>').join(' / ')+'</span></div>';
  h+='<div class="sep"></div>';
  h+='<div class="nvme-health">';
  if(nh.wear!==undefined)h+='<div class="nvme-stat"><div class="ns-val '+(nh.wear>50?'cr':nh.wear>20?'wn':'ok')+'">'+nh.wear+'%</div><div class="ns-lbl">Wear</div></div>';
  if(nh.spare!==undefined)h+='<div class="nvme-stat"><div class="ns-val ok">'+nh.spare+'%</div><div class="ns-lbl">Spare</div></div>';
  if(nh.power_cycles)h+='<div class="nvme-stat"><div class="ns-val ac">'+nh.power_cycles+'</div><div class="ns-lbl">Power Cycles</div></div>';
  if(nh.power_hours)h+='<div class="nvme-stat"><div class="ns-val">'+nh.power_hours+'</div><div class="ns-lbl">Power Hours</div></div>';
  if(nh.media_errors)h+='<div class="nvme-stat"><div class="ns-val ok">'+nh.media_errors+'</div><div class="ns-lbl">Media Errors</div></div>';
  if(nh.unsafe_shutdowns)h+='<div class="nvme-stat"><div class="ns-val wn">'+nh.unsafe_shutdowns+'</div><div class="ns-lbl">Unsafe Shutdowns</div></div>';
  if(nh.total_read_gb)h+='<div class="nvme-stat"><div class="ns-val">'+nh.total_read_gb+' GB</div><div class="ns-lbl">Total Read</div></div>';
  if(nh.total_written_gb)h+='<div class="nvme-stat"><div class="ns-val">'+nh.total_written_gb+' GB</div><div class="ns-lbl">Total Written</div></div>';
  h+='</div></div>';

  // ═══ NETWORK ═══
  const net=d.net||{};
  h+='<div class="sec"><h2>Network</h2>';
  h+='<canvas id="ch-net"></canvas>';
  // DNS & Ping
  h+='<div class="row"><span class="k">DNS Pi (192.168.1.69)</span><span class="val '+(d.dns_pi?'ok':'cr')+'">'+(d.dns_pi?'OK':'FAIL')+'</span></div>';
  h+='<div class="row"><span class="k">DNS Internet</span><span class="val '+(d.dns_internet?'ok':'cr')+'">'+(d.dns_internet?'OK':'FAIL')+'</span></div>';
  h+='<div class="row"><span class="k">Ping Pi</span><span class="val '+(d.ping_pi!=null?d.ping_pi.toFixed(1)+'ms':'TIMEOUT')+'"></span></div>';
  h+='<div class="row"><span class="k">Ping 1.1.1.1</span><span class="val '+(d.ping_internet!=null?d.ping_internet.toFixed(1)+'ms':'<span class=\"cr\">TIMEOUT</span>')+'"></span></div>';
  h+='<div class="sep"></div>';
  h+='<div class="iface-grid">';
  const ifaceLabels={'enp1s0':'LAN (Ethernet)','wlan0':'WiFi','tailscale0':'Tailscale VPN'};
  for(const[iface,info] of Object.entries(net.interfaces||{})){
    const label=ifaceLabels[iface]||iface;
    h+='<div class="iface-box">';
    h+='<div class="if-name">'+label+' <span class="'+(info.state==='up'?'ok':'wn')+'" style="font-size:9px">'+info.state.toUpperCase()+'</span></div>';
    h+='<div class="if-row"><span>↓ RX</span><span class="iv">'+fmtB(info.speed_rx)+'/s</span></div>';
    h+='<div class="if-row"><span>↑ TX</span><span class="iv">'+fmtB(info.speed_tx)+'/s</span></div>';
    h+='<div class="if-row"><span>Total ↓</span><span class="iv">'+fmtB(info.rx)+'</span></div>';
    h+='<div class="if-row"><span>Total ↑</span><span class="iv">'+fmtB(info.tx)+'</span></div>';
    h+='</div>';
  }
  h+='</div></div>';

  // ═══ PROCESSES ═══
  h+='<div class="sec">';
  h+='<h2>Top Processes <span class="badge">'+d.proc_count+' procs / '+d.thread_count+' threads</span></h2>';
  if(d.zombies>0) h+='<div class="row"><span class="k">Zombies</span><span class="val cr">'+d.zombies+'</span></div>';
  h+='<div class="sec-scroll"><table class="ctbl"><thead><tr><th>Process</th><th>MEM</th><th>RSS</th></tr></thead><tbody>';
  (d.procs||[]).forEach(p=>{
    h+='<tr><td style="font-size:10px;color:var(--text);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+p.cmd+'</td>';
    h+='<td class="'+(p.mem>20?'cr':p.mem>10?'wn':'')+'" style="font-size:10px">'+p.mem.toFixed(1)+'%</td>';
    h+='<td style="font-size:10px;color:var(--dim)">'+p.rss_mb+'M</td></tr>';
  });
  h+='</tbody></table></div>';
  // Zram compression
  if(d.zram&&d.zram.length){
    h+='<div class="sep"></div>';
    d.zram.forEach(z=>{
      if(z.orig_mb>1) h+='<div class="row"><span class="k">'+z.dev+'</span><span class="val"><span class="cy">'+z.orig_mb+'M</span> → '+z.comp_mb+'M <span class="ok">('+z.ratio+'% saved)</span></span></div>';
    });
  }
  // OS info
  if(d.os_info&&d.os_info.kernel){
    h+='<div class="sep"></div>';
    h+='<div class="row"><span class="k">Kernel</span><span class="val" style="font-size:10px">'+d.os_info.kernel+'</span></div>';
    if(d.os_info.distro) h+='<div class="row"><span class="k">OS</span><span class="val" style="font-size:10px">'+d.os_info.distro+'</span></div>';
    if(d.os_info.boot_time) h+='<div class="row"><span class="k">Boot</span><span class="val" style="font-size:10px">'+d.os_info.boot_time+'</span></div>';
  }
  h+='</div>';

  // ═══ THERMALS ═══
  const th=d.thermals||{};
  const thermalOrder=['cpu0-thermal','cpu1-thermal','cpu2-thermal','cpu3-thermal','cpu4-thermal','cpu5-thermal','cpu6-thermal','cpu7-thermal',
    'cpuss0-thermal','cpuss1-thermal','cpu8-thermal','cpu9-thermal','cpu10-thermal','cpu11-thermal',
    'gpuss0-thermal','gpuss1-thermal','nspss0-thermal','nspss1-thermal',
    'ddr-thermal','video-thermal','ufs-thermal',
    'mdmss0-thermal','mdmss1-thermal','mdmss2-thermal','mdmss3-thermal',
    'aoss0-thermal','aoss1-thermal',
    'pm8350c-thermal','pm7250b-thermal','pm7325-thermal',
    'xo-thermal','camera0-thermal','quiet-thermal','msm-skin-thermal'];
  h+='<div class="sec w2"><h2>Thermal Map <span class="badge">'+Object.keys(th).length+' Sensors</span></h2>';
  h+='<canvas id="ch-temp"></canvas>';
  h+='<div class="thermal-grid">';
  for(const tz of thermalOrder){
    const info=th[tz];
    if(!info)continue;
    const c=tc(info.temp,55,70);
    h+='<div class="t-item"><span class="tn">'+info.label+'</span><span class="tv '+c+'">'+info.temp+'°C</span></div>';
  }
  h+='</div></div>';

  // ═══ VOLTAGES ═══
  h+='<div class="sec"><h2>Voltages / Power</h2>';
  h+='<div class="volt-grid">';
  const importantRegs=['vph_pwr','vreg_bob_3p296','vcc_5v_peri','vcc_3v3','vcc_1v8',
    'vreg_s7b_0p536','vreg_l1c_1p8','vreg_s1b_1p84','vreg_l2b_3p072',
    'vreg_l6c_2p96','vreg_l9c_2p96','vreg_l10c_0p88',
    'vreg_l1b_0p912','vreg_l6b_1p2','vreg_l9b_1p2','vreg_l7b_2v96',
    'vreg_l17b_1p8','vreg_l18b_1p8','vreg_l19b_1p8'];
  const regLabels={'vph_pwr':'VPH PWR (Main)','vreg_bob_3p296':'BOB','vcc_5v_peri':'5V Peripherals','vcc_3v3':'3.3V Rail','vcc_1v8':'1.8V Rail','vreg_s7b_0p536':'VDD (Core)','vreg_l1c_1p8':'L1C 1.8V','vreg_s1b_1p84':'S1B','vreg_l2b_3p072':'L2B 3.07V','vreg_l6c_2v96':'L6C','vreg_l9c_2v96':'L9C','vreg_l10c_0p88':'L10C 0.88V'};
  for(const reg of d.regulators||[]){
    const label=regLabels[reg.name]||reg.name;
    if(reg.voltage_v!=null)h+='<div class="v-item"><span class="vn">'+label+'</span><span class="vv">'+reg.voltage_v.toFixed(2)+'V</span></div>';
  }
  h+='</div></div>';

  // ═══ DOCKER ═══
  h+='<div class="sec full"><h2>Docker Containers <span class="badge">'+d.container_up+'/'+d.container_total+'</span></h2>';
  h+='<div class="sec-scroll"><table class="ctbl"><thead><tr><th>#</th><th>Container</th><th>Status</th></tr></thead><tbody>';
  d.containers.forEach((c,i)=>{
    const up=c.state==='running';
    h+='<tr><td style="color:var(--dim)">'+(i+1)+'</td><td><span class="dot '+(up?'up':'down')+'"></span>'+c.name+'</td><td class="'+(up?'ok':'cr')+'">'+c.status+'</td></tr>';
  });
  h+='</tbody></table></div></div>';

  if(!$('grid')._tpl) $('grid')._tpl = document.createElement('div');
  $('grid')._tpl.innerHTML = h;
  function morph(oldN, newN) {
    if(oldN.nodeType !== newN.nodeType) { oldN.replaceWith(newN.cloneNode(true)); return; }
    if(oldN.nodeType === Node.TEXT_NODE) { if(oldN.nodeValue !== newN.nodeValue) oldN.nodeValue = newN.nodeValue; return; }
    if(oldN.tagName === 'CANVAS') return;
    const oA = oldN.attributes, nA = newN.attributes;
    for(let i=oA.length-1; i>=0; i--) if(!newN.hasAttribute(oA[i].name)) oldN.removeAttribute(oA[i].name);
    for(let i=0; i<nA.length; i++) if(oldN.getAttribute(nA[i].name) !== nA[i].value) oldN.setAttribute(nA[i].name, nA[i].value);
    const oC = Array.from(oldN.childNodes), nC = Array.from(newN.childNodes);
    for(let i=0; i<Math.max(oC.length, nC.length); i++) {
      if(!oC[i]) oldN.appendChild(nC[i].cloneNode(true));
      else if(!nC[i]) oldN.removeChild(oC[i]);
      else morph(oC[i], nC[i]);
    }
  }
  if(!$('grid').children.length) $('grid').innerHTML = h;
  else {
    const oC = Array.from($('grid').childNodes), nC = Array.from($('grid')._tpl.childNodes);
    for(let i=0; i<Math.max(oC.length, nC.length); i++) {
      if(!oC[i]) $('grid').appendChild(nC[i].cloneNode(true));
      else if(!nC[i]) $('grid').removeChild(oC[i]);
      else morph(oC[i], nC[i]);
    }
  }

  // Charts
  setTimeout(()=>{
    const hi=d.hist;
    spark('ch-cpu',hi.cpu,'rgb(88,166,255)',100);
    spark('ch-ram',hi.ram,'rgb(63,185,80)',100);
    spark('ch-gpu',hi.gpu,'rgb(188,140,255)');
    const maxD=Math.max(...hi.disk_r.concat(hi.disk_w).filter(v=>v!=null&&v>0),1);
    dualSpark('ch-disk',hi.disk_r,hi.disk_w,'rgb(57,197,207)','rgb(240,136,62)',maxD);
    const maxN=Math.max(...hi.net_rx.concat(hi.net_tx).filter(v=>v!=null&&v>0),1);
    dualSpark('ch-net',hi.net_rx,hi.net_tx,'rgb(63,185,80)','rgb(88,166,255)',maxN);
    const maxT=Math.max(...hi.temp.filter(v=>v!=null),60);
    spark('ch-temp',hi.temp,'rgb(210,153,34)',maxT);
    spark('ch-npu-util',hi.npu_util,'rgb(188,140,255)',100);
  },60);
}

function connectStream() {
  const evtSource = new EventSource('/stream');
  evtSource.onmessage = function(e) {
    try {
      const d = JSON.parse(e.data);
      render(d);
    } catch(err) {
      $('meta-text').textContent = 'Error: ' + err.message;
    }
  };
  evtSource.onerror = function(e) {
    $('meta-text').textContent = 'Connection lost. Reconnecting...';
    evtSource.close();
    setTimeout(connectStream, 3000);
  };
}
connectStream();
</script>
</body>
</html>
"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(LATEST_STATS)))
            self.end_headers()
            self.wfile.write(LATEST_STATS.encode())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                # Send immediately upon connect
                self.wfile.write(f"data: {LATEST_STATS}\n\n".encode())
                self.wfile.flush()
                # Wait for updates
                while True:
                    time.sleep(2)
                    self.wfile.write(f"data: {LATEST_STATS}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            body = DASHBOARD.encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=stats_loop, daemon=True).start()
    print(f"System Monitor → http://0.0.0.0:{PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    server.serve_forever()
