#!/usr/bin/env python3
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODEL = "Arista DCS-7050QX-32S-F"
READ_ONLY = re.compile(r"^(show|ping|traceroute|traceroute6|dir|more)\b", re.I)
BLOCKED = re.compile(r"^(configure|conf|enable|reload|reboot|write|copy|delete|erase|bash|sudo|install)\b", re.I)


def now_ms():
    return int(time.time() * 1000)


def is_safe_command(command):
    command = command.strip()
    return bool(command) and not BLOCKED.search(command) and bool(READ_ONLY.search(command))


def run_cli(command, timeout=22):
    if not is_safe_command(command):
        raise ValueError("Only read-only commands are allowed: show / ping / traceroute / dir / more.")

    candidates = [
        ["FastCli", "-p", "15", "-c", command],
        ["/usr/bin/FastCli", "-p", "15", "-c", command],
        ["Cli", "-c", command],
        ["/usr/bin/Cli", "-c", command],
    ]

    last_error = None
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            output = (result.stdout or "").strip()
            error = (result.stderr or "").strip()
            if result.returncode == 0 and output:
                return output
            last_error = error or output or "command returned %s" % result.returncode
        except FileNotFoundError as exc:
            last_error = str(exc)
        except subprocess.TimeoutExpired:
            raise TimeoutError("Command timed out.")

    raise RuntimeError(last_error or "No EOS CLI runner found.")


def normalize_interface(token):
    match = re.match(r"^(?:Et|Ethernet)([\d/]+)$", str(token), re.I)
    if match:
        return "Ethernet%s" % match.group(1)
    return str(token)


def parse_hostname(output):
    match = re.search(r"^Hostname:\s*(.+)$", output, re.I | re.M)
    if match:
        return match.group(1).strip()
    fqdn = re.search(r"^FQDN:\s*(.+)$", output, re.I | re.M)
    if fqdn:
        return fqdn.group(1).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "arista-7050qx"


def parse_version(output):
    version = "-"
    serial = "-"
    version_match = re.search(r"Software image version:\s*([^\r\n]+)", output, re.I)
    if not version_match:
        version_match = re.search(r"EOS version:\s*([^\r\n]+)", output, re.I)
    serial_match = re.search(r"Serial number:\s*(\S+)", output, re.I)
    if version_match:
        version = version_match.group(1).strip()
    if serial_match:
        serial = serial_match.group(1).strip()
    return version, serial


def parse_interfaces(output):
    ports = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or not re.match(r"^(Et|Ethernet)\d+(?:/\d+)?\b", stripped, re.I):
            continue

        tokens = stripped.split()
        port = {
            "name": normalize_interface(tokens[0]),
            "label": tokens[0],
            "media": "-",
            "speed": "-",
            "duplex": "-",
            "status": "down",
            "vlan": "-",
            "description": "",
            "rxMbps": 0.0,
            "txMbps": 0.0,
            "rxKpps": 0.0,
            "txKpps": 0.0,
            "errors": 0,
            "statusLine": stripped,
        }

        status_index = -1
        for idx, token in enumerate(tokens):
            if token.lower() in ("connected", "notconnect", "disabled", "errdisabled", "inactive"):
                status_index = idx
                break

        if status_index >= 0:
            port["status"] = "up" if tokens[status_index].lower() == "connected" else "down"
            if status_index + 1 < len(tokens):
                port["vlan"] = tokens[status_index + 1]
            if status_index + 2 < len(tokens):
                port["duplex"] = tokens[status_index + 2]
            if status_index + 3 < len(tokens):
                port["speed"] = tokens[status_index + 3].upper()
            if status_index + 4 < len(tokens):
                port["media"] = " ".join(tokens[status_index + 4:])
        if status_index > 1:
            port["description"] = " ".join(tokens[1:status_index])
        ports.append(port)

    return ports


