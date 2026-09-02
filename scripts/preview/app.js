(function () {
  "use strict";

  const API_BASE = "/api/v1/imagery";
  const POLL_INTERVAL_MS = 2000;
  const WS_CONNECT_TIMEOUT_MS = 5000;

  if (typeof Cesium.Ion !== "undefined") {
    Cesium.Ion.defaultAccessToken = undefined;
  }

  const statusEl = document.getElementById("status");
  const coordsEl = document.getElementById("coords");
  const toastContainer = document.getElementById("toastContainer");

  const panels = {
    ingest: document.getElementById("ingestPanel"),
    progress: document.getElementById("progressPanel"),
    publish: document.getElementById("publishPanel"),
    layers: document.getElementById("layersPanel"),
  };
  const overlay = document.getElementById("panelOverlay");

  let viewer = null;
  let overlayLayer = null;
  let minZoom = 0;
  let maxZoom = 22;
  let currentTileset = null;
  let currentLat = "—";
  let currentLng = "—";
  let currentZoom = "—";
  let pollTimer = null;
  let jobSocket = null;
  let trackingJobId = null;
  let activePanel = null;
  let activeSubmitTab = "upload";
  let workspaceRelativePath = "";
  let selectedWorkspaceFile = null;
  let lastJobDetail = null;

  function showToast(message, type) {
    const toast = document.createElement("div");
    toast.className = "toast" + (type ? " " + type : "");
    toast.textContent = message;
    toastContainer.appendChild(toast);
    setTimeout(function () {
      toast.remove();
    }, 4000);
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function formatCoord(deg) {
    return deg.toFixed(6) + "°";
  }

  function renderCoords() {
    coordsEl.textContent =
      "经度: " + currentLng + "　纬度: " + currentLat + "　层级: " + currentZoom;
  }

  function getZoomLevel() {
    if (!viewer) return null;

    const canvas = viewer.scene.canvas;
    if (!canvas || canvas.clientWidth <= 0) return null;

    const carto = viewer.camera.positionCartographic;
    if (!carto) return null;
    const height = carto.height;
    if (!height || height <= 0) return null;

    const fovy = viewer.camera.frustum.fovy;
    if (!fovy) return null;

    const metersPerPixel =
      (2 * height * Math.tan(fovy / 2)) / canvas.clientHeight;
    if (!metersPerPixel || metersPerPixel <= 0) return null;

    const earthCircumference =
      2 * Math.PI * 6378137 * Math.cos(carto.latitude);
    const zoom = Math.log2(earthCircumference / (256 * metersPerPixel));
    return Math.max(minZoom, Math.min(maxZoom, Math.round(zoom)));
  }

  function parseBounds(bounds) {
    if (!Array.isArray(bounds) || bounds.length !== 4) return null;
    const values = bounds.map(Number);
    const west = values[0];
    const south = values[1];
    const east = values[2];
    const north = values[3];
    if (values.some(function (v) {
      return Number.isNaN(v);
    })) {
      return null;
    }
    if (west < -180 || east > 180 || south < -90 || north > 90) return null;
    if (west >= east || south >= north) return null;
    return { west: west, south: south, east: east, north: north };
  }

  async function apiFetch(path, options) {
    const response = await fetch(API_BASE + path, options);
    let payload = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.indexOf("application/json") !== -1) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (!response.ok) {
      const detail =
        payload && payload.detail
          ? payload.detail
          : typeof payload === "string"
            ? payload
            : "HTTP " + response.status;
      throw new Error(
        typeof detail === "string" ? detail : JSON.stringify(detail),
      );
    }
    return payload;
  }

  function setJobIdFields(jobId) {
    document.getElementById("jobIdInput").value = jobId;
    document.getElementById("publishJobIdInput").value = jobId;
  }

  function updateUrlTileset(name) {
    const url = new URL(window.location.href);
    if (name) {
      url.searchParams.set("tileset", name);
    } else {
      url.searchParams.delete("tileset");
    }
    window.history.replaceState({}, "", url.toString());
  }

  function removeOverlayLayer() {
    if (!viewer || !overlayLayer) return;
    viewer.imageryLayers.remove(overlayLayer, true);
    overlayLayer = null;
  }

  async function loadTileset(name, options) {
    if (!viewer) return;

    const shouldFly = options && options.flyTo === true;

    const metadataUrl = "/imagery/" + encodeURIComponent(name) + "/tile.json";
    setStatus("Loading tileset: " + name + "…");

    const response = await fetch(metadataUrl);
    if (!response.ok) {
      throw new Error("HTTP " + response.status + " for " + metadataUrl);
    }

    const meta = await response.json();
    let urlTemplate =
      (meta.tiles && meta.tiles[0]) ||
      "/imagery/" + encodeURIComponent(name) + "/{z}/{x}/{y}.png";

    if (meta.scheme === "tms") {
      urlTemplate = urlTemplate.replace("{y}", "{reverseY}");
    }

    try {
      const templateUrl = new URL(urlTemplate, window.location.origin);
      urlTemplate = templateUrl.pathname + templateUrl.search;
    } catch (_) {
      // Keep relative templates as-is.
    }

    const bounds = parseBounds(meta.bounds);
    minZoom = meta.minzoom != null ? meta.minzoom : 0;
    maxZoom = meta.maxzoom != null ? meta.maxzoom : 18;

    const providerOptions = {
      url: urlTemplate,
      tilingScheme: new Cesium.WebMercatorTilingScheme(),
      minimumLevel: minZoom,
      maximumLevel: maxZoom,
    };

    if (bounds) {
      providerOptions.rectangle = Cesium.Rectangle.fromDegrees(
        bounds.west,
        bounds.south,
        bounds.east,
        bounds.north,
      );
    }

    removeOverlayLayer();
    const provider = new Cesium.UrlTemplateImageryProvider(providerOptions);
    overlayLayer = viewer.imageryLayers.addImageryProvider(provider);
    currentTileset = name;
    updateUrlTileset(name);
    setStatus("Loaded tileset: " + name);

    if (bounds && shouldFly) {
      viewer.camera.flyTo({
        destination: Cesium.Rectangle.fromDegrees(
          bounds.west,
          bounds.south,
          bounds.east,
          bounds.north,
        ),
      });
    }
  }

  function formatZoomRange(minZoom, maxZoom) {
    if (minZoom == null || maxZoom == null) return "—";
    if (minZoom === maxZoom) return String(minZoom);
    return minZoom + "–" + maxZoom;
  }

  function renderTilesetMeta(item) {
    const metaEl = document.createElement("div");
    metaEl.className = "meta";

    const row = document.createElement("div");
    row.className = "meta-row";

    function addTag(label, className) {
      const tag = document.createElement("span");
      tag.className = "meta-tag" + (className ? " " + className : "");
      tag.textContent = label;
      row.appendChild(tag);
    }

    const scheme = item.scheme_label || item.scheme || "—";
    const schemeClass =
      item.scheme === "tms" ? "scheme-tms" : item.scheme === "xyz" ? "scheme-xyz" : "";
    addTag("瓦片: " + scheme, schemeClass);
    addTag("层级: " + formatZoomRange(item.min_zoom, item.max_zoom));
    addTag("坐标系: " + (item.crs || "—"));

    metaEl.appendChild(row);
    return metaEl;
  }

  function renderTilesetList(tilesets) {
    const listEl = document.getElementById("tilesetList");
    listEl.innerHTML = "";

    if (!tilesets.length) {
      listEl.innerHTML =
        '<p class="empty-hint">暂无已发布图层。请先完成任务并发布 tileset。</p>';
      return;
    }

    tilesets.forEach(function (item) {
      const li = document.createElement("li");
      li.className = "tileset-item";
      if (item.name === currentTileset) {
        li.classList.add("active");
      }

      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = item.name;

      const unpublishBtn = document.createElement("button");
      unpublishBtn.type = "button";
      unpublishBtn.className = "tileset-unpublish-btn";
      unpublishBtn.textContent = "下架";
      unpublishBtn.title = "下架";
      unpublishBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        unpublishTileset(item.name);
      });

      const urlEl = document.createElement("div");
      urlEl.className = "url";
      urlEl.textContent = item.imagery_url || item.url_template || "";

      li.appendChild(unpublishBtn);
      li.appendChild(nameEl);
      li.appendChild(urlEl);
      li.appendChild(renderTilesetMeta(item));
      li.addEventListener("click", function () {
        loadTileset(item.name, { flyTo: true })
          .then(function () {
            renderTilesetList(tilesets);
            showToast("已加载图层: " + item.name, "success");
          })
          .catch(function (err) {
            showToast("加载失败: " + err.message, "error");
          });
      });

      listEl.appendChild(li);
    });
  }

  async function unpublishTileset(tilesetName) {
    if (!tilesetName) return;
    if (!window.confirm('确认下架图层 "' + tilesetName + '"？')) {
      return;
    }

    try {
      await apiFetch("/tilesets/" + encodeURIComponent(tilesetName), {
        method: "DELETE",
      });

      if (currentTileset === tilesetName) {
        removeOverlayLayer();
        currentTileset = null;
        updateUrlTileset(null);
        setStatus("图层已下架: " + tilesetName);
      }

      showToast("已下架: " + tilesetName, "success");
      await refreshTilesets();
    } catch (err) {
      showToast("下架失败: " + err.message, "error");
    }
  }

  async function refreshTilesets() {
    const listEl = document.getElementById("tilesetList");
    listEl.innerHTML = '<p class="empty-hint">加载中…</p>';

    try {
      const data = await apiFetch("/tilesets");
      renderTilesetList(data.tilesets || []);
    } catch (err) {
      listEl.innerHTML =
        '<p class="error-text">获取图层列表失败: ' + err.message + "</p>";
    }
  }

  function stopJobTracking() {
    trackingJobId = null;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (jobSocket) {
      jobSocket.close();
      jobSocket = null;
    }
  }

  function isTerminalJobStatus(status) {
    return status === "completed" || status === "failed";
  }

  function jobWebSocketUrl(jobId) {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return (
      protocol +
      "//" +
      window.location.host +
      API_BASE +
      "/jobs/" +
      encodeURIComponent(jobId) +
      "/ws"
    );
  }

  function handleJobQueryError(err) {
    stopJobTracking();
    clearJobProgress();
    const hintEl = document.getElementById("jobQueryHint");
    if (hintEl) {
      hintEl.hidden = false;
      hintEl.className = "error-text";
      hintEl.textContent = "查询失败: " + err.message;
    }
    updatePublishControls(null);
  }

  function onJobProgressUpdate(job, jobId) {
    renderJobDetail(job);

    if (!isTerminalJobStatus(job.status)) {
      return;
    }

    stopJobTracking();
    if (job.status === "completed") {
      showToast("任务已完成: " + jobId, "success");
      refreshTilesets();
      return;
    }

    showToast("任务失败: " + (job.error || jobId), "error");
  }

  function startPollingFallback(jobId) {
    if (pollTimer || trackingJobId !== jobId) {
      return;
    }

    async function tick() {
      try {
        const job = await fetchJob(jobId);
        onJobProgressUpdate(job, jobId);
      } catch (err) {
        handleJobQueryError(err);
      }
    }

    tick();
    pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  }

  function startJobTracking(jobId) {
    stopJobTracking();
    trackingJobId = jobId;
    setJobIdFields(jobId);

    if (typeof WebSocket === "undefined") {
      startPollingFallback(jobId);
      return;
    }

    const socket = new WebSocket(jobWebSocketUrl(jobId));
    jobSocket = socket;
    let connectTimer = null;

    function clearConnectTimer() {
      if (connectTimer) {
        clearTimeout(connectTimer);
        connectTimer = null;
      }
    }

    socket.onopen = function () {
      clearConnectTimer();
    };

    socket.onmessage = function (event) {
      try {
        const job = JSON.parse(event.data);
        onJobProgressUpdate(job, jobId);
      } catch (err) {
        showToast("进度消息解析失败: " + err.message, "error");
      }
    };

    socket.onerror = function () {
      clearConnectTimer();
    };

    socket.onclose = function () {
      clearConnectTimer();
      if (jobSocket === socket) {
        jobSocket = null;
      }
      if (trackingJobId !== jobId) {
        return;
      }

      const terminal =
        lastJobDetail &&
        lastJobDetail.job_id === jobId &&
        isTerminalJobStatus(lastJobDetail.status);
      if (!terminal) {
        startPollingFallback(jobId);
      }
    };

    connectTimer = setTimeout(function () {
      if (
        trackingJobId === jobId &&
        jobSocket === socket &&
        socket.readyState !== WebSocket.OPEN &&
        !pollTimer
      ) {
        socket.close();
        startPollingFallback(jobId);
      }
    }, WS_CONNECT_TIMEOUT_MS);
  }

  function statusBadgeClass(status) {
    return "status-badge " + (status || "queued");
  }

  function clampPercent(value) {
    const num = Number(value);
    if (Number.isNaN(num)) return 0;
    return Math.max(0, Math.min(100, num));
  }

  function formatProgressPercent(value) {
    const percent = clampPercent(value);
    return (Math.round(percent * 10) / 10).toFixed(percent % 1 === 0 ? 0 : 1) + "%";
  }

  function progressPhaseLabel(phase) {
    const labels = {
      queued: "排队中",
      initializing: "初始化",
      gdal_preprocess: "预处理",
      gdal_raster_tile: "切片",
      register_tileset: "注册发布",
      done: "完成",
      failed: "失败",
    };
    return labels[phase] || phase || "—";
  }

  function statusLabel(status) {
    const labels = {
      queued: "排队中",
      running: "运行中",
      preprocessing: "预处理中",
      tiling: "切片中",
      publishing: "发布中",
      completed: "已完成",
      failed: "失败",
    };
    return labels[status] || status || "—";
  }

  function formatElapsed(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return "—";
    const total = Math.max(0, Math.floor(Number(seconds)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
      return hours + "小时 " + minutes + "分 " + secs + "秒";
    }
    if (minutes > 0) {
      return minutes + "分 " + secs + "秒";
    }
    return secs + "秒";
  }

  function clearJobProgress() {
    const progressEl = document.getElementById("jobProgress");
    const fillEl = document.getElementById("jobProgressFill");
    const barEl = document.getElementById("jobProgressBar");
    const hintEl = document.getElementById("jobQueryHint");
    progressEl.hidden = true;
    fillEl.style.width = "0%";
    fillEl.className = "job-progress-fill";
    barEl.setAttribute("aria-valuenow", "0");
    document.getElementById("jobProgressPercent").textContent = "0%";
    document.getElementById("jobProgressLabel").textContent = "进度";
    document.getElementById("jobStatusValue").textContent = "—";
    document.getElementById("jobStatusValue").className = "";
    document.getElementById("jobStageValue").textContent = "—";
    document.getElementById("jobElapsedValue").textContent = "—";
    if (hintEl) {
      hintEl.hidden = false;
      hintEl.className = "empty-hint";
      hintEl.textContent =
        "在「数据接入」提交任务后，在此查看进度；完成后可到「瓦片发布」发布图层。";
    }
  }

  function renderJobProgress(job) {
    const progressEl = document.getElementById("jobProgress");
    const fillEl = document.getElementById("jobProgressFill");
    const barEl = document.getElementById("jobProgressBar");
    const hintEl = document.getElementById("jobQueryHint");
    const progress = job && job.progress ? job.progress : null;

    if (!job) {
      clearJobProgress();
      return;
    }

    let percent = progress ? clampPercent(progress.percent) : 0;
    if (job.status === "completed") percent = 100;
    if (job.status === "queued" && !progress) percent = 0;

    const phase = (progress && progress.phase) || job.stage || job.status;

    progressEl.hidden = false;
    if (hintEl) hintEl.hidden = true;
    fillEl.style.width = percent + "%";
    fillEl.className =
      "job-progress-fill" +
      (job.status === "failed"
        ? " is-failed"
        : job.status === "completed"
          ? " is-completed"
          : "");
    barEl.setAttribute("aria-valuenow", String(Math.round(percent)));
    document.getElementById("jobProgressPercent").textContent =
      formatProgressPercent(percent);
    document.getElementById("jobProgressLabel").textContent = "进度";

    const statusEl = document.getElementById("jobStatusValue");
    statusEl.textContent = statusLabel(job.status);
    statusEl.className = statusBadgeClass(job.status);
    document.getElementById("jobStageValue").textContent =
      progressPhaseLabel(phase);
    document.getElementById("jobElapsedValue").textContent = formatElapsed(
      job.elapsed_seconds
    );
  }

  function updatePublishControls(job) {
    lastJobDetail = job;
    const publishBtn = document.getElementById("publishJobBtn");
    const unpublishBtn = document.getElementById("unpublishJobBtn");

    if (!job) {
      publishBtn.disabled = true;
      unpublishBtn.disabled = true;
      return;
    }

    publishBtn.disabled = !(job.status === "completed" && !job.published);
    unpublishBtn.disabled = !job.published;
  }

  function renderJobDetail(job) {
    renderJobProgress(job);
    updatePublishControls(job);
  }

  async function fetchJob(jobId) {
    return apiFetch("/jobs/" + encodeURIComponent(jobId));
  }

  function startPolling(jobId) {
    startJobTracking(jobId);
  }

  async function lookupJob() {
    const jobId = document.getElementById("jobIdInput").value.trim();
    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }
    startPolling(jobId);
  }

  async function refreshJobOnce(jobId) {
    const job = await fetchJob(jobId);
    renderJobDetail(job);
    return job;
  }

  async function publishJob() {
    const jobId = document.getElementById("publishJobIdInput").value.trim();
    const tilesetName = document.getElementById("tilesetNameInput").value.trim();
    const statusBox = document.getElementById("publishStatus");
    const publishBtn = document.getElementById("publishJobBtn");

    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }

    publishBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">发布中…</p>';

    try {
      const body = tilesetName ? { tileset_name: tilesetName } : {};
      const job = await apiFetch("/jobs/" + encodeURIComponent(jobId) + "/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      setJobIdFields(jobId);
      renderJobDetail(job);
      statusBox.innerHTML =
        '<p class="success-text">已发布 tileset: ' +
        (job.tileset_name || jobId) +
        "</p>";
      showToast("发布成功", "success");
      refreshTilesets();
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">发布失败: ' + err.message + "</p>";
      showToast("发布失败: " + err.message, "error");
      try {
        await refreshJobOnce(jobId);
      } catch (_) {
        updatePublishControls(lastJobDetail);
      }
    }
  }

  async function unpublishJob() {
    const jobId = document.getElementById("publishJobIdInput").value.trim();
    const statusBox = document.getElementById("publishStatus");
    const unpublishBtn = document.getElementById("unpublishJobBtn");

    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }

    unpublishBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">下架中…</p>';

    try {
      const job = await apiFetch("/jobs/" + encodeURIComponent(jobId) + "/publish", {
        method: "DELETE",
      });

      setJobIdFields(jobId);
      renderJobDetail(job);
      statusBox.innerHTML = '<p class="success-text">已下架 tileset</p>';
      showToast("下架成功", "success");
      refreshTilesets();
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">下架失败: ' + err.message + "</p>";
      showToast("下架失败: " + err.message, "error");
      try {
        await refreshJobOnce(jobId);
      } catch (_) {
        updatePublishControls(lastJobDetail);
      }
    }
  }

  function formatFileSize(bytes) {
    if (bytes == null) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  function readOptionalInt(id) {
    const el = document.getElementById(id);
    if (!el) return undefined;
    const raw = el.value.trim();
    if (!raw) return undefined;
    const value = parseInt(raw, 10);
    return Number.isNaN(value) ? undefined : value;
  }

  function collectJobOptions() {
    const preprocess = {
      target_crs: document.getElementById("optTargetCrs").value.trim() || "EPSG:3857",
      build_overviews: document.getElementById("optBuildOverviews").checked,
      add_alpha: document.getElementById("optAddAlpha").checked,
      white_as_transparent: document.getElementById("optWhiteTransparent").checked,
      compress: document.getElementById("optCompress").value,
    };

    const tiling_options = {
      profile: document.getElementById("optProfile").value,
      tile_format: document.getElementById("optTileFormat").value,
      tile_scheme: document.getElementById("optTileScheme").value,
      end_zoom: readOptionalInt("optEndZoom") ?? 0,
      resampling_method: document.getElementById("optResampling").value,
    };

    const startZoom = readOptionalInt("optStartZoom");
    if (startZoom !== undefined) {
      tiling_options.start_zoom = startZoom;
    }

    const publish = {
      auto_publish: document.getElementById("optAutoPublish").checked,
    };

    const tilesetName = document.getElementById("optTilesetName").value.trim();
    if (tilesetName) {
      publish.tileset_name = tilesetName;
    }

    return { preprocess: preprocess, tiling_options: tiling_options, publish: publish };
  }

  function afterJobSubmitted(jobId, message) {
    document.getElementById("submitStatus").innerHTML =
      '<p class="success-text">' + message + "，任务 ID: " + jobId + "</p>";
    showToast(message, "success");
    setJobIdFields(jobId);
    openPanel("progress");
    startPolling(jobId);
  }

  async function submitUpload() {
    const fileInput = document.getElementById("uploadFile");
    const submitBtn = document.getElementById("uploadSubmitBtn");
    const statusBox = document.getElementById("submitStatus");

    if (!fileInput.files || !fileInput.files[0]) {
      showToast("请选择 GeoTIFF 文件", "error");
      return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const opts = collectJobOptions();
    formData.append("preprocess_json", JSON.stringify(opts.preprocess));
    formData.append("tiling_options_json", JSON.stringify(opts.tiling_options));
    formData.append("publish_json", JSON.stringify(opts.publish));

    submitBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">上传中，请稍候…</p>';

    try {
      const result = await apiFetch("/jobs/upload", {
        method: "POST",
        body: formData,
      });
      afterJobSubmitted(result.job_id, "上传成功，已开始处理");
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">上传失败: ' + err.message + "</p>";
      showToast("上传失败: " + err.message, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  async function submitWorkspaceJob() {
    const submitBtn = document.getElementById("workspaceSubmitBtn");
    const statusBox = document.getElementById("submitStatus");

    if (!selectedWorkspaceFile) {
      showToast("请选择服务器上的 GeoTIFF 文件", "error");
      return;
    }

    submitBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">提交任务中…</p>';

    try {
      const opts = collectJobOptions();
      const result = await apiFetch("/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_path: selectedWorkspaceFile.absolute_path,
          preprocess: opts.preprocess,
          tiling_options: opts.tiling_options,
          publish: opts.publish,
        }),
      });
      afterJobSubmitted(result.job_id, "任务已提交");
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">提交失败: ' + err.message + "</p>";
      showToast("提交失败: " + err.message, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  function renderWorkspaceBreadcrumb(listing) {
    const breadcrumbEl = document.getElementById("workspaceBreadcrumb");
    breadcrumbEl.innerHTML = "";

    const rootBtn = document.createElement("button");
    rootBtn.type = "button";
    rootBtn.textContent = "工作区";
    rootBtn.addEventListener("click", function () {
      loadWorkspace("");
    });
    breadcrumbEl.appendChild(rootBtn);

    if (!listing.relative_path) return;

    const parts = listing.relative_path.split("/");
    parts.forEach(function (_part, index) {
      const segmentPath = parts.slice(0, index + 1).join("/");
      const sep = document.createElement("span");
      sep.textContent = "/";
      sep.style.color = "#777";
      breadcrumbEl.appendChild(sep);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = parts[index];
      btn.addEventListener("click", function () {
        loadWorkspace(segmentPath);
      });
      breadcrumbEl.appendChild(btn);
    });
  }

  function renderWorkspaceList(listing) {
    const listEl = document.getElementById("workspaceList");
    listEl.innerHTML = "";
    selectedWorkspaceFile = null;
    document.getElementById("workspaceSubmitBtn").disabled = true;

    document.getElementById("workspacePath").textContent =
      "当前目录: " + listing.absolute_path;

    renderWorkspaceBreadcrumb(listing);

    if (!listing.entries.length) {
      listEl.innerHTML = '<li class="empty-hint">此目录下没有可浏览的内容</li>';
      return;
    }

    listing.entries.forEach(function (entry) {
      const li = document.createElement("li");
      li.className = "workspace-item";

      const label = document.createElement("div");
      label.className = "label";
      label.textContent =
        (entry.entry_type === "directory" ? "📁 " : "📄 ") + entry.name;

      const meta = document.createElement("div");
      meta.className = "meta";
      if (entry.entry_type === "directory") {
        meta.textContent = "目录";
      } else if (entry.selectable) {
        meta.textContent = formatFileSize(entry.size_bytes);
      } else {
        meta.textContent = "不可选";
        li.classList.add("disabled");
      }

      li.appendChild(label);
      li.appendChild(meta);

      if (entry.entry_type === "directory") {
        li.addEventListener("click", function () {
          loadWorkspace(entry.relative_path);
        });
      } else if (entry.selectable) {
        li.addEventListener("click", function () {
          listEl.querySelectorAll(".workspace-item.selected").forEach(function (node) {
            node.classList.remove("selected");
          });
          li.classList.add("selected");
          selectedWorkspaceFile = entry;
          document.getElementById("workspaceSubmitBtn").disabled = false;
        });
      }

      listEl.appendChild(li);
    });
  }

  async function loadWorkspace(relativePath) {
    const listEl = document.getElementById("workspaceList");
    listEl.innerHTML = '<li class="empty-hint">加载中…</li>';
    workspaceRelativePath = relativePath;

    try {
      const query = relativePath
        ? "?path=" + encodeURIComponent(relativePath)
        : "";
      const listing = await apiFetch("/workspace" + query);
      renderWorkspaceList(listing);
    } catch (err) {
      listEl.innerHTML =
        '<li class="error-text">加载失败: ' + err.message + "</li>";
    }
  }

  function setSubmitTab(tabName) {
    activeSubmitTab = tabName;
    document.querySelectorAll(".panel-subtab[data-submit-tab]").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.submitTab === tabName);
    });
    document.getElementById("uploadTabPanel").classList.toggle(
      "active",
      tabName === "upload",
    );
    document.getElementById("workspaceTabPanel").classList.toggle(
      "active",
      tabName === "workspace",
    );

    if (tabName === "workspace") {
      loadWorkspace(workspaceRelativePath);
    }
  }

  function preparePublishPanel() {
    const queryJobId = document.getElementById("jobIdInput").value.trim();
    const publishJobIdInput = document.getElementById("publishJobIdInput");
    const publishJobId = publishJobIdInput.value.trim();
    if (queryJobId && !publishJobId) {
      publishJobIdInput.value = queryJobId;
    }
    const resolvedId = publishJobIdInput.value.trim();
    if (lastJobDetail && lastJobDetail.job_id === resolvedId) {
      updatePublishControls(lastJobDetail);
    } else if (resolvedId) {
      refreshJobOnce(resolvedId).catch(function () {
        updatePublishControls(null);
      });
    } else {
      updatePublishControls(null);
    }
  }

  function setNavActive(panelName) {
    document.querySelectorAll(".side-nav-item[data-panel]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.panel === panelName);
    });
  }

  function openPanel(name) {
    Object.keys(panels).forEach(function (key) {
      panels[key].classList.toggle("open", key === name);
    });
    overlay.classList.add("open");
    activePanel = name;
    setNavActive(name);

    if (name === "layers") {
      refreshTilesets();
    }

    if (name === "ingest" && activeSubmitTab === "workspace") {
      loadWorkspace(workspaceRelativePath);
    }

    if (name === "publish") {
      preparePublishPanel();
    }
  }

  function closePanel() {
    Object.keys(panels).forEach(function (key) {
      panels[key].classList.remove("open");
    });
    overlay.classList.remove("open");
    activePanel = null;
    setNavActive(null);
  }

  function bindUi() {
    document.querySelectorAll(".side-nav-item[data-panel]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const panelName = btn.dataset.panel;
        if (activePanel === panelName) {
          closePanel();
        } else {
          openPanel(panelName);
        }
      });
    });

    document.querySelectorAll(".panel-subtab[data-submit-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        setSubmitTab(tab.dataset.submitTab);
      });
    });

    document.getElementById("refreshTilesetsBtn").addEventListener("click", function () {
      refreshTilesets();
    });

    document.getElementById("lookupJobBtn").addEventListener("click", function () {
      lookupJob();
    });

    document.getElementById("uploadSubmitBtn").addEventListener("click", function () {
      submitUpload();
    });

    document.getElementById("workspaceSubmitBtn").addEventListener("click", function () {
      submitWorkspaceJob();
    });

    document.getElementById("publishJobBtn").addEventListener("click", function () {
      publishJob();
    });

    document.getElementById("unpublishJobBtn").addEventListener("click", function () {
      unpublishJob();
    });

    overlay.addEventListener("click", closePanel);

    document.querySelectorAll(".panel-close").forEach(function (btn) {
      btn.addEventListener("click", closePanel);
    });

    document.getElementById("jobIdInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        lookupJob();
      }
    });

    document.getElementById("publishJobIdInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        publishJob();
      }
    });
  }

  async function initViewer() {
    let baseProvider;
    if (Cesium.ArcGisMapServerImageryProvider.fromUrl) {
      baseProvider = await Cesium.ArcGisMapServerImageryProvider.fromUrl(
        "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer",
      );
    } else {
      baseProvider = new Cesium.UrlTemplateImageryProvider({
        url:
          "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        maximumLevel: 19,
      });
    }

    viewer = new Cesium.Viewer("cesiumContainer", {
      baseLayer: new Cesium.ImageryLayer(baseProvider),
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      infoBox: false,
      selectionIndicator: false,
    });
    viewer.scene.globe.enableLighting = false;

    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction(function (movement) {
      const cartesian = viewer.camera.pickEllipsoid(
        movement.endPosition,
        viewer.scene.globe.ellipsoid,
      );
      if (!cartesian) {
        currentLat = "—";
        currentLng = "—";
      } else {
        const c = Cesium.Cartographic.fromCartesian(cartesian);
        currentLat = formatCoord(Cesium.Math.toDegrees(c.latitude));
        currentLng = formatCoord(Cesium.Math.toDegrees(c.longitude));
      }
      renderCoords();
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

    viewer.scene.postRender.addEventListener(function () {
      const zoom = getZoomLevel();
      const nextZoom = zoom === null ? "—" : String(zoom);
      if (nextZoom !== currentZoom) {
        currentZoom = nextZoom;
        renderCoords();
      }
    });
  }

  async function boot() {
    bindUi();
    await initViewer();

    const params = new URLSearchParams(window.location.search);
    const jobId = params.get("job");

    setStatus("请通过「数据接入」提交影像，「图层管理」选择预览");

    if (jobId) {
      openPanel("progress");
      setJobIdFields(jobId);
      startPolling(jobId);
    }
  }

  boot().catch(function (err) {
    setStatus("Preview init failed: " + err.message);
    showToast("初始化失败: " + err.message, "error");
  });
})();
