/* Data manager: imports, sync, schedule and dataset downloads. */
(function () {
  "use strict";
  const { api, fmt, toast, debounce, clockMs } = window.AQ;
  const $ = (id) => document.getElementById(id);
  const ICON = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    cross: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    bang: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 6v8M12 18v.01"/></svg>',
    csv: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M6 2.5h8.5L19 7v14.5H6z"/><path d="M14.5 2.5V7H19"/><text x="12.5" y="17.5" font-size="5.2" text-anchor="middle" fill="currentColor" stroke="none" font-family="sans-serif" font-weight="700">CSV</text></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>',
  };
  const table = { page: 1, q: "", region: $("region").value, period: "", selected: new Set() };
  let polling = null;

  // ---------------- summary
  function statusVisual(run) {
    if (!run) return { cls: "", icon: ICON.check, label: "No imports" };
    if (run.source === "Deletion") return { cls: "warn", icon: ICON.bang, label: "Deleted" };
    return {
      running: { cls: "run", icon: ICON.bang, label: "Running" },
      complete: { cls: "", icon: ICON.check, label: "Complete" },
      partial: { cls: "warn", icon: ICON.bang, label: "Warnings" },
      failed: { cls: "fail", icon: ICON.cross, label: "Failed" },
    }[run.status];
  }

  function describeRun(run) {
    if (run.status === "running") return `Importing ${run.filename}…`;
    if (run.source === "Deletion") return run.message.split("\n").join(" ");
    const parts = [`${fmt.int(run.rows_stored)} new readings`];
    if (run.rows_duplicate) parts.push(`${fmt.int(run.rows_duplicate)} already stored`);
    if (run.rows_rejected) parts.push(`${fmt.int(run.rows_rejected)} rejected`);
    if (run.stations.length) parts.push(`${run.stations.length} station${run.stations.length === 1 ? "" : "s"}`);
    const head = run.status === "failed" ? run.message : parts.join(" · ");
    const extra = run.status === "partial" && run.message ? ` — ${run.message.split("\n")[0]}` : "";
    return `${run.filename}: ${head}${extra}`;
  }

  async function loadSummary() {
    const s = await api("/api/summary/");
    $("c-stations").textContent = fmt.int(s.stations);
    $("c-readings").textContent = fmt.int(s.stored_readings);
    const run = s.last_import;
    const v = statusVisual(run);
    $("c-last").textContent = run ? fmt.time(clockMs(run.started_at)) : "–";
    $("c-last").title = run ? new Date(clockMs(run.started_at)).toUTCString().replace(" GMT", "") : "";
    $("c-state").textContent = v.label;
    $("c-state-icon").className = `check ${v.cls}`;
    $("c-state-icon").innerHTML = ICON[v.icon === ICON.check ? "check" : v.icon === ICON.cross ? "cross" : "bang"];
    if (run) {
      $("n-icon").className = `check ${v.cls}`;
      $("n-icon").innerHTML = $("c-state-icon").innerHTML;
      $("n-title").textContent = run.source === "Deletion"
        ? "Data deleted"
        : { running: "Import running", complete: "Latest import complete", partial: "Latest import complete with warnings", failed: "Latest import failed" }[run.status];
      $("n-text").textContent = describeRun(run);
    }
    renderSchedule(s.schedule);
    return s;
  }

  // ---------------- import
  async function upload(file) {
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) return toast("Choose a .csv file.");
    const body = new FormData();
    body.append("file", file);
    $("n-icon").className = "check run";
    $("n-title").textContent = "Importing";
    $("n-text").textContent = `${file.name} (${fmt.num(file.size / 1048576, 1)} MB)…`;
    try {
      const run = await api("/api/import/", { method: "POST", body });
      toast(run.status === "partial" ? `Imported with warnings:\n${run.message}` : `Imported ${fmt.int(run.rows_stored)} new readings.`);
    } catch (err) {
      toast(err.data?.message || err.message, 7000);
    }
    await Promise.all([loadSummary(), loadDatasets(), loadSensors()]);
  }
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
  $("file").addEventListener("change", (e) => { upload(e.target.files[0]); e.target.value = ""; });
  $("import-btn").addEventListener("click", (event) => {
    if (event.currentTarget.dataset.loginUrl) { location.href = event.currentTarget.dataset.loginUrl; return; }
    $("file").click();
  });

  // ---------------- sync
  $("sync").addEventListener("click", async (event) => {
    if (event.currentTarget.dataset.loginUrl) { location.href = event.currentTarget.dataset.loginUrl; return; }
    const res = await api("/api/sync/", { method: "POST" }).catch((err) => ({ message: err.message }));
    toast(res.message);
    $("sync").disabled = true;
    clearInterval(polling);
    const started = Date.now();
    polling = setInterval(async () => {
      const s = await loadSummary();
      if ((s.last_import && s.last_import.status !== "running") || Date.now() - started > 15 * 60000) {
        clearInterval(polling);
        $("sync").disabled = false;
        loadDatasets();
        loadSensors();
      }
    }, 3000);
  });

  // ---------------- schedule
  function renderSchedule(sch) {
    $("schedule-toggle").setAttribute("aria-checked", String(sch.enabled));
    $("interval").value = String(sch.interval_minutes);
    $("interval").disabled = !sch.enabled;
    $("next-run").textContent = sch.enabled && sch.next_run ? `Next run ${fmt.time(clockMs(sch.next_run))}` : "Paused";
  }
  $("schedule-toggle").addEventListener("click", async (e) => {
    const on = e.currentTarget.getAttribute("aria-checked") !== "true";
    renderSchedule(await api("/api/schedule/", { method: "POST", json: { enabled: on } }));
  });
  $("interval").addEventListener("change", async (e) => {
    renderSchedule(await api("/api/schedule/", { method: "POST", json: { interval_minutes: +e.target.value } }));
  });

  // ---------------- sensors
  const SENSOR_STATE = {
    ready: { cls: "", text: "Ready" },
    waiting: { cls: "run", text: "Downloading…" },
    "new": { cls: "run", text: "Queued" },
    failed: { cls: "fail", text: "Failed" },
    paused: { cls: "warn", text: "Paused" },
  };

  async function loadSensors() {
    const data = await api("/api/devices/");
    const body = $("sensor-rows");
    body.innerHTML = "";
    if (!data.results.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No sensors registered yet. '
        + '<a href="#add-sensor">Add a sensor ID</a> below to start downloading its recordings.</td></tr>';
      return data;
    }
    for (const d of data.results) {
      const state = SENSOR_STATE[d.status] || SENSOR_STATE.ready;
      const period = `${fmt.day(clockMs(d.download_from), true)} – `
        + (d.download_until ? fmt.day(clockMs(d.download_until), true) : "now");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong class="did"></strong><div class="small muted lab"></div></td>
        <td class="st"></td><td>${period}</td>
        <td class="num">${d.readings === null ? "–" : fmt.int(d.readings)}</td>
        <td><span class="status-dot ${state.cls}"></span><span class="stt"></span></td>
        <td>${d.last_success ? fmt.dateTime(clockMs(d.last_success)) : "Never"}</td>`;
      tr.querySelector(".did").textContent = d.device_id;
      tr.querySelector(".lab").textContent = d.label || (d.sessions ? `Sessions ${d.sessions}` : "");
      tr.querySelector(".st").textContent = d.station ? `${d.station_name} · ${d.station}` : "Automatic";
      tr.querySelector(".stt").textContent = state.text;
      if (d.last_error) tr.querySelector(".stt").title = d.last_error;
      body.appendChild(tr);
    }
    return data;
  }

  // ---------------- datasets
  function periodLabel(a, b) { return fmt.range(clockMs(a), clockMs(b) + 86400000); }

  async function loadDatasets() {
    const p = new URLSearchParams({ page: table.page, page_size: 8 });
    if (table.q) p.set("q", table.q);
    if (table.region) p.set("region", table.region);
    if (table.period) p.set("period", table.period);
    const data = await api(`/api/datasets/?${p}`);
    const periodSel = $("period");
    if (periodSel.options.length - 1 !== data.periods.length) {
      const keep = periodSel.value;
      periodSel.length = 1;
      data.periods.forEach((d) => periodSel.add(new Option(`Week of ${fmt.day(clockMs(d), true)}`, d)));
      periodSel.value = keep;
    }
    const tbody = $("rows");
    tbody.innerHTML = "";
    if (!data.results.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">${data.total === 0 && !table.q && !table.region && !table.period
        ? "No datasets yet. Import a CSV file above to create weekly datasets for each station."
        : "No datasets match these filters."}</td></tr>`;
    }
    for (const d of data.results) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><input type="checkbox" aria-label="Select"></td>
        <td><div class="file-cell">${ICON.csv}<span class="fn"></span></div></td>
        <td class="st"></td><td>${periodLabel(d.period_start, d.period_end)}</td>
        <td class="num">${fmt.int(d.readings)}</td>
        <td><span class="status-dot"></span>Ready</td>
        <td style="text-align:center"><a class="icon-btn" href="${d.url}" aria-label="Download">${ICON.download}</a></td>`;
      tr.querySelector(".fn").textContent = d.filename;
      tr.querySelector(".st").textContent = d.station;
      tr.querySelector(".file-cell").title = d.measurements.join(", ");
      const cb = tr.querySelector("input");
      cb.checked = table.selected.has(d.id);
      cb.setAttribute("aria-label", `Select ${d.filename}`);
      cb.addEventListener("change", () => { cb.checked ? table.selected.add(d.id) : table.selected.delete(d.id); syncSelection(); });
      tbody.appendChild(tr);
    }
    const from = data.total ? (data.page - 1) * 8 + 1 : 0;
    $("showing").textContent = `Showing ${data.results.length ? `${from}–${from + data.results.length - 1}` : 0} of ${fmt.int(data.total)} datasets`;
    renderPager(data.page, data.pages);
    syncSelection();
  }

  function renderPager(page, pages) {
    const nav = $("pager");
    nav.innerHTML = "";
    const btn = (label, target, opts = {}) => {
      const b = document.createElement("button");
      b.type = "button"; b.innerHTML = label; b.disabled = !!opts.disabled;
      if (opts.current) b.setAttribute("aria-current", "page");
      b.addEventListener("click", () => { table.page = target; loadDatasets(); });
      nav.appendChild(b);
    };
    btn("‹&nbsp; Previous", page - 1, { disabled: page <= 1 });
    const nums = new Set([1, pages, page - 1, page, page + 1].filter((n) => n >= 1 && n <= pages));
    let last = 0;
    [...nums].sort((a, b) => a - b).forEach((n) => {
      if (n - last > 1) { const s = document.createElement("span"); s.textContent = "…"; nav.appendChild(s); }
      btn(String(n), n, { current: n === page });
      last = n;
    });
    btn("Next &nbsp;›", page + 1, { disabled: page >= pages });
  }

  function syncSelection() {
    const boxes = [...$("rows").querySelectorAll("input[type=checkbox]")];
    $("select-all").checked = boxes.length > 0 && boxes.every((b) => b.checked);
    $("download-selected").disabled = table.selected.size === 0;
    $("download-selected").lastChild.textContent = table.selected.size ? `Download selected (${table.selected.size})` : "Download selected";
    $("delete-selected").disabled = table.selected.size === 0;
    $("delete-selected").lastChild.textContent = table.selected.size ? `Delete selected (${table.selected.size})` : "Delete selected";
  }
  $("select-all").addEventListener("change", (e) => {
    $("rows").querySelectorAll("input[type=checkbox]").forEach((b) => { if (b.checked !== e.target.checked) { b.checked = e.target.checked; b.dispatchEvent(new Event("change")); } });
  });
  $("download-selected").addEventListener("click", () => { location.href = `/datasets/download/?ids=${[...table.selected].join(",")}`; });
  $("delete-selected").addEventListener("click", async () => {
    if ($("delete-selected").dataset.loginUrl) { location.href = $("delete-selected").dataset.loginUrl; return; }
    const count = table.selected.size;
    if (!count || !window.confirm(`Permanently delete ${count} selected dataset${count === 1 ? "" : "s"}?\n\nThe matching readings will also be removed from the station data. This cannot be undone.`)) return;
    let result;
    try {
      result = await api("/api/datasets/delete/", { method: "POST", json: { ids: [...table.selected] } });
    } catch (err) {
      table.selected.clear();
      toast(err.message, 7000);
      await Promise.all([loadSummary(), loadDatasets(), loadSensors()]);
      return;
    }
    table.selected.clear();
    table.page = 1;
    toast([result.detail, ...(result.notes || [])].join(" "), 7000);
    // The summary counters and the map both change when readings go, so refresh both.
    await Promise.all([loadSummary(), loadDatasets(), loadSensors()]);
  });
  // ---------------- storage not listed in the table
  const MB = (bytes) => `${fmt.num(bytes / 1e6, 1)} MB`;

  function renderOrphans(survey) {
    const box = $("orphans");
    box.innerHTML = "";
    if (!survey.total) {
      box.textContent = "Nothing left over: everything stored is listed above.";
      return;
    }
    const list = document.createElement("ul");
    list.style.margin = "0";
    for (const o of [...survey.stations, ...survey.files]) {
      const li = document.createElement("li");
      const what = o.readings ? `${fmt.int(o.readings)} readings, ${MB(o.size)}` : MB(o.size);
      li.textContent = `${o.label} — ${what}. ${o.detail}`;
      if (o.destroys_readings) li.style.color = "var(--danger, #b42318)";
      list.appendChild(li);
    }
    box.appendChild(list);
  }

  $("purge-orphans").addEventListener("click", async (event) => {
    if (event.currentTarget.dataset.loginUrl) { location.href = event.currentTarget.dataset.loginUrl; return; }
    const survey = await api("/api/orphans/").catch((err) => { toast(err.message, 6000); return null; });
    if (!survey) return;
    renderOrphans(survey);
    if (!survey.total) return toast("Nothing to remove: everything stored is listed above.");

    // Stations still holding readings are called out separately: removing one destroys data.
    const lines = [`Permanently remove ${survey.total} item${survey.total === 1 ? "" : "s"} (${MB(survey.size)}) that the Data manager does not list?`];
    if (survey.stations.length) {
      lines.push(`This includes ${survey.stations.length} station${survey.stations.length === 1 ? "" : "s"} still holding ${fmt.int(survey.readings)} readings:`);
      lines.push(survey.stations.map((o) => `  • ${o.label} (${fmt.int(o.readings)} readings)`).join("\n"));
      lines.push("\nThose readings are deleted from the database and from disk.");
    }
    lines.push("\nThis cannot be undone.");
    if (!window.confirm(lines.join("\n"))) return;

    let result;
    try {
      result = await api("/api/orphans/purge/", { method: "POST", json: { include_stations: true } });
    } catch (err) {
      toast(err.message, 7000);
      return;
    }
    toast([result.detail, ...(result.notes || [])].join(" "), 7000);
    renderOrphans(await api("/api/orphans/"));
    await Promise.all([loadSummary(), loadDatasets(), loadSensors()]);
  });

  // ---------------- readings recorded outside the region
  function renderOutsideRegion(survey) {
    const box = $("outside-region");
    box.innerHTML = "";
    if (!survey.enabled) {
      box.textContent = "Readings are not restricted to a region, so none of them count as outside one.";
      return;
    }
    if (!survey.total) {
      box.textContent = `Nothing to remove: everything stored was recorded inside ${survey.region}.`;
      return;
    }
    const list = document.createElement("ul");
    list.style.margin = "0";
    for (const station of survey.stations) {
      const li = document.createElement("li");
      li.textContent = `${station.name} · ${station.code} — ${fmt.int(station.readings)} readings taken outside ${survey.region}.`;
      list.appendChild(li);
    }
    for (const station of survey.misplaced) {
      const li = document.createElement("li");
      li.textContent = `${station.name} · ${station.code} — its map marker was placed outside ${survey.region}.`;
      list.appendChild(li);
    }
    box.appendChild(list);
  }

  $("purge-outside-region").addEventListener("click", async (event) => {
    if (event.currentTarget.dataset.loginUrl) { location.href = event.currentTarget.dataset.loginUrl; return; }
    const survey = await api("/api/outside-region/").catch((err) => { toast(err.message, 6000); return null; });
    if (!survey) return;
    renderOutsideRegion(survey);
    if (!survey.enabled) return toast("Readings are not restricted to a region.");
    if (!survey.total) return toast(`Nothing to remove: everything stored was recorded inside ${survey.region}.`);

    // The count is the whole warning: a reading removed here cannot be brought back.
    const lines = [`Permanently remove ${fmt.int(survey.readings)} reading${survey.readings === 1 ? "" : "s"} taken outside ${survey.region}?`];
    if (survey.stations.length) {
      lines.push("", ...survey.stations.map((s) => `  • ${s.name} (${fmt.int(s.readings)} readings)`));
      lines.push("", "A station keeps the readings taken inside the region and its marker moves to match. One with nothing left inside is removed.");
    }
    if (survey.misplaced.length) {
      lines.push("", `${survey.misplaced.length} station marker${survey.misplaced.length === 1 ? "" : "s"} placed by hand outside ${survey.region} will be cleared.`);
    }
    lines.push("", "This cannot be undone.");
    if (!window.confirm(lines.join("\n"))) return;

    let result;
    try {
      result = await api("/api/outside-region/purge/", { method: "POST", json: {} });
    } catch (err) {
      toast(err.message, 7000);
      return;
    }
    toast([result.detail, ...(result.notes || [])].join(" "), 7000);
    renderOutsideRegion(await api("/api/outside-region/"));
    await Promise.all([loadSummary(), loadDatasets(), loadSensors()]);
  });

  $("q").addEventListener("input", debounce((e) => { table.q = e.target.value.trim(); table.page = 1; loadDatasets(); }));
  $("region").addEventListener("change", (e) => { table.region = e.target.value; table.page = 1; loadDatasets(); });
  $("period").addEventListener("change", (e) => { table.period = e.target.value; table.page = 1; loadDatasets(); });

  loadSummary().catch((err) => toast(err.message));
  loadDatasets().catch((err) => toast(err.message));
  loadSensors().catch((err) => toast(err.message));

  // A first download runs in the background; keep the sensor list moving while it does.
  setInterval(() => {
    if (document.visibilityState === "visible") loadSensors().catch(() => {});
  }, 20000);
})();