def parse_interface_rates(output):
    rates = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or not re.match(r"^(Et|Ethernet|Ma)\S+", stripped, re.I):
            continue
        tokens = stripped.split()
        interval_index = -1
        for idx, token in enumerate(tokens):
            if re.match(r"^\d+:\d+$", token):
                interval_index = idx
                break
        if interval_index < 0 or len(tokens) <= interval_index + 6:
            continue

        def to_float(value):
            try:
                return float(str(value).replace("%", ""))
            except ValueError:
                return 0.0

        rates[normalize_interface(tokens[0]).lower()] = {
            "rxMbps": to_float(tokens[interval_index + 1]),
            "rxPercent": to_float(tokens[interval_index + 2]),
            "rxKpps": to_float(tokens[interval_index + 3]),
            "txMbps": to_float(tokens[interval_index + 4]),
            "txPercent": to_float(tokens[interval_index + 5]),
            "txKpps": to_float(tokens[interval_index + 6]),
            "rateLine": stripped,
        }
    return rates


def parse_interface_errors(output):
    errors = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or not re.match(r"^(Et|Ethernet|Ma)\S+", stripped, re.I):
            continue
        tokens = stripped.split()
        total = 0
        for token in tokens[1:]:
            if re.match(r"^\d+$", token):
                total += int(token)
        errors[normalize_interface(tokens[0]).lower()] = {"errors": total, "errorLine": stripped}
    return errors


def enrich_ports(ports, rates, errors):
    enriched = []
    for port in ports:
        key = port["name"].lower()
        item = dict(port)
        item.update(rates.get(key, {}))
        item.update(errors.get(key, {}))
        item["errors"] = int(item.get("errors") or 0)
        enriched.append(item)
    return enriched


def parse_environment(output):
    text = output.lower()
    has_fault = any(word in text for word in ("fail", "fault", "bad", "overheat"))
    temp_match = re.search(r"(\d+)\s*(?:c|degrees)", output, re.I)
    temperature = int(temp_match.group(1)) if temp_match else (58 if has_fault else 40)
    return {
        "temperature": temperature,
        "fanStatus": "CHECK" if has_fault else "OK",
        "psuStatus": "CHECK" if has_fault else "OK",
    }


def parse_system_health(top_output, environment_output, version_output):
    health = parse_environment(environment_output)
    cpu = 0
    idle_match = re.search(r"([\d.]+)\s*id", top_output)
    if idle_match:
        cpu = max(0, min(100, round(100 - float(idle_match.group(1)))))

    mem_total = mem_used = mem_free = mem_avail = 0.0
    mem_match = re.search(
        r"MiB Mem\s*:\s*([\d.]+)\s+total,\s*([\d.]+)\s+free,\s*([\d.]+)\s+used,\s*([\d.]+)\s+buff/cache",
        top_output,
        re.I,
    )
    avail_match = re.search(r"([\d.]+)\s+avail Mem", top_output, re.I)
    if mem_match:
        mem_total = float(mem_match.group(1))
        mem_free = float(mem_match.group(2))
        mem_used = float(mem_match.group(3))
        mem_avail = float(avail_match.group(1)) if avail_match else mem_free
    else:
        total_match = re.search(r"Total memory:\s*(\d+)\s*kB", version_output, re.I)
        free_match = re.search(r"Free memory:\s*(\d+)\s*kB", version_output, re.I)
        if total_match:
            mem_total = int(total_match.group(1)) / 1024.0
        if free_match:
            mem_free = int(free_match.group(1)) / 1024.0
        mem_avail = mem_free
        mem_used = max(0.0, mem_total - mem_free)

    memory = round(((mem_total - mem_avail) / mem_total) * 100) if mem_total else 0
    health.update(
        {
            "cpu": cpu,
            "memory": memory,
            "memoryTotalMiB": round(mem_total, 1),
            "memoryUsedMiB": round(mem_used, 1),
            "memoryAvailableMiB": round(mem_avail, 1),
        }
    )
    return health


def format_rate(mbps):
    if mbps >= 1000000:
        return "%.2f Tbps" % (mbps / 1000000.0)
    if mbps >= 1000:
        return "%.2f Gbps" % (mbps / 1000.0)
    return "%.2f Mbps" % mbps


