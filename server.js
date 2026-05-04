const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { Client: SshClient } = require("ssh2");

const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, "public");
const DATA_DIR = path.join(ROOT, "data");
const STATE_FILE = path.join(DATA_DIR, "state.json");
const CONFIG_FILE = path.join(DATA_DIR, "config.json");
const PORT = Number(process.env.PORT || 2480);

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml; charset=utf-8"
};

const BLOCKED_COMMANDS = [
  "configure",
  "conf",
  "enable",
  "reload",
  "reboot",
  "write",
  "copy",
  "delete",
  "erase",
  "bash",
  "sudo",
  "install"
];

function nowIso() {
  return new Date().toISOString();
}

function defaultPorts() {
  const qsfp = Array.from({ length: 32 }, (_, index) => ({
    id: index + 1,
    name: `Ethernet${index + 1}`,
    role: index < 24 ? "leaf/server" : "spine/uplink",
    media: "QSFP+",
    speed: "40G",
    status: index % 7 === 0 ? "down" : "up",
    vlan: index < 24 ? "trunk" : "routed",
    description: index < 24 ? `Server rack ${String(index + 1).padStart(2, "0")}` : `Fabric link ${index - 23}`,
    rxMbps: index % 7 === 0 ? 0 : 1180 + index * 43,
    txMbps: index % 7 === 0 ? 0 : 960 + index * 39,
    errors: index % 13 === 0 ? 2 : 0
  }));

  const sfp = Array.from({ length: 4 }, (_, index) => ({
    id: index + 33,
    name: `Ethernet${index + 33}`,
    role: "management/uplink",
    media: "SFP+",
    speed: "10G",
    status: index === 3 ? "down" : "up",
    vlan: index === 0 ? "mgmt" : "trunk",
    description: `10G uplink ${index + 1}`,
    rxMbps: index === 3 ? 0 : 280 + index * 61,
    txMbps: index === 3 ? 0 : 240 + index * 55,
    errors: 0
  }));

  return [...qsfp, ...sfp];
}

function defaultState() {
  return {
    device: {
      model: "Arista DCS-7050QX-32S-F",
      hostname: "arista-7050qx",
      serial: "LOCAL-SIM",
      eosVersion: "模拟模式",
      uptime: "未连接真实设备",
      switchingCapacity: "2.56 Tbps",
      forwardingRate: "1.44 Bpps",
      airflow: "Front-to-back",
      lastRefresh: nowIso(),
      source: "local"
    },
    health: {
      cpu: 18,
      memory: 42,
      temperature: 38,
      fanStatus: "OK",
      psuStatus: "1+1 OK"
    },
    ports: defaultPorts(),
    events: [
      {
        time: nowIso(),
        level: "info",
        message: "本地后台已初始化，尚未连接 Arista eAPI。"
      }
    ]
  };
}

function defaultConfig() {
  return {
    enabled: false,
    mode: "ssh",
    protocol: "http",
    host: "",
    port: 22,
    username: "",
    password: "",
    commandFormat: "json",
    updatedAt: nowIso()
  };
}

async function ensureDataFiles() {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await ensureFile(STATE_FILE, defaultState());
  await ensureFile(CONFIG_FILE, defaultConfig());
}

async function ensureFile(file, value) {
  try {
    await fs.access(file);
  } catch {
    await fs.writeFile(file, JSON.stringify(value, null, 2), "utf8");
  }
}

async function readJson(file, fallback) {
  try {
    const content = await fs.readFile(file, "utf8");
    return JSON.parse(content);
  } catch {
    return fallback;
  }
}

async function writeJson(file, value) {
  await fs.writeFile(file, JSON.stringify(value, null, 2), "utf8");
}

function sanitizeConfig(config) {
  return {
    enabled: Boolean(config.enabled),
    mode: config.mode || "ssh",
    protocol: config.protocol || "http",
    host: config.host || "",
    port: Number(config.port || ((config.mode || "ssh") === "ssh" ? 22 : 80)),
    username: config.username || "",
    hasPassword: Boolean(config.password),
    updatedAt: config.updatedAt || null
  };
}

function json(res, status, payload) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store"
  });
  res.end(JSON.stringify(payload));
}

function text(res, status, payload) {
  res.writeHead(status, { "Content-Type": "text/plain; charset=utf-8" });
  res.end(payload);
}

async function readBody(req) {
  let body = "";
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 1024 * 1024) {
      throw new Error("请求体过大");
    }
  }
  return body ? JSON.parse(body) : {};
}

function isReadOnlyCommand(command) {
  const normalized = command.trim().toLowerCase();
  if (!normalized) return false;
  if (BLOCKED_COMMANDS.some((blocked) => normalized === blocked || normalized.startsWith(`${blocked} `))) {
    return false;
  }
  return /^(show|ping|traceroute|traceroute6|dir|more)\b/.test(normalized);
}

