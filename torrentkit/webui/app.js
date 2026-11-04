(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    preview: null,
    pending: null,
    busy: false,
    jobs: [],
    loadingJobs: false,
    timer: null,
    defaultDir: "",
    dirPath: "",
    dirParent: "",
    offline: false,
  };

  const STATUS_TEXT = {
    queued: "排队中",
    preparing: "准备文件",
    resolving: "获取元数据",
    verifying: "校验数据",
    downloading: "下载中",
    completed: "已完成",
    stopped: "已停止",
    error: "出错",
  };

  const RUNNING_STATES = new Set(["preparing", "resolving", "verifying", "downloading"]);
  const CANCELLABLE_STATES = new Set(["queued", ...RUNNING_STATES]);

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function humanBytes(value) {
    let size = Number(value) || 0;
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    for (const unit of units) {
      if (Math.abs(size) < 1024 || unit === units[units.length - 1]) {
        return unit === "B" ? `${Math.round(size)} B` : `${size.toFixed(2)} ${unit}`;
      }
      size /= 1024;
    }
    return `${size.toFixed(2)} PiB`;
  }

  function humanSpeed(value) {
    return `${humanBytes(value)}/s`;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) return `${hours} 小时 ${minutes} 分`;
    if (minutes > 0) return `${minutes} 分 ${secs} 秒`;
    return `${secs} 秒`;
  }

  function rememberSaveDir(value) {
    try {
      if (value) localStorage.setItem("torrentkit.saveDir", value);
    } catch {
      /* storage may be disabled */
    }
  }

  function toast(message, kind = "info") {    const host = $("toasts");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 260);
    }, 4200);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok || !data || data.ok === false) {
      const message = (data && data.error) || `请求失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return data;
  }

  function setServerStatus(online, detail) {
    const pill = $("server-status");
    pill.classList.toggle("online", online);
    pill.classList.toggle("offline", !online);
    $("server-label").textContent = online ? "本地服务在线" : "服务未连接";
    if (detail) pill.title = `下载目录：${detail}`;
  }

  function setBusy(busy) {
    state.busy = busy;
    $("parse-btn").disabled = busy;
    refreshDownloadButton();
  }

  function refreshDownloadButton() {
    const v2Only = Boolean(state.preview && state.preview.v2_only);
    $("download-btn").disabled = state.busy || !state.pending || v2Only;
  }

  async function fileToBase64(file) {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunk = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunk));
    }
    return btoa(binary);
  }

  function renderFileList(data) {
    const details = $("pv-file-details");
    const list = $("pv-files-list");
    const files = data.files || [];
    if (!files.length) {
      details.hidden = true;
      list.innerHTML = "";
      return;
    }
    const total = data.files_total || files.length;
    $("pv-file-count").textContent = `共 ${total} 个`;
    const shown = files.slice(0, 200);
    let html = shown.map((file) => (
      `<li><span class="file-path" title="${escapeHtml(file.path)}">${escapeHtml(file.path)}</span>` +
      `<span class="file-size">${humanBytes(file.length)}</span></li>`
    )).join("");
    if (total > shown.length) {
      html += `<li><span class="file-path">还有 ${total - shown.length} 个文件未显示</span></li>`;
    }
    list.innerHTML = html;
    details.hidden = false;
    details.open = shown.length <= 8;
  }

  function showPreview(data) {
    state.preview = data;
    $("preview-empty").hidden = true;
    $("preview-content").hidden = false;
    $("clear-preview").hidden = false;

    $("pv-name").textContent = data.name || "未命名";
    $("pv-source").textContent = data.source === "magnet"
      ? (data.resolved ? "磁力链接" : "磁力链接 · 待获取元数据")
      : "种子文件";
    $("pv-size").textContent = data.total_length ? humanBytes(data.total_length) : "未知";
    $("pv-pieces").textContent = data.piece_count ? String(data.piece_count) : "—";
    $("pv-trackers").textContent = String((data.trackers || []).length);
    $("pv-files").textContent = data.files_total ? String(data.files_total) : "—";
    $("pv-hash").textContent = data.info_hash || "—";
    $("pv-magnet").value = data.magnet || "";

    const warnText = data.v2_only
      ? "纯 BitTorrent v2 种子暂不支持下载，仅能查看信息。"
      : (data.warning || "");
    $("pv-warn").hidden = !warnText;
    $("pv-warn").textContent = warnText;

    renderFileList(data);
    refreshDownloadButton();
  }

  function clearPreview() {
    state.preview = null;
    $("preview-content").hidden = true;
    $("preview-empty").hidden = false;
    $("clear-preview").hidden = true;
    $("pv-warn").hidden = true;
    $("pv-file-details").hidden = true;
    $("pv-files-list").innerHTML = "";
    $("pv-magnet").value = "";
    refreshDownloadButton();
  }

  function resetComposer() {
    state.pending = null;
    $("file-input").value = "";
    $("magnet-input").value = "";
    clearPreview();
  }

  async function parseTorrentFile(file) {
    if (!file) return;
    if (file.size > 16 * 1024 * 1024) {
      toast("种子文件过大（上限 16 MiB）", "error");
      return;
    }
    setBusy(true);
    try {
      const torrent = await fileToBase64(file);
      const data = await api("/api/parse", {
        method: "POST",
        body: JSON.stringify({ torrent, filename: file.name }),
      });
      state.pending = { torrent };
      showPreview(data);
      toast(`已解析：${data.name}`, "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function parseMagnet() {
    const magnet = $("magnet-input").value.trim();
    if (!magnet) {
      toast("请先粘贴磁力链接，或拖入 .torrent 文件", "error");
      return;
    }
    setBusy(true);
    try {
      const data = await api("/api/parse", {
        method: "POST",
        body: JSON.stringify({ magnet }),
      });
      state.pending = { magnet };
      showPreview(data);
      if (data.info_hash) {
        toast(`已解析磁力链接：${data.name}`, "success");
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function readPeers() {
    return $("peers-input").value
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function readTrackers() {
    return $("trackers-input").value
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  async function startDownload() {
    if (!state.pending) return;
    const maxPeers = Number($("max-peers").value) || 100;
    const payload = {
      ...state.pending,
      peers: readPeers(),
      trackers: readTrackers(),
      use_dht: $("use-dht").checked,
      rarest_first: $("rarest").checked,
      max_peers: maxPeers,
    };
    const saveDir = $("save-dir").value.trim();
    if (saveDir) {
      payload.save_dir = saveDir;
      rememberSaveDir(saveDir);
    }
    setBusy(true);
    try {
      const data = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast(`任务已创建：${data.job.name}`, "success");
      resetComposer();
      await loadJobs();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function jobCard(job) {
    const running = RUNNING_STATES.has(job.state);
    const cancellable = CANCELLABLE_STATES.has(job.state);
    const percent = Math.min(100, Math.max(0, (Number(job.progress) || 0) * 100));
    const waiting = job.state === "downloading" && !job.peers && !job.speed;
    const statusText = waiting && !job.downloaded
      ? "等待 Peer"
      : STATUS_TEXT[job.state] || job.state;
    const pillState = waiting ? "waiting" : job.state;
    const eta = job.speed > 0 && job.total_length > job.downloaded
      ? (job.total_length - job.downloaded) / job.speed
      : null;
    const hash = job.info_hash ? `${job.info_hash.slice(0, 16)}…` : "—";

    let hint = "";
    if (waiting) {
      if (job.downloaded > 0) {
        hint = "当前没有已连接的 Peer，下载已暂停，正在重试。";
      } else if (!job.trackers || job.trackers.length === 0) {
        hint = job.use_dht
          ? "没有 Tracker，正在通过 DHT 查找 Peer… 也可以手动补充直连 Peer。"
          : "这个种子没有 Tracker，也没有可用的直连 Peer。请在高级选项里填写 Peer 后重新创建任务。";
      } else if (job.tracker_failed > 0 && !job.tracker_ok) {
        hint = `Tracker 连接失败：${job.tracker_error || "未知错误"}`;
      } else if (job.tracker_ok > 0 && job.tracker_peers === 0) {
        hint = "Tracker 已响应，但返回 0 个可用 Peer。正在通过 DHT 继续查找…";
      } else {
        hint = "正在等待 Peer 连接… 如果长时间没有速度，请检查防火墙或添加直连 Peer。";
      }
    }

    let barPercent = percent;
    let pctText = `${percent.toFixed(1)}%`;
    if (job.state === "verifying" && job.verify_total > 0) {
      barPercent = Math.min(100, (job.verify_done / job.verify_total) * 100);
      pctText = `校验 ${barPercent.toFixed(0)}%`;
    } else if (job.state === "preparing") {
      if (job.prepare_total > 0) {
        barPercent = Math.min(100, (job.prepare_done / job.prepare_total) * 100);
        pctText = `准备 ${barPercent.toFixed(0)}%`;
      } else {
        pctText = "准备中…";
      }
    } else if (job.state === "queued") {
      pctText = job.queue_position ? `第 ${job.queue_position} 位` : "排队中";
    }

    return `
      <article class="job ${running ? "active" : ""}" data-job="${job.id}">
        <div class="job-head">
          <div class="job-title">
            <h3 title="${escapeHtml(job.name)}">${escapeHtml(job.name)}</h3>
            <span class="hash">${escapeHtml(hash)}</span>
            <span class="job-dest" title="${escapeHtml(job.save_path || "")}">→ ${escapeHtml(job.save_path || "")}</span>
          </div>
          <span class="pill ${escapeHtml(pillState)}">${escapeHtml(statusText)}</span>
        </div>
        <div class="progress-row">
          <div class="bar"><i style="width:${barPercent.toFixed(2)}%"></i></div>
          <span class="pct">${pctText}</span>
        </div>
        <div class="metrics">
          <div class="metric"><span>大小</span><strong>${job.total_length ? humanBytes(job.total_length) : "—"}</strong></div>
          <div class="metric"><span>已下载</span><strong>${humanBytes(job.downloaded)}</strong></div>
          <div class="metric"><span>速度</span><strong>${job.speed ? humanSpeed(job.speed) : "—"}</strong></div>
          <div class="metric"><span>连接</span><strong>${job.peers}</strong></div>
          <div class="metric"><span>分片</span><strong>${job.pieces_done}/${job.pieces_total || "—"}</strong></div>
          ${eta !== null ? `<div class="metric"><span>剩余</span><strong>${formatDuration(eta)}</strong></div>` : ""}
        </div>
        ${hint ? `<p class="job-hint">${escapeHtml(hint)}</p>` : ""}
        ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}
        <div class="job-actions">
          ${cancellable ? '<button class="btn small ghost" type="button" data-action="sources">补充 Peer/Tracker</button>' : ""}
          ${cancellable ? '<button class="btn small ghost" type="button" data-action="stop">停止</button>' : ""}
          ${job.state === "completed" ? '<button class="btn small ghost" type="button" data-action="reveal">打开目录</button>' : ""}
          ${cancellable ? "" : '<button class="btn small ghost danger" type="button" data-action="remove">移除</button>'}
        </div>
      </article>`;
  }

  function renderJobs() {
    const jobs = state.jobs || [];
    const active = jobs.filter((job) => CANCELLABLE_STATES.has(job.state)).length;
    $("jobs-summary").textContent = jobs.length
      ? `${jobs.length} 个任务 · ${active} 个进行中`
      : "";
    $("jobs-empty").hidden = jobs.length > 0;
    $("jobs-list").innerHTML = jobs.map(jobCard).join("");
  }

  async function loadJobs() {
    if (state.loadingJobs) return;
    state.loadingJobs = true;
    try {
      const data = await api("/api/jobs");
      state.jobs = data.jobs || [];
      state.offline = false;
      renderJobs();
      setServerStatus(true, data.download_dir);
    } catch (error) {
      setServerStatus(false);
      $("jobs-summary").textContent = "获取任务失败，正在重试…";
      if (!state.offline) {
        state.offline = true;
        toast("获取任务失败，正在重试…", "error");
      }
    } finally {
      state.loadingJobs = false;
    }
  }

  function copyText(target) {
    const text = target.value !== undefined ? target.value : target.textContent;
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    if (target.select) {
      target.select();
    } else {
      const range = document.createRange();
      range.selectNodeContents(target);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    document.execCommand("copy");
    return Promise.resolve();
  }

  function setDirError(message, kind = "error") {
    const element = $("dir-error");
    element.hidden = !message;
    element.textContent = message || "";
    element.classList.toggle("warn", kind === "warn");
  }

  async function loadDirectory(pathText) {
    setDirError("");
    try {
      const data = await api(`/api/fs?path=${encodeURIComponent(pathText)}`);
      state.dirPath = data.path;
      state.dirParent = data.parent || "";
      $("dir-path").textContent = data.path;
      $("dir-up").disabled = !data.parent;
      $("dir-roots").innerHTML = (data.roots || [])
        .map((root) => `<button type="button" class="btn tiny ghost" data-root="${escapeHtml(root.path)}">${escapeHtml(root.name)}</button>`)
        .join("");
      $("dir-list").innerHTML = data.dirs.length
        ? data.dirs
            .map((dir) => `<li><button type="button" class="dir-item" data-path="${escapeHtml(dir.path)}"><span>${escapeHtml(dir.name)}</span><span class="chev">›</span></button></li>`)
            .join("")
        : '<li class="dir-empty">没有子文件夹</li>';
      $("dir-select").disabled = false;
      if (!data.writable) {
        setDirError("这个目录当前不可写，下载可能会失败。", "warn");
      } else if (data.truncated) {
        setDirError("目录项过多，只显示前 500 个文件夹。", "warn");
      }
    } catch (error) {
      state.dirPath = "";
      state.dirParent = "";
      $("dir-path").textContent = pathText || "—";
      $("dir-list").innerHTML = "";
      $("dir-roots").innerHTML = "";
      $("dir-up").disabled = true;
      $("dir-select").disabled = true;
      setDirError(error.message, "error");
    }
  }

  async function openDirPicker() {
    $("dir-modal").hidden = false;
    document.body.classList.add("modal-open");
    const start = $("save-dir").value.trim() || state.defaultDir;
    await loadDirectory(start);
    $("new-dir-name").focus();
  }

  function closeDirPicker() {
    $("dir-modal").hidden = true;
    document.body.classList.remove("modal-open");
  }

  async function createDirectory() {
    const name = $("new-dir-name").value.trim();
    if (!name) {
      setDirError("请输入文件夹名称。", "error");
      return;
    }
    if (!state.dirPath) {
      setDirError("请先选择一个父目录。", "error");
      return;
    }
    try {
      const data = await api("/api/fs/mkdir", {
        method: "POST",
        body: JSON.stringify({ parent: state.dirPath, name }),
      });
      $("new-dir-name").value = "";
      await loadDirectory(data.path);
    } catch (error) {
      setDirError(error.message, "error");
    }
  }

  function bindEvents() {
    const dropzone = $("dropzone");
    const fileInput = $("file-input");

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    fileInput.addEventListener("change", () => {
      parseTorrentFile(fileInput.files[0]);
      fileInput.value = "";
    });

    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach((name) => {
      dropzone.addEventListener(name, () => dropzone.classList.remove("dragover"));
    });
    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
      const file = event.dataTransfer && event.dataTransfer.files[0];
      parseTorrentFile(file);
    });
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());

    $("parse-btn").addEventListener("click", parseMagnet);
    $("download-btn").addEventListener("click", startDownload);
    $("reset-btn").addEventListener("click", resetComposer);
    $("clear-preview").addEventListener("click", resetComposer);

    $("browse-dir").addEventListener("click", openDirPicker);
    $("dir-close").addEventListener("click", closeDirPicker);
    $("dir-cancel").addEventListener("click", closeDirPicker);
    $("dir-modal").addEventListener("click", (event) => {
      if (event.target === $("dir-modal")) closeDirPicker();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("dir-modal").hidden) closeDirPicker();
    });
    $("dir-up").addEventListener("click", () => {
      if (state.dirParent) loadDirectory(state.dirParent);
    });
    $("dir-refresh").addEventListener("click", () => {
      loadDirectory(state.dirPath || state.defaultDir);
    });
    $("dir-select").addEventListener("click", () => {
      if (!state.dirPath) return;
      $("save-dir").value = state.dirPath;
      rememberSaveDir(state.dirPath);
      closeDirPicker();
      toast(`下载目录：${state.dirPath}`, "success");
    });
    $("new-dir-btn").addEventListener("click", createDirectory);
    $("new-dir-name").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        createDirectory();
      }
    });
    $("dir-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-path]");
      if (button) loadDirectory(button.dataset.path);
    });
    $("dir-roots").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-root]");
      if (button) loadDirectory(button.dataset.root);
    });

    $("magnet-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        parseMagnet();
      }
    });

    document.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-copy]");
      if (!button) return;
      const target = $(button.dataset.copy);
      if (!target) return;
      try {
        await copyText(target);
        toast("已复制到剪贴板", "success");
      } catch {
        toast("复制失败，请手动选择文本", "error");
      }
    });

    $("jobs-list").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const card = button.closest("[data-job]");
      if (!card) return;
      const id = card.dataset.job;
      const action = button.dataset.action;
      if (button.disabled) return;
      button.disabled = true;
      try {
        if (action === "stop") {
          await api(`/api/jobs/${id}/stop`, { method: "POST", body: "{}" });
          toast("已请求停止任务", "info");
        } else if (action === "sources") {
          const peersText = window.prompt("输入直连 Peer（每行一个 HOST:PORT，可留空）", "");
          if (peersText === null) {
            button.disabled = false;
            return;
          }
          const trackersText = window.prompt("输入额外 Tracker（每行一个 http(s):// 或 udp:// 地址，可留空）", "");
          if (trackersText === null) {
            button.disabled = false;
            return;
          }
          const peers = peersText.split(/[\\s,;]+/).map((item) => item.trim()).filter(Boolean);
          const trackers = trackersText.split(/[\\s,;]+/).map((item) => item.trim()).filter(Boolean);
          await api(`/api/jobs/${id}/sources`, {
            method: "POST",
            body: JSON.stringify({ peers, trackers, use_dht: true }),
          });
          toast("已补充 Peer/Tracker，任务继续查找节点", "success");
        } else if (action === "remove") {
          if (!window.confirm("从列表移除任务？已下载的文件不会被删除。")) {
            button.disabled = false;
            return;
          }
          await api(`/api/jobs/${id}`, { method: "DELETE" });
        } else if (action === "reveal") {
          await api(`/api/jobs/${id}/reveal`, { method: "POST", body: "{}" });
        }
        await loadJobs();
      } catch (error) {
        if (String(error.message).includes("任务不存在")) {
          await loadJobs();
          toast("任务已不存在，列表已刷新", "error");
        } else {
          toast(error.message, "error");
        }
        button.disabled = false;
      }
    });
  }

  async function boot() {
    bindEvents();
    try {
      const health = await api("/api/health");
      $("version").textContent = health.version ? `v${health.version}` : "";
      state.defaultDir = health.download_dir || "";
      let remembered = "";
      try {
        remembered = localStorage.getItem("torrentkit.saveDir") || "";
      } catch {
        remembered = "";
      }
      $("save-dir").value = remembered || state.defaultDir;
      setServerStatus(true, health.download_dir);
    } catch {
      setServerStatus(false);
    }
    await loadJobs();
    state.timer = setInterval(loadJobs, 1200);
  }

  boot();
})();
