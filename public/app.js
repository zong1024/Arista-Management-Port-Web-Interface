const $ = (selector) => document.querySelector(selector);

const elements = {
  connectionBadge: $("#connectionBadge"),
  refreshBtn: $("#refreshBtn"),
  hostname: $("#hostname"),
  eosVersion: $("#eosVersion"),
  portSummary: $("#portSummary"),
  lastRefresh: $("#lastRefresh"),
  cpuMeter: $("#cpuMeter"),
  cpuValue: $("#cpuValue"),
  memoryMeter: $("#memoryMeter"),
  memoryValue: $("#memoryValue"),
  temperatureMeter: $("#temperatureMeter"),
  temperatureValue: $("#temperatureValue"),
  fanStatus: $("#fanStatus"),
  psuStatus: $("#psuStatus"),
  portGrid: $("#portGrid"),
  settingsForm: $("#settingsForm"),
  enabled: $("#enabled"),
  mode: $("#mode"),
  modeHint: $("#modeHint"),
  protocolLabel: $("#protocolLabel"),
  protocol: $("#protocol"),
  host: $("#host"),
  port: $("#port"),
  username: $("#username"),
  password: $("#password"),
  commandForm: $("#commandForm"),
  commandInput: $("#commandInput"),
  commandOutput: $("#commandOutput"),
  eventList: $("#eventList"),
  toast: $("#toast")
};

let toastTimer = null;

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 2600);
}

function formatTime(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(value));
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function syncModeFields() {
  const mode = elements.mode.value;
  elements.modeHint.textContent = mode === "ssh" ? "SSH Console" : "Arista eAPI";
  elements.protocolLabel.style.display = mode === "ssh" ? "none" : "grid";

  const current = elements.port.value;
  if (!current || current === "22" || current === "80" || current === "443") {
    elements.port.value = mode === "ssh" ? 22 : elements.protocol.value === "https" ? 443 : 80;
  }
}

function renderConfig(config) {
  const mode = config.mode || "ssh";
  elements.enabled.checked = Boolean(config.enabled);
  elements.mode.value = mode;
  elements.protocol.value = config.protocol || "http";
  elements.host.value = config.host || "";
  elements.port.value = config.port || (mode === "ssh" ? 22 : elements.protocol.value === "https" ? 443 : 80);
  elements.username.value = config.username || "";
  elements.password.value = "";
  syncModeFields();

  if (config.enabled && config.host) {
    elements.connectionBadge.textContent = `${mode === "ssh" ? "SSH" : "eAPI"} ${config.host}`;
    elements.connectionBadge.className = "badge badge-live";
  } else {
    elements.connectionBadge.textContent = "本地模拟";
    elements.connectionBadge.className = "badge badge-neutral";
  }
}

function renderState(state) {
  const device = state.device || {};
  const health = state.health || {};
  const ports = state.ports || [];
  const upPorts = ports.filter((port) => port.status === "up").length;
  const errorPorts = ports.filter((port) => Number(port.errors || 0) > 0).length;

  elements.hostname.textContent = device.hostname || "-";
  elements.eosVersion.textContent = device.eosVersion || "-";
  elements.portSummary.textContent = `${upPorts}/${ports.length} up, ${errorPorts} error`;
  elements.lastRefresh.textContent = formatTime(device.lastRefresh);

  elements.cpuMeter.value = Number(health.cpu || 0);
  elements.cpuValue.textContent = `${health.cpu || 0}%`;
  elements.memoryMeter.value = Number(health.memory || 0);
  elements.memoryValue.textContent = `${health.memory || 0}%`;
  elements.temperatureMeter.value = Number(health.temperature || 0);
  elements.temperatureValue.textContent = `${health.temperature || 0}C`;
  elements.fanStatus.textContent = health.fanStatus || "-";
  elements.psuStatus.textContent = health.psuStatus || "-";

  elements.portGrid.innerHTML = ports
    .map(
      (port) => `
        <article class="port" data-status="${port.status}" data-errors="${Number(port.errors || 0) > 0}">
          <div class="port-name">
            <span>${port.name}</span>
            <span class="port-speed">${port.speed}</span>
          </div>
          <div class="port-detail">
            ${port.media} / ${port.vlan}<br />
            ${port.description || port.role || ""}
          </div>
          <div class="port-traffic">
            <span>RX <b>${Math.round(port.rxMbps || 0)}M</b></span>
            <span>TX <b>${Math.round(port.txMbps || 0)}M</b></span>
          </div>
        </article>
      `
    )
    .join("");

  elements.eventList.innerHTML = (state.events || [])
    .map(
      (event) => `
        <li data-level="${event.level || "info"}">
          <span class="event-time">${formatTime(event.time)} / ${event.level || "info"}</span>
          <span class="event-message">${event.message}</span>
        </li>
      `
    )
    .join("");
}

async function loadState() {
  const payload = await requestJson("/api/state");
  renderConfig(payload.config);
  renderState(payload.state);
}

async function refreshState() {
  elements.refreshBtn.disabled = true;
  elements.refreshBtn.textContent = "刷新中";
  try {
    const payload = await requestJson("/api/refresh", { method: "POST", body: "{}" });
    renderState(payload.state);
    showToast(payload.mode === "local" ? "已刷新本地模拟状态" : `已通过 ${payload.mode.toUpperCase()} 刷新`);
  } catch (error) {
    showToast(error.message);
    await loadState();
  } finally {
    elements.refreshBtn.disabled = false;
    elements.refreshBtn.textContent = "刷新";
  }
}

elements.refreshBtn.addEventListener("click", refreshState);
elements.mode.addEventListener("change", syncModeFields);

elements.protocol.addEventListener("change", () => {
  if (!elements.port.value || elements.port.value === "80" || elements.port.value === "443") {
    elements.port.value = elements.protocol.value === "https" ? 443 : 80;
  }
});

elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    enabled: elements.enabled.checked,
    mode: elements.mode.value,
    protocol: elements.protocol.value,
    host: elements.host.value,
    port: Number(elements.port.value || (elements.mode.value === "ssh" ? 22 : 80)),
    username: elements.username.value,
    password: elements.password.value
  };

  try {
    const result = await requestJson("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    renderConfig(result.config);
    showToast("连接配置已保存");
  } catch (error) {
    showToast(error.message);
  }
});

elements.commandForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const command = elements.commandInput.value.trim();
  if (!command) return;

  elements.commandOutput.textContent = `> ${command}\n运行中...`;
  try {
    const payload = await requestJson("/api/command", {
      method: "POST",
      body: JSON.stringify({ command })
    });
    const output = typeof payload.output === "string" ? payload.output : JSON.stringify(payload.output, null, 2);
    elements.commandOutput.textContent = `> ${payload.command}\n${output}`;
  } catch (error) {
    elements.commandOutput.textContent = `> ${command}\nERROR: ${error.message}`;
  }
});

loadState().catch((error) => showToast(error.message));
setInterval(() => {
  loadState().catch(() => {});
}, 15000);