function eapiUrl(config) {
  const protocol = config.protocol === "https" ? "https" : "http";
  const host = String(config.host || "").trim();
  const port = Number(config.port || (protocol === "https" ? 443 : 80));
  return `${protocol}://${host}:${port}/command-api`;
}

async function runEapiCommands(config, commands, format = "json") {
  if (!config.enabled || !config.host || !config.username || !config.password) {
    throw new Error("尚未配置 eAPI 连接。");
  }

  const auth = Buffer.from(`${config.username}:${config.password}`).toString("base64");
  const response = await fetch(eapiUrl(config), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Basic ${auth}`
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "runCmds",
      params: {
        version: 1,
        cmds: commands,
        format
      },
      id: `console-${Date.now()}`
    })
  });

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`eAPI HTTP ${response.status}: ${JSON.stringify(payload)}`);
  }
  if (payload && payload.error) {
    throw new Error(payload.error.message || JSON.stringify(payload.error));
  }
  return payload ? payload.result : [];
}

function runSshCommand(config, command) {
  return new Promise((resolve, reject) => {
    if (!config.enabled || !config.host || !config.username || !config.password) {
      reject(new Error("SSH connection is not configured."));
      return;
    }

    const conn = new SshClient();
    const timeout = setTimeout(() => {
      conn.end();
      reject(new Error("SSH command timed out."));
    }, 20000);

    conn
      .on("ready", () => {
        conn.exec(command, (error, stream) => {
          if (error) {
            clearTimeout(timeout);
            conn.end();
            reject(error);
            return;
          }

          let stdout = "";
          let stderr = "";
          stream
            .on("close", (code) => {
              clearTimeout(timeout);
              conn.end();
              if (code && stderr.trim()) {
                reject(new Error(stderr.trim()));
                return;
              }
              resolve(stdout.trim() || stderr.trim());
            })
            .on("data", (data) => {
              stdout += data.toString("utf8");
            })
            .stderr.on("data", (data) => {
              stderr += data.toString("utf8");
            });
        });
      })
      .on("keyboard-interactive", (_name, _instructions, _lang, _prompts, finish) => {
        finish([config.password]);
      })
      .on("error", (error) => {
        clearTimeout(timeout);
        reject(error);
      })
      .connect({
        host: String(config.host || "").trim(),
        port: Number(config.port || 22),
        username: config.username,
        password: config.password,
        tryKeyboard: true,
        readyTimeout: 15000
      });
  });
}

async function runSshCommands(config, commands) {
  const results = [];
  for (const command of commands) {
    results.push(await runSshCommand(config, command));
  }
  return results;
}

function normalizeInterfaceName(name) {
  const match = String(name || "").match(/^(?:Et|Ethernet)(\d+)$/i);
  return match ? `Ethernet${match[1]}` : name;
}

function parseSshVersion(output) {
  const version = {};
  const serialMatch = output.match(/Serial number:\s*(\S+)/i);
  const versionMatch = output.match(/(?:Software image version|EOS version|Version):\s*([^\r\n]+)/i);
  if (serialMatch) version.serialNumber = serialMatch[1];
  if (versionMatch) version.version = versionMatch[1].trim();
  return version;
}

function parseSshInterfacesStatus(output, fallbackPorts) {
  const ports = (fallbackPorts || defaultPorts()).map((port) => ({ ...port }));
  const byName = new Map(ports.map((port) => [port.name.toLowerCase(), port]));

  for (const line of String(output || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    const firstToken = trimmed.split(/\s+/)[0];
    const normalized = normalizeInterfaceName(firstToken);
    const port = byName.get(String(normalized).toLowerCase());
    if (!port) continue;

    const tokens = trimmed.split(/\s+/);
    const statusIndex = tokens.findIndex((token) => /^(connected|notconnect|disabled|errdisabled|inactive)$/i.test(token));
    if (statusIndex >= 0) {
      port.status = /^connected$/i.test(tokens[statusIndex]) ? "up" : "down";
      port.vlan = tokens[statusIndex + 1] || port.vlan;
    }

    const speed = tokens.find((token) => /^(?:auto|10G|40G|1000M|100M|10M)$/i.test(token));
    if (speed && !/^auto$/i.test(speed)) port.speed = speed.toUpperCase();
    port.rxMbps = port.status === "up" ? port.rxMbps : 0;
    port.txMbps = port.status === "up" ? port.txMbps : 0;
  }

  return ports;
}

function parseInterfacesStatus(result) {
  const source = result && (result.interfaceStatuses || result.interfaces || result);
  if (!source || typeof source !== "object") return null;

  const ports = defaultPorts();
  const byName = new Map(ports.map((port) => [port.name.toLowerCase(), port]));

  for (const [name, details] of Object.entries(source)) {
    const normalized = name.toLowerCase().replace(/^et/, "ethernet");
    const port = byName.get(normalized);
    if (!port || !details || typeof details !== "object") continue;

    const link = String(details.linkStatus || details.lineProtocolStatus || details.interfaceStatus || "").toLowerCase();
    const speed = details.bandwidth ? `${Math.round(Number(details.bandwidth) / 1000000000)}G` : details.speed || port.speed;

    port.status = link.includes("up") || link === "connected" ? "up" : "down";
    port.description = details.description || port.description;
    port.speed = String(speed || port.speed).toUpperCase();
    port.vlan = details.vlanInformation?.interfaceMode || details.vlanId || port.vlan;
    port.rxMbps = Number(port.status === "up" ? port.rxMbps : 0);
    port.txMbps = Number(port.status === "up" ? port.txMbps : 0);
  }

  return ports;
}

function deriveHealth(environmentResult) {
  const textDump = JSON.stringify(environmentResult || {});
  const hasFault = /fail|fault|bad|overheat/i.test(textDump);
  return {
    cpu: 20,
    memory: 45,
    temperature: hasFault ? 58 : 40,
    fanStatus: hasFault ? "CHECK" : "OK",
    psuStatus: hasFault ? "CHECK" : "1+1 OK"
  };
}

async function refreshFromSsh(config, state) {
  const result = await runSshCommands(config, [
    "show version",
    "show hostname",
    "show uptime",
    "show interfaces status",
    "show environment all"
  ]);

  const version = parseSshVersion(result[0] || "");
  const hostnameOutput = String(result[1] || "").trim();
  const uptimeOutput = String(result[2] || "").trim();
  const parsedPorts = parseSshInterfacesStatus(result[3] || "", state.ports || defaultPorts());

  const nextState = {
    ...state,
    device: {
      ...state.device,
      hostname: hostnameOutput.split(/\r?\n/).pop() || state.device.hostname,
      serial: version.serialNumber || state.device.serial,
      eosVersion: version.version || state.device.eosVersion,
      uptime: uptimeOutput.split(/\r?\n/).find((line) => /uptime/i.test(line)) || uptimeOutput || state.device.uptime,
      lastRefresh: nowIso(),
      source: "ssh"
    },
    health: deriveHealth(result[4]),
    ports: parsedPorts,
    events: [
      {
        time: nowIso(),
        level: "success",
        message: `Refreshed device state from ${config.host} over SSH.`
      },
      ...(state.events || [])
    ].slice(0, 50)
  };

  await writeJson(STATE_FILE, nextState);
  return nextState;
}

async function refreshFromDevice() {
  const config = await readJson(CONFIG_FILE, defaultConfig());
  const state = await readJson(STATE_FILE, defaultState());

  if ((config.mode || "ssh") === "ssh") {
    return refreshFromSsh(config, state);
  }

  const result = await runEapiCommands(config, [
    "show version",
    "show hostname",
    "show uptime",
    "show interfaces status",
    "show environment all"
  ]);

  const version = result[0] || {};
  const hostname = result[1] || {};
  const uptime = result[2] || {};
  const parsedPorts = parseInterfacesStatus(result[3]) || state.ports || defaultPorts();

  const nextState = {
    ...state,
    device: {
      ...state.device,
      hostname: hostname.hostname || version.hostname || state.device.hostname,
      serial: version.serialNumber || state.device.serial,
      eosVersion: version.version || state.device.eosVersion,
      uptime: uptime.upTime || uptime.uptime || state.device.uptime,
      lastRefresh: nowIso(),
      source: "eapi"
    },
    health: deriveHealth(result[4]),
    ports: parsedPorts,
    events: [
      {
        time: nowIso(),
        level: "success",
        message: `已从 ${config.host} 刷新设备状态。`
      },
      ...(state.events || [])
    ].slice(0, 50)
  };

  await writeJson(STATE_FILE, nextState);
  return nextState;
}

function updateMockState(state) {
  const tick = Date.now() / 1000;
  return {
    ...state,
    device: {
      ...state.device,
      lastRefresh: nowIso(),
      source: state.device.source || "local"
    },
    health: {
      ...state.health,
      cpu: Math.max(8, Math.min(86, Math.round(20 + Math.sin(tick / 13) * 6))),
      memory: Math.max(20, Math.min(90, Math.round(43 + Math.cos(tick / 17) * 4))),
      temperature: Math.max(28, Math.min(72, Math.round(39 + Math.sin(tick / 19) * 3)))
    },
    ports: (state.ports || defaultPorts()).map((port, index) => {
      if (port.status !== "up") return { ...port, rxMbps: 0, txMbps: 0 };
      const wave = Math.abs(Math.sin(tick / 8 + index));
      return {
        ...port,
        rxMbps: Math.round(300 + wave * (port.speed === "40G" ? 3600 : 900)),
        txMbps: Math.round(260 + (1 - wave / 2) * (port.speed === "40G" ? 2800 : 760))
      };
    })
  };
}

async function handleApi(req, res, pathname) {
  if (req.method === "GET" && pathname === "/api/state") {
    const state = updateMockState(await readJson(STATE_FILE, defaultState()));
    const config = await readJson(CONFIG_FILE, defaultConfig());
    return json(res, 200, { state, config: sanitizeConfig(config) });
  }

  if (req.method === "POST" && pathname === "/api/settings") {
    const input = await readBody(req);
    const current = await readJson(CONFIG_FILE, defaultConfig());
    const mode = input.mode === "eapi" ? "eapi" : "ssh";
    const protocol = input.protocol === "https" ? "https" : "http";
    const defaultPort = mode === "ssh" ? 22 : protocol === "https" ? 443 : 80;
    const next = {
      enabled: Boolean(input.enabled),
      mode,
      protocol,
      host: String(input.host || "").trim(),
      port: Number(input.port || defaultPort),
      username: String(input.username || "").trim(),
      password: input.password ? String(input.password) : current.password,
      commandFormat: "json",
      updatedAt: nowIso()
    };

    await writeJson(CONFIG_FILE, next);
    return json(res, 200, { ok: true, config: sanitizeConfig(next) });
  }

  if (req.method === "POST" && pathname === "/api/refresh") {
    const config = await readJson(CONFIG_FILE, defaultConfig());
    if (!config.enabled) {
      const state = updateMockState(await readJson(STATE_FILE, defaultState()));
      await writeJson(STATE_FILE, state);
      return json(res, 200, { ok: true, state, mode: "local" });
    }

    try {
      const state = await refreshFromDevice();
      return json(res, 200, { ok: true, state, mode: config.mode || "ssh" });
    } catch (error) {
      const state = await readJson(STATE_FILE, defaultState());
      state.events = [
        {
          time: nowIso(),
          level: "error",
          message: `刷新失败：${error.message}`
        },
        ...(state.events || [])
      ].slice(0, 50);
      await writeJson(STATE_FILE, state);
      return json(res, 502, { ok: false, error: error.message, state });
    }
  }

  if (req.method === "POST" && pathname === "/api/command") {
    const input = await readBody(req);
    const command = String(input.command || "").trim();
    if (!isReadOnlyCommand(command)) {
      return json(res, 400, {
        ok: false,
        error: "只允许只读命令：show / ping / traceroute / dir / more。配置、重启、删除类命令已拦截。"
      });
    }

    const config = await readJson(CONFIG_FILE, defaultConfig());
    if (!config.enabled) {
      return json(res, 200, {
        ok: true,
        command,
        mode: "local",
        output: `模拟输出\n> ${command}\n当前未启用 eAPI，后台运行在本地模拟模式。请在“连接”里填写交换机管理地址后再执行真实查询。`
      });
    }

    try {
      if ((config.mode || "ssh") === "ssh") {
        const output = await runSshCommand(config, command);
        return json(res, 200, {
          ok: true,
          command,
          mode: "ssh",
          output
        });
      }

      const result = await runEapiCommands(config, [command], "text");
      return json(res, 200, {
        ok: true,
        command,
        mode: "eapi",
        output: result[0] && (result[0].output || result[0])
      });
    } catch (error) {
      return json(res, 502, { ok: false, command, error: error.message });
    }
  }

  return json(res, 404, { ok: false, error: "API 不存在" });
}

async function serveStatic(req, res, pathname) {
  const safePath = pathname === "/" ? "/index.html" : pathname;
  const filePath = path.normalize(path.join(PUBLIC_DIR, safePath));

  if (!filePath.startsWith(PUBLIC_DIR)) {
    return text(res, 403, "Forbidden");
  }

  try {
    const content = await fs.readFile(filePath);
    const ext = path.extname(filePath);
    res.writeHead(200, {
      "Content-Type": MIME_TYPES[ext] || "application/octet-stream",
      "Cache-Control": "no-store"
    });
    res.end(content);
  } catch {
    text(res, 404, "Not found");
  }
}

async function handleRequest(req, res) {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname.startsWith("/api/")) {
      return await handleApi(req, res, url.pathname);
    }
    return await serveStatic(req, res, url.pathname);
  } catch (error) {
    return json(res, 500, { ok: false, error: error.message });
  }
}

ensureDataFiles()
  .then(() => {
    http.createServer(handleRequest).listen(PORT, () => {
      console.log("");
      console.log("Arista 7050QX Web 后台已启动");
      console.log(`访问地址: http://localhost:${PORT}`);
      console.log("停止服务: Ctrl + C");
      console.log("");
    });
  })
  .catch((error) => {
    console.error("启动失败:", error);
    process.exit(1);
  });
