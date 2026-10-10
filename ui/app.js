const state = {
  settings: null,
  running: false,
  lastLogId: 0,
  poller: null,
};

const elements = {
  timeGroups: document.querySelector("#timeGroups"),
  venuePriority: document.querySelector("#venuePriority"),
  delay: document.querySelector("#delay"),
  start: document.querySelector("#startButton"),
  dryRun: document.querySelector("#dryRunButton"),
  stop: document.querySelector("#stopButton"),
  statusPill: document.querySelector("#statusPill"),
  summary: document.querySelector("#runSummary"),
  logs: document.querySelector("#logs"),
  credentialNotice: document.querySelector("#credentialNotice"),
  customStart: document.querySelector("#customStart"),
  addTime: document.querySelector("#addTimeButton"),
  wakeOption: document.querySelector("#wakeOption"),
  wakeEnabled: document.querySelector("#wakeEnabled"),
  applyWake: document.querySelector("#applyWakeButton"),
  dialog: document.querySelector("#confirmDialog"),
  confirmCheck: document.querySelector("#confirmCheck"),
  confirmStart: document.querySelector("#confirmStart"),
  toast: document.querySelector("#toast"),
};

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { elements.toast.hidden = true; }, 3600);
}

function scheduleSave() {
  clearTimeout(scheduleSave.timer);
  scheduleSave.timer = setTimeout(async () => {
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ settings: collectSettings() }),
      });
    } catch (error) {
      showToast(error.message);
    }
  }, 350);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "操作失败");
  return payload;
}

function move(list, index, direction) {
  const target = index + direction;
  if (target < 0 || target >= list.length) return;
  [list[index], list[target]] = [list[target], list[index]];
}

function renderTimeGroups() {
  elements.timeGroups.innerHTML = "";
  state.settings.time_groups.forEach((group, index) => {
    const item = document.createElement("div");
    item.className = `sort-item${group.enabled ? "" : " disabled"}`;
    item.innerHTML = `
      <input type="checkbox" ${group.enabled ? "checked" : ""} aria-label="启用 ${group.label}" />
      <div class="sort-copy"><strong>${group.label}</strong><small>${group.slots.join(" · ")}</small></div>
      <div class="move-buttons">
        <button class="icon-button up" ${index === 0 ? "disabled" : ""} aria-label="上移">↑</button>
        <button class="icon-button down" ${index === state.settings.time_groups.length - 1 ? "disabled" : ""} aria-label="下移">↓</button>
        ${group.custom ? '<button class="icon-button remove" aria-label="删除自定义时间">×</button>' : ""}
      </div>`;
    item.querySelector("input").addEventListener("change", (event) => {
      group.enabled = event.target.checked;
      renderTimeGroups();
      scheduleSave();
    });
    item.querySelector(".up").addEventListener("click", () => {
      move(state.settings.time_groups, index, -1); renderTimeGroups(); scheduleSave();
    });
    item.querySelector(".down").addEventListener("click", () => {
      move(state.settings.time_groups, index, 1); renderTimeGroups(); scheduleSave();
    });
    item.querySelector(".remove")?.addEventListener("click", () => {
      state.settings.time_groups.splice(index, 1); renderTimeGroups(); scheduleSave();
    });
    elements.timeGroups.appendChild(item);
  });
}

function renderVenues() {
  elements.venuePriority.innerHTML = "";
  state.settings.venue_priority.forEach((venue, index) => {
    const item = document.createElement("div");
    item.className = "sort-item";
    item.innerHTML = `
      <span class="rank">${index + 1}</span>
      <div class="sort-copy"><strong>${venue} 号场</strong><small>优先级 ${index + 1}</small></div>
      <div class="move-buttons">
        <button class="icon-button up" ${index === 0 ? "disabled" : ""} aria-label="上移">↑</button>
        <button class="icon-button down" ${index === state.settings.venue_priority.length - 1 ? "disabled" : ""} aria-label="下移">↓</button>
      </div>`;
    item.querySelector(".up").addEventListener("click", () => {
      move(state.settings.venue_priority, index, -1); renderVenues(); scheduleSave();
    });
    item.querySelector(".down").addEventListener("click", () => {
      move(state.settings.venue_priority, index, 1); renderVenues(); scheduleSave();
    });
    elements.venuePriority.appendChild(item);
  });
}

function collectSettings() {
  return {
    ...state.settings,
    release_delay_seconds: Number(elements.delay.value),
    wake_enabled: state.settings.wake_enabled,
  };
}