def format_packets(kpps):
    if kpps >= 1000000:
        return "%.2f Bpps" % (kpps / 1000000.0)
    if kpps >= 1000:
        return "%.2f Mpps" % (kpps / 1000.0)
    return "%.2f Kpps" % kpps


def traffic_summary(ports):
    rx_mbps = sum(float(port.get("rxMbps") or 0) for port in ports)
    tx_mbps = sum(float(port.get("txMbps") or 0) for port in ports)
    rx_kpps = sum(float(port.get("rxKpps") or 0) for port in ports)
    tx_kpps = sum(float(port.get("txKpps") or 0) for port in ports)
    total_mbps = rx_mbps + tx_mbps
    total_kpps = rx_kpps + tx_kpps
    return {
        "rxMbps": round(rx_mbps, 2),
        "txMbps": round(tx_mbps, 2),
        "totalMbps": round(total_mbps, 2),
        "rxKpps": round(rx_kpps, 2),
        "txKpps": round(tx_kpps, 2),
        "totalKpps": round(total_kpps, 2),
        "throughputLabel": format_rate(total_mbps),
        "packetRateLabel": format_packets(total_kpps),
        "capacityUtilization": round((total_mbps / 2560000.0) * 100, 4),
    }


def collect_state():
    errors = []

    def get(command):
        try:
            return run_cli(command)
        except Exception as exc:
            errors.append("%s: %s" % (command, exc))
            return ""

    version_output = get("show version")
    hostname_output = get("show hostname")
    uptime_output = get("show uptime")
    interface_output = get("show interfaces status")
    rates_output = get("show interfaces counters rates")
    errors_output = get("show interfaces counters errors")
    top_output = get("show processes top once")
    env_output = get("show environment all")

    eos_version, serial = parse_version(version_output)
    ports = enrich_ports(parse_interfaces(interface_output), parse_interface_rates(rates_output), parse_interface_errors(errors_output))
    traffic = traffic_summary(ports)
    return {
        "device": {
            "model": MODEL,
            "hostname": parse_hostname(hostname_output),
            "serial": serial,
            "eosVersion": eos_version,
            "uptime": uptime_output.strip() or "-",
            "switchingCapacity": traffic["throughputLabel"],
            "forwardingRate": traffic["packetRateLabel"],
            "airflow": "Front-to-back",
            "lastRefresh": now_ms(),
            "source": "on-box",
        },
        "health": parse_system_health(top_output, env_output, version_output),
        "traffic": traffic,
        "ports": ports,
        "events": [
            {
                "time": now_ms(),
                "level": "error" if errors else "success",
                "message": "; ".join(errors) if errors else "Refreshed from local EOS CLI.",
            }
        ],
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Arista 7050QX 后台</title>
  <style>
    :root{--bg:#f6f7f9;--surface:#fff;--ink:#18202a;--muted:#697586;--line:#dde3ea;--accent:#0f766e;--green:#168a45;--red:#c2410c;--amber:#b7791f;--blue:#2563eb}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}button,input{font:inherit}
    .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px clamp(16px,4vw,44px);border-bottom:1px solid var(--line);background:#fff}.eyebrow{margin:0 0 4px;color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em}h1,h2,p{margin:0}h1{font-size:clamp(22px,3vw,34px);line-height:1.1}h2{font-size:16px}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.badge{display:inline-flex;align-items:center;min-height:34px;padding:0 12px;border:1px solid #b7ead2;border-radius:999px;background:#ecfdf5;color:var(--green);font-size:13px;font-weight:800;white-space:nowrap}.primary{min-height:38px;border:1px solid var(--accent);border-radius:8px;padding:0 14px;background:var(--accent);color:#fff;font-weight:750;cursor:pointer}.ghost{min-height:34px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);cursor:pointer}
    .layout{width:min(1480px,100%);margin:0 auto;padding:22px clamp(14px,3vw,34px) 34px}.overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.metric,.panel{border:1px solid var(--line);border-radius:8px;background:var(--surface);box-shadow:0 14px 35px rgba(24,32,42,.08)}.metric{min-height:92px;padding:15px}.metric span,.muted{color:var(--muted);font-size:12px}.metric strong{display:block;margin-top:8px;overflow-wrap:anywhere;font-size:20px;line-height:1.15}.metric small{display:block;margin-top:6px;color:var(--muted);font-size:11px}.main-grid{display:grid;grid-template-columns:minmax(0,1fr) 370px;gap:16px;align-items:start}.bottom-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:16px;margin-top:16px}.panel{padding:16px}.panel-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:14px}
    .legend{display:flex;gap:12px;flex-wrap:wrap;justify-content:flex-end;color:var(--muted);font-size:12px}.legend span{display:inline-flex;align-items:center;gap:6px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%}.up{background:var(--green)}.down{background:var(--muted)}.warn{background:var(--amber)}
    .port-grid{display:grid;grid-template-columns:repeat(8,minmax(82px,1fr));gap:8px}.port{position:relative;min-height:104px;padding:10px;border:1px solid var(--line);border-radius:8px;background:#fbfcfd;overflow:hidden;text-align:left;cursor:pointer}.port:hover{border-color:#9fb0c2;background:#fff}.port:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--muted)}.port[data-status=up]:before{background:var(--green)}.port[data-errors=true]:before{background:var(--amber)}.port-name{display:flex;align-items:center;justify-content:space-between;gap:6px;font-size:12px;font-weight:800}.port-speed{color:var(--blue);font-size:11px;font-weight:800}.port-detail{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.35;overflow-wrap:anywhere}.port-traffic{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:8px;font-size:11px}.port-traffic b{display:block;color:var(--ink);font-size:12px}
    .side-stack{display:grid;gap:16px}.gauge-list{display:grid;gap:12px}.gauge-row{display:grid;grid-template-columns:56px minmax(120px,1fr) 60px;align-items:center;gap:10px;color:var(--muted);font-size:13px}meter{width:100%;height:12px}.mini-status{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:15px}.mini-status span{min-height:44px;padding:10px;border:1px solid var(--line);border-radius:8px;color:var(--muted);background:#fbfcfd}.mini-status b{display:block;margin-top:2px;color:var(--ink)}.command-form{display:grid;grid-template-columns:minmax(0,1fr) 92px;gap:10px}input{width:100%;min-height:38px;border:1px solid var(--line);border-radius:8px;padding:0 10px;color:var(--ink);background:#fff}pre{min-height:210px;max-height:360px;margin:12px 0 0;overflow:auto;border:1px solid #1e293b;border-radius:8px;padding:14px;color:#d7e2ef;background:#111827;font:13px/1.55 "Cascadia Mono",Consolas,monospace;white-space:pre-wrap}
    .event-list{display:grid;gap:8px;max-height:320px;margin:0;padding:0;overflow:auto;list-style:none}.event-list li{display:grid;gap:3px;min-height:54px;border-left:4px solid var(--line);border-radius:8px;padding:9px 10px;background:#fbfcfd}.event-list li[data-level=success]{border-left-color:var(--green)}.event-list li[data-level=error]{border-left-color:var(--red)}.event-time{color:var(--muted);font-size:11px}.event-message{font-size:13px}.toast{position:fixed;right:18px;bottom:18px;max-width:min(420px,calc(100vw - 36px));padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:#fff;box-shadow:0 14px 35px rgba(24,32,42,.08);transform:translateY(90px);opacity:0;transition:.18s ease}.toast.show{transform:translateY(0);opacity:1}
    .modal{position:fixed;inset:0;display:none;align-items:center;justify-content:center;padding:18px;background:rgba(15,23,42,.42);z-index:30}.modal.show{display:flex}.dialog{width:min(760px,100%);max-height:88vh;overflow:auto;border-radius:8px;border:1px solid var(--line);background:#fff;box-shadow:0 24px 80px rgba(0,0,0,.24)}.dialog-head{display:flex;align-items:center;justify-content:space-between;padding:16px;border-bottom:1px solid var(--line)}.dialog-body{padding:16px}.detail-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.detail-item{border:1px solid var(--line);border-radius:8px;padding:10px;background:#fbfcfd}.detail-item span{display:block;color:var(--muted);font-size:11px}.detail-item b{display:block;margin-top:5px;font-size:15px;overflow-wrap:anywhere}
    @media(max-width:1080px){.overview{grid-template-columns:repeat(2,minmax(0,1fr))}.main-grid,.bottom-grid{grid-template-columns:1fr}.port-grid{grid-template-columns:repeat(6,minmax(72px,1fr))}}@media(max-width:720px){.topbar{align-items:flex-start;flex-direction:column}.actions{width:100%;justify-content:space-between}.overview,.mini-status,.detail-grid{grid-template-columns:1fr}.port-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.command-form{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <header class="topbar"><div><p class="eyebrow">ON-BOX CONSOLE</p><h1>Arista DCS-7050QX-32S-F</h1></div><div class="actions"><span class="badge">EOS 本机运行</span><button id="refreshBtn" class="primary">刷新</button></div></header>
  <main class="layout">
    <section class="overview"><article class="metric"><span>主机名</span><strong id="hostname">-</strong></article><article class="metric"><span>EOS</span><strong id="eosVersion">-</strong></article><article class="metric"><span>实时转发</span><strong id="forwardingLive">-</strong><small id="trafficSubline">-</small></article><article class="metric"><span>实时交换</span><strong id="switchingLive">-</strong><small id="capacitySubline">-</small></article></section>
    <section class="main-grid"><div class="panel"><div class="panel-title"><div><h2>端口视图</h2><p id="portSummary" class="muted">-</p></div><div class="legend"><span><i class="dot up"></i>Up</span><span><i class="dot down"></i>Down</span><span><i class="dot warn"></i>Error</span></div></div><div id="portGrid" class="port-grid"></div></div>
      <aside class="side-stack"><div class="panel"><div class="panel-title"><h2>设备健康</h2><span id="lastRefresh" class="muted">-</span></div><div class="gauge-list"><div class="gauge-row"><span>CPU</span><meter id="cpuMeter" min="0" max="100" value="0"></meter><strong id="cpuValue">-</strong></div><div class="gauge-row"><span>内存</span><meter id="memoryMeter" min="0" max="100" value="0"></meter><strong id="memoryValue">-</strong></div><div class="gauge-row"><span>温度</span><meter id="temperatureMeter" min="0" max="90" value="0"></meter><strong id="temperatureValue">-</strong></div></div><div class="mini-status"><span>风扇 <b id="fanStatus">-</b></span><span>电源 <b id="psuStatus">-</b></span></div></div>
      <div class="panel"><div class="panel-title"><h2>运行位置</h2><span class="muted">0.0.0.0:2480</span></div><p class="muted">此页面直接运行在交换机 EOS bash 环境中，通过本机 CLI 读取实时状态。</p></div></aside></section>
    <section class="bottom-grid"><div class="panel"><div class="panel-title"><h2>命令控制台</h2><span class="muted">只读命令</span></div><form id="commandForm" class="command-form"><input id="commandInput" value="show interfaces status" /><button class="primary" type="submit">运行</button></form><pre id="commandOutput">等待命令...</pre></div><div class="panel"><div class="panel-title"><h2>事件</h2><span class="muted">最近状态</span></div><ul id="eventList" class="event-list"></ul></div></section>
  </main>
  <div id="portModal" class="modal"><div class="dialog"><div class="dialog-head"><h2 id="modalTitle">端口详情</h2><button id="modalClose" class="ghost">关闭</button></div><div id="modalBody" class="dialog-body"></div></div></div>
  <div id="toast" class="toast"></div>
  <script>
    const $=s=>document.querySelector(s),el={refreshBtn:$("#refreshBtn"),hostname:$("#hostname"),eosVersion:$("#eosVersion"),forwardingLive:$("#forwardingLive"),switchingLive:$("#switchingLive"),trafficSubline:$("#trafficSubline"),capacitySubline:$("#capacitySubline"),portSummary:$("#portSummary"),lastRefresh:$("#lastRefresh"),cpuMeter:$("#cpuMeter"),cpuValue:$("#cpuValue"),memoryMeter:$("#memoryMeter"),memoryValue:$("#memoryValue"),temperatureMeter:$("#temperatureMeter"),temperatureValue:$("#temperatureValue"),fanStatus:$("#fanStatus"),psuStatus:$("#psuStatus"),portGrid:$("#portGrid"),commandForm:$("#commandForm"),commandInput:$("#commandInput"),commandOutput:$("#commandOutput"),eventList:$("#eventList"),toast:$("#toast"),portModal:$("#portModal"),modalTitle:$("#modalTitle"),modalBody:$("#modalBody"),modalClose:$("#modalClose")};
    let toastTimer=null,currentPorts=[];function esc(v){return String(v??"-").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]))}function toast(m){el.toast.textContent=m;el.toast.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.toast.classList.remove("show"),2600)}function fmt(v){return v?new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(new Date(v)):"-"}async function req(u,o={}){const r=await fetch(u,{headers:{"Content-Type":"application/json"},...o});const p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||`HTTP ${r.status}`);return p}function pct(v){return Number.isFinite(Number(v))?`${Number(v).toFixed(0)}%`:"-"}function mbps(v){return `${Number(v||0).toFixed(2)}M`}
    function detail(label,value){return `<div class="detail-item"><span>${label}</span><b>${esc(value)}</b></div>`}function showPort(i){const p=currentPorts[i];if(!p)return;el.modalTitle.textContent=`${p.label||p.name} 端口详情`;el.modalBody.innerHTML=`<div class="detail-grid">${detail("状态",p.status)}${detail("VLAN",p.vlan)}${detail("双工",p.duplex)}${detail("协商/网速",p.speed)}${detail("介质",p.media)}${detail("RX Mbps",Number(p.rxMbps||0).toFixed(2))}${detail("TX Mbps",Number(p.txMbps||0).toFixed(2))}${detail("RX Kpps",Number(p.rxKpps||0).toFixed(2))}${detail("TX Kpps",Number(p.txKpps||0).toFixed(2))}${detail("错误计数",p.errors||0)}${detail("描述",p.description||"-")}</div><pre>${esc([p.statusLine,p.rateLine,p.errorLine].filter(Boolean).join("\n"))}</pre>`;el.portModal.classList.add("show")}
    function render(state){const d=state.device||{},h=state.health||{},t=state.traffic||{},ports=state.ports||[],up=ports.filter(p=>p.status==="up").length,err=ports.filter(p=>Number(p.errors||0)>0).length;currentPorts=ports;el.hostname.textContent=d.hostname||"-";el.eosVersion.textContent=d.eosVersion||"-";el.forwardingLive.textContent=d.forwardingRate||t.packetRateLabel||"-";el.switchingLive.textContent=d.switchingCapacity||t.throughputLabel||"-";el.trafficSubline.textContent=`RX ${mbps(t.rxMbps)} / TX ${mbps(t.txMbps)}`;el.capacitySubline.textContent=`占用 ${Number(t.capacityUtilization||0).toFixed(4)}% of 2.56Tbps`;el.portSummary.textContent=`${up}/${ports.length} up, ${err} error`;el.lastRefresh.textContent=fmt(d.lastRefresh);el.cpuMeter.value=Number(h.cpu||0);el.cpuValue.textContent=pct(h.cpu);el.memoryMeter.value=Number(h.memory||0);el.memoryValue.textContent=pct(h.memory);el.temperatureMeter.value=Number(h.temperature||0);el.temperatureValue.textContent=Number.isFinite(Number(h.temperature))?`${h.temperature}C`:"-";el.fanStatus.textContent=h.fanStatus||"-";el.psuStatus.textContent=h.psuStatus||"-";el.portGrid.innerHTML=ports.map((p,i)=>`<button class="port" data-index="${i}" data-status="${p.status}" data-errors="${Number(p.errors||0)>0}"><div class="port-name"><span>${esc(p.label||p.name)}</span><span class="port-speed">${esc(p.speed)}</span></div><div class="port-detail">${esc(p.media)} / VLAN ${esc(p.vlan)}<br>${esc(p.description||p.status)}</div><div class="port-traffic"><span>RX <b>${mbps(p.rxMbps)}</b></span><span>TX <b>${mbps(p.txMbps)}</b></span></div></button>`).join("");el.eventList.innerHTML=(state.events||[]).map(e=>`<li data-level="${e.level||"info"}"><span class="event-time">${fmt(e.time)} / ${esc(e.level||"info")}</span><span class="event-message">${esc(e.message)}</span></li>`).join("")}
    async function load(){const p=await req("/api/state");render(p.state)}async function refresh(){el.refreshBtn.disabled=true;el.refreshBtn.textContent="刷新中";try{const p=await req("/api/refresh",{method:"POST",body:"{}"});render(p.state);toast("已从 EOS CLI 刷新")}catch(e){toast(e.message)}finally{el.refreshBtn.disabled=false;el.refreshBtn.textContent="刷新"}}
    el.refreshBtn.addEventListener("click",refresh);el.portGrid.addEventListener("click",e=>{const card=e.target.closest(".port");if(card)showPort(Number(card.dataset.index))});el.modalClose.addEventListener("click",()=>el.portModal.classList.remove("show"));el.portModal.addEventListener("click",e=>{if(e.target===el.portModal)el.portModal.classList.remove("show")});el.commandForm.addEventListener("submit",async e=>{e.preventDefault();const command=el.commandInput.value.trim();if(!command)return;el.commandOutput.textContent=`> ${command}\n运行中...`;try{const p=await req("/api/command",{method:"POST",body:JSON.stringify({command})});el.commandOutput.textContent=`> ${p.command}\n${p.output}`}catch(err){el.commandOutput.textContent=`> ${command}\nERROR: ${err.message}`}});load().catch(e=>toast(e.message));setInterval(()=>load().catch(()=>{}),15000);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    cached_state = None
    cached_at = 0

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0") or "0")
        if size > 1024 * 1024:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(size).decode("utf-8") if size else "{}"
        return json.loads(raw or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/state":
            if not Handler.cached_state or time.time() - Handler.cached_at > 10:
                Handler.cached_state = collect_state()
                Handler.cached_at = time.time()
            self.send_json(200, {"ok": True, "state": Handler.cached_state})
            return

        self.send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/refresh":
                Handler.cached_state = collect_state()
                Handler.cached_at = time.time()
                self.send_json(200, {"ok": True, "state": Handler.cached_state})
                return

            if path == "/api/command":
                payload = self.read_json()
                command = str(payload.get("command", "")).strip()
                output = run_cli(command)
                self.send_json(200, {"ok": True, "command": command, "output": output})
                return

            self.send_json(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="On-box web console for Arista DCS-7050QX-32S-F.")
    parser.add_argument("--host", default=os.environ.get("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "2480")))
    parser.add_argument("--daemon", action="store_true", help="Run in the background on EOS.")
    parser.add_argument("--log", default="/mnt/flash/arista7050_web.log")
    args = parser.parse_args()

    if args.daemon:
        if os.fork() > 0:
            print("Arista 7050QX web console started in background.")
            return
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if os.fork() > 0:
            os._exit(0)
        os.chdir("/")
        sys.stdin.close()
        log = open(args.log, "a", buffering=1)
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Arista 7050QX on-box web console listening on http://%s:%s" % (args.host, args.port))
    print("Open http://<switch-management-ip>:%s/" % args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