function updateStatus(status) {
  state.running = status.running;
  elements.start.disabled = status.running;
  elements.dryRun.disabled = status.running;
  elements.stop.hidden = !status.running;
  elements.statusPill.className = `status-pill${status.running ? " running" : status.return_code && status.return_code !== 0 ? " error" : ""}`;
  elements.statusPill.querySelector("b").textContent = status.running ? "任务运行中" : "当前空闲";
  if (status.running) {
    const mode = status.mode === "live" ? "真实预约" : "安全测试";
    elements.summary.textContent = `${mode}正在运行 · 启动于 ${status.started_at || "刚刚"}`;
  } else if (status.return_code !== null && status.return_code !== undefined) {
    elements.summary.textContent = status.return_code === 0 ? "任务已正常结束" : `任务已结束 · 退出码 ${status.return_code}`;
  } else {
    elements.summary.textContent = "尚未启动任务";
  }
  if (status.logs?.length) {
    if (elements.logs.textContent === "等待启动…") elements.logs.textContent = "";
    elements.logs.textContent += status.logs.map((item) => item.line).join("\n") + "\n";
    elements.logs.scrollTop = elements.logs.scrollHeight;
    state.lastLogId = status.last_log_id;
  }
}

async function startTask(live, confirmed = false) {
  try {
    const status = await api("/api/start", {
      method: "POST",
      body: JSON.stringify({ live, confirmed, settings: collectSettings() }),
    });
    state.settings = collectSettings();
    updateStatus(status);
    showToast(live ? "预约任务已开启" : "安全测试已启动");
  } catch (error) {
    showToast(error.message);
  }
}

async function pollStatus() {
  try {
    const status = await api(`/api/status?since=${state.lastLogId}`);
    updateStatus(status);
  } catch (_) {
    elements.statusPill.className = "status-pill error";
    elements.statusPill.querySelector("b").textContent = "连接中断";
  }
}

elements.start.addEventListener("click", () => {
  elements.confirmCheck.checked = false;
  elements.confirmStart.disabled = true;
  elements.dialog.showModal();
});
elements.confirmCheck.addEventListener("change", () => {
  elements.confirmStart.disabled = !elements.confirmCheck.checked;
});
elements.confirmStart.addEventListener("click", (event) => {
  event.preventDefault();
  elements.dialog.close();
  startTask(true, true);
});
elements.dryRun.addEventListener("click", () => startTask(false));
elements.delay.addEventListener("change", scheduleSave);
elements.applyWake.addEventListener("click", async () => {
  elements.applyWake.disabled = true;
  elements.applyWake.textContent = "正在等待系统确认…";
  try {
    const payload = await api("/api/wake", {
      method: "POST",
      body: JSON.stringify({
        settings: { ...collectSettings(), wake_enabled: elements.wakeEnabled.checked },
      }),
    });
    state.settings = payload.settings;
    showToast(payload.message);
  } catch (error) {
    elements.wakeEnabled.checked = state.settings.wake_enabled;
    showToast(error.message);
  } finally {
    elements.applyWake.disabled = false;
    elements.applyWake.textContent = "应用唤醒设置";
  }
});
elements.addTime.addEventListener("click", () => {
  const start = Number(elements.customStart.value);
  const slots = [
    `${String(start).padStart(2, "0")}:00-${String(start + 1).padStart(2, "0")}:00`,
    `${String(start + 1).padStart(2, "0")}:00-${String(start + 2).padStart(2, "0")}:00`,
  ];
  if (state.settings.time_groups.some((group) => group.slots.join("|") === slots.join("|"))) {
    showToast("这个时间窗口已经在列表中");
    return;
  }
  state.settings.time_groups.push({
    name: `custom_${String(start).padStart(2, "0")}_${String(start + 2).padStart(2, "0")}`,
    label: `${String(start).padStart(2, "0")}:00–${String(start + 2).padStart(2, "0")}:00`,
    slots,
    enabled: true,
    custom: true,
  });
  renderTimeGroups();
  scheduleSave();
});
elements.stop.addEventListener("click", async () => {
  try {
    const status = await api("/api/stop", { method: "POST", body: "{}" });
    updateStatus(status);
    showToast("正在停止任务");
  } catch (error) { showToast(error.message); }
});
document.querySelector("#clearLogs").addEventListener("click", () => {
  elements.logs.textContent = "";
});

async function initialize() {
  try {
    for (let hour = 8; hour <= 20; hour += 1) {
      const option = document.createElement("option");
      option.value = String(hour);
      option.textContent = `${String(hour).padStart(2, "0")}:00–${String(hour + 2).padStart(2, "0")}:00`;
      elements.customStart.appendChild(option);
    }
    const payload = await api("/api/config");
    state.settings = payload.settings;
    elements.delay.value = state.settings.release_delay_seconds;
    elements.wakeEnabled.checked = state.settings.wake_enabled;
    elements.wakeOption.hidden = !payload.wake_supported;
    elements.credentialNotice.hidden = payload.credentials_ready;
    renderTimeGroups();
    renderVenues();
    await pollStatus();
    state.poller = setInterval(pollStatus, 1000);
  } catch (error) {
    showToast(error.message);
  }
}

initialize();
