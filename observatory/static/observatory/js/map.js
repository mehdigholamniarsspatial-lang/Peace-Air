/* Map explorer: one marker per station, popups, side panel charts, time window. */
(function () {
  "use strict";
  const { api, fmt, DualRange, clockMs, isoDay, DAY, debounce, charts } = window.AQ;
  const $ = (id) => document.getElementById(id);
  const HOUR = 3600000;

  const AGG_LABELS = { auto: "Automatic", raw: "Readings", "10min": "10-minute mean", hourly: "Hourly mean",
                       "6h": "6-hour mean", daily: "Daily mean" };
  const params = new URLSearchParams(location.search);
  const state = {
    measurement: "PM2.5", ci: 95, selected: JSON.parse($("initial-station").textContent) || null,
    aggregation: AGG_LABELS[params.get("aggregation")] ? params.get("aggregation") : "auto",
    start: null, end: null, // ms; end exclusive — drives the map and the series alike
    features: [], unplaced: [], markers: new Map(), extent: null, labels: {},
  };
  const seriesWindow = () => [state.start, state.end];
  const label = () => state.labels[state.measurement] || state.measurement;

  // ---------------- map
  const mapEl = $("map");
  const map = L.map(mapEl, { zoomControl: false, attributionControl: true }).setView([53.4, -8.0], 7);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.control.scale({ position: "bottomleft", imperial: false, maxWidth: 140 }).addTo(map);
  L.tileLayer(mapEl.dataset.tiles, { attribution: mapEl.dataset.attribution, subdomains: "abcd", maxZoom: 19 }).addTo(map);
  const layer = L.featureGroup().addTo(map);

  const styleFor = (selected) => selected
    ? { radius: 11, color: "#ffffff", weight: 3, fillColor: charts.colors.teal, fillOpacity: 1 }
    : { radius: 7, color: "#ffffff", weight: 2, fillColor: charts.colors.violet, fillOpacity: 0.95 };

  function popupHtml(p) {
    const s = p.snapshot || {};
    const unit = fmt.unit(p.unit);
    const div = document.createElement("div");
    div.innerHTML = `<div class="pop-title"></div>
      <div class="pop-label">Latest ${label()}</div>
      <div class="pop-value">${fmt.num(s.latest)} <small>${unit}</small></div>
      <div class="pop-meta">${s.latest_time ? fmt.dateTime(clockMs(s.latest_time)) + " · " : ""}${fmt.int(s.count)} readings in window</div>`;
    div.querySelector(".pop-title").textContent = `${p.name} · ${p.code}`;
    return div;
  }

  function renderMarkers() {
    const reopen = state.markers.get(state.selected)?.isPopupOpen();
    layer.clearLayers();
    state.markers.clear();
    for (const f of state.features) {
      const p = f.properties;
      const [lon, lat] = f.geometry.coordinates;
      const marker = L.circleMarker([lat, lon], styleFor(p.code === state.selected));
      marker.bindPopup(() => popupHtml(p), { offset: [0, -6], closeButton: true });
      marker.bindTooltip(p.name, { permanent: p.code === state.selected, direction: "right", offset: [12, 0], className: "station-label" });
      marker.on("click", () => select(p.code, { pan: false }));
      marker.on("keypress", (e) => e.originalEvent.key === "Enter" && select(p.code));
      marker.addTo(layer);
      state.markers.set(p.code, marker);
    }
    if (reopen) state.markers.get(state.selected)?.openPopup();
    $("count-chip").textContent = `${state.features.length} station${state.features.length === 1 ? "" : "s"}`;
    const box = $("unplaced");
    if (state.unplaced.length) {
      box.hidden = false;
      box.innerHTML = `<strong>${state.unplaced.length} without coordinates</strong><br><span class="muted"></span> <a href="/data/#stations">Set location</a>`;
      box.querySelector(".muted").textContent = state.unplaced.map((u) => u.name).join(", ") + ".";
    } else box.hidden = true;
  }

  function refreshMarkerStyles() {
    for (const [code, m] of state.markers) {
      const on = code === state.selected;
      m.setStyle(styleFor(on));
      m.setRadius(styleFor(on).radius);
      const tip = m.getTooltip();
      m.unbindTooltip();
      m.bindTooltip(tip.getContent(), { permanent: on, direction: "right", offset: [12, 0], className: "station-label" });
      if (on) m.bringToFront();
    }
  }

  // ---------------- data
  function windowParams() {
    const p = new URLSearchParams({ measurement: state.measurement });
    if (state.start) p.set("start", isoDay(state.start));
    if (state.end) p.set("end", isoDay(state.end - DAY));
    return p;
  }

  async function loadStations(fit = false) {
    const p = windowParams();
    const data = await api(`/api/stations/?${p}`);
    state.features = data.features;
    state.unplaced = data.unplaced;
    renderMarkers();
    if (fit && state.features.length) map.invalidateSize(), map.fitBounds(layer.getBounds().pad(0.12), { maxZoom: 11 });
    const codes = [...state.features, ...state.unplaced.map((u) => ({ properties: u }))].map((f) => f.properties);
    $("station-list").innerHTML = "";
    for (const s of codes) {
      const o = document.createElement("option");
      o.value = `${s.name} · ${s.code}`;
      $("station-list").appendChild(o);
    }
    return codes;
  }

  const seriesChart = charts.series($("series"));
  const histChart = charts.histogram($("hist"));

  /* A station can disappear while this page is open (deleted in the data manager), so
     say so plainly instead of leaving the previous station's numbers up. */
  function showRemoved(code) {
    state.markers.delete(code);
    $("st-title").textContent = `${code} — deleted`;
    $("st-sub").textContent = "This station and all of its readings have been deleted.";
    $("m-latest").innerHTML = "–";
    $("m-mean").innerHTML = "–";
    $("m-count").textContent = "0";
    $("median-label").textContent = "Median";
    charts.setSeries(seriesChart, { t: [], mean: [], upper: [], lower: [], has_ci: false }, { unit: "" });
    charts.setHistogram(histChart, {}, { unit: "" });
    $("series-empty").hidden = false;
    $("series-empty").textContent = "This station has been deleted. Its readings and datasets are no longer stored.";
    $("hist-empty").hidden = false;
    $("hist-empty").textContent = "Deleted";
    $("export").setAttribute("aria-disabled", "true");
    $("export").removeAttribute("href");
    clearStats();
  }

  async function loadPanel() {
    const code = state.selected;
    const all = [...state.features.map((f) => f.properties), ...state.unplaced];
    const st = all.find((s) => s.code === code);
    if (!st) return showRemoved(code);
    $("st-title").textContent = `${st.name} · ${st.code}`;
    $("st-sub").textContent = st.latitude != null ? `Selected station · ${st.location_source}` : "Selected station · no coordinates yet";
    $("series-title").textContent = `${label()} over time`;
    const [from, to] = seriesWindow();
    const span = (to || 0) - (from || 0);
    // "Automatic" keeps the old behaviour: 10-minute means for a short window, hourly beyond.
    const aggregation = state.aggregation === "auto" ? (span > 3 * DAY ? "hourly" : "10min") : state.aggregation;
    const p = new URLSearchParams({ measurement: state.measurement });
    if (from) p.set("start", isoDay(from));
    if (to) p.set("end", isoDay(to - DAY));
    p.set("aggregation", aggregation);
    p.set("ci", state.ci);
    $("agg-label").textContent = AGG_LABELS[aggregation] || AGG_LABELS[state.aggregation];
    $("ci-label").textContent = `${state.ci}% CI`;
    $("ci-legend").hidden = aggregation === "raw";
    const exportUrl = `/api/stations/${encodeURIComponent(code)}/export.csv?${p}`;
    $("export").href = exportUrl;
    $("export").removeAttribute("aria-disabled");
    try {
      const data = await api(`/api/stations/${encodeURIComponent(code)}/analysis/?${p}`);
      const unit = fmt.unit(data.unit);
      const s = data.stats;
      $("m-latest-label").textContent = `Latest ${label()}`;
      $("m-mean-label").textContent = `Mean ${label()}`;
      $("m-latest").innerHTML = s.count ? `${fmt.num(s.latest)} <small>${unit}</small>` : "–";
      $("m-mean").innerHTML = s.count ? `${fmt.num(s.mean)} <small>${unit}</small>` : "–";
      $("m-count").textContent = fmt.int(s.count);
      $("series-empty").hidden = s.count > 0;
      $("series-empty").textContent = s.count ? "" : `No ${label()} readings for this station in the selected window.`;
      charts.setSeries(seriesChart, data.series, {
        unit, yTitle: `${label()} (${unit})`, ciLabel: `${state.ci}% CI`,
        range: [from, to], measurement: state.measurement,
      });
      $("hist-empty").hidden = s.count > 0;
      $("median-label").textContent = s.count ? `Median ${fmt.num(s.median)} ${unit}` : "Median";
      charts.setHistogram(histChart, data.histogram, { median: s.median, unit, xTitle: `${label()} (${unit})` });
      renderStats(data, unit, [from, to]);
    } catch (err) {
      if (err.status === 404) return showRemoved(code);
      $("series-empty").hidden = false;
      $("series-empty").textContent = err.message;
    }
  }

  /* The descriptive statistics that used to live on the Station analysis page. */
  function renderStats(data, unit, [from, to]) {
    const s = data.stats;
    const empty = !s.count;
    const period = from ? `for ${fmt.range(from, to)}` : "all readings";
    const v = (x) => (empty ? "–" : `${fmt.num(x)} <small>${unit}</small>`);
    $("s-mean").innerHTML = v(s.mean);
    $("s-median").innerHTML = v(s.median);
    $("s-min").innerHTML = v(s.min);
    $("s-max").innerHTML = v(s.max);
    $("s-std").innerHTML = v(s.std);
    $("s-count").textContent = fmt.int(s.count);
    $("s-mean-ci").textContent = empty || s.mean_ci[0] == null ? period : `${state.ci}% CI ${fmt.num(s.mean_ci[0], 2)}–${fmt.num(s.mean_ci[1], 2)}`;
    $("s-iqr").textContent = empty ? period : `IQR ${fmt.num(s.p25)}–${fmt.num(s.p75)} ${unit}`;
    document.querySelectorAll("[data-period]").forEach((el) => { el.textContent = period; });
    // Aggregation smooths the plot; it does not pool the readings these figures describe.
    // Saying so stops the numbers looking stuck when the chart visibly changes shape.
    $("stats-note").textContent = empty
      ? `No ${label()} readings ${period}.`
      : `Every ${label()} reading ${period}: ${fmt.int(s.count)} in total. `
        + "Aggregation and the confidence interval change the plot, not these figures.";
  }

  function clearStats() {
    ["s-mean", "s-median", "s-min", "s-max", "s-std"].forEach((id) => { $(id).innerHTML = "–"; });
    $("s-count").textContent = "0";
    $("stats-note").textContent = "";
  }

  function select(code, { pan = true } = {}) {
    state.selected = code;
    refreshMarkerStyles();
    const m = state.markers.get(code);
    if (m && pan) { map.panTo(m.getLatLng()); m.openPopup(); }
    const url = new URL(location.href);
    url.searchParams.set("station", code);
    history.replaceState(null, "", url);
    loadPanel();
  }

  // ---------------- time window
  const slider = new DualRange($("window-slider"), {
    label: "Time window", step: HOUR,
    onInput: ([a, b]) => showWindow(a, b + 0),
    onChange: ([a, b]) => setWindow(a, b, true),
  });

  function showWindow(start, end) {
    $("start").value = isoDay(start);
    $("end").value = isoDay(end - 1);
    const days = Math.max(1, Math.round((end - start) / DAY));
    $("days").textContent = `${days} day${days === 1 ? "" : "s"}`;
  }

  function setWindow(start, end, reload) {
    if (reload !== "panel") stopPlaying();
    // Snap to whole days: the API filters by calendar dates.
    state.start = Math.floor(start / DAY) * DAY;
    state.end = Math.max(state.start + DAY, Math.ceil(end / DAY) * DAY);
    slider.setValues(state.start, state.end);
    showWindow(state.start, state.end);
    // "panel" refreshes only the charts: stepping through time should not refetch the
    // map markers on every frame.
    if (reload === "panel") { if (state.selected) loadPanel(); }
    else if (reload) { loadStations(); if (state.selected) loadPanel(); }
  }

  const onDates = () => {
    const a = clockMs($("start").value), b = clockMs($("end").value);
    if (a == null || b == null || b < a) return;
    setWindow(a, b + DAY, true);
  };
  $("start").addEventListener("change", onDates);
  $("end").addEventListener("change", onDates);

  // ---------------- toolbar
  $("measurement").addEventListener("change", (e) => { state.measurement = e.target.value; loadStations(); if (state.selected) loadPanel(); });
  $("ci").addEventListener("change", (e) => { state.ci = +e.target.value; if (state.selected) loadPanel(); });
  $("aggregation").value = state.aggregation;
  $("aggregation").addEventListener("change", (e) => { state.aggregation = e.target.value; if (state.selected) loadPanel(); });

  /* Step the series window forward a day at a time through the selected period. */
  let timer = null;
  const playIcon = $("play").innerHTML;
  function stopPlaying() {
    clearInterval(timer);
    timer = null;
    $("play").innerHTML = playIcon;
    $("play").setAttribute("aria-label", "Play through time");
  }
  $("play").addEventListener("click", () => {
    if (timer) return stopPlaying();
    if (state.start == null || state.end == null || state.extent == null) return;
    // Slide a frame the width of the current window across everything the station holds.
    const [lo0, hi0] = state.extent;
    const width = Math.min(Math.max(DAY, state.end - state.start), hi0 - lo0);
    let lo = state.end >= hi0 ? lo0 : state.start;
    $("play").innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor"/></svg>';
    $("play").setAttribute("aria-label", "Pause");
    const step = () => {
      const hi = Math.min(hi0, lo + width);
      setWindow(lo, hi, "panel");
      if (hi >= hi0) { stopPlaying(); loadStations(); return; }
      lo += DAY;
    };
    step();
    timer = setInterval(step, 1100);
  });

  /* The series on a white background, titled with the station and period. */
  $("download-chart").addEventListener("click", () => {
    const canvas = $("series");
    const pad = 24, head = 44;
    const out = document.createElement("canvas");
    out.width = canvas.width + pad * 2;
    out.height = canvas.height + pad * 2 + head;
    const ctx = out.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.fillStyle = "#132a3e";
    ctx.font = `600 ${22 * devicePixelRatio}px sans-serif`;
    const [from, to] = seriesWindow();
    const st = [...state.features.map((f) => f.properties), ...state.unplaced].find((x) => x.code === state.selected);
    ctx.fillText(`${st ? `${st.name} · ${st.code}` : state.selected} — ${label()}, ${from ? fmt.range(from, to) : "all readings"}`,
                 pad, pad + 20 * devicePixelRatio);
    ctx.drawImage(canvas, pad, pad + head);
    const link = document.createElement("a");
    link.download = `${state.selected}_${state.measurement.replace(/\W+/g, "_")}_${isoDay(from || Date.now())}.png`;
    link.href = out.toDataURL("image/png");
    link.click();
  });
  $("search").addEventListener("change", (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) return;
    const all = [...state.features.map((f) => f.properties), ...state.unplaced];
    const hit = all.find((s) => `${s.name} · ${s.code}`.toLowerCase() === q)
      || all.find((s) => s.code.toLowerCase() === q || s.name.toLowerCase() === q)
      || all.find((s) => s.name.toLowerCase().includes(q) || s.code.toLowerCase().includes(q));
    if (hit) { select(hit.code); const m = state.markers.get(hit.code); if (m) map.setView(m.getLatLng(), Math.max(map.getZoom(), 9)); }
    else window.AQ.toast(`No station matches “${e.target.value}”.`);
  });

  // ---------------- boot
  (async function init() {
    const stations = await loadStations(true);
    if (!stations.length) {
      $("count-chip").textContent = "No stations yet";
      $("series-empty").innerHTML = 'No stations yet. <a href="/data/">Import sensor records</a> to get started.';
      return;
    }
    const keys = new Map();
    let min = Infinity, max = -Infinity;
    // Slider bounds come from stations shown on the map, so an unplaced indoor
    // recording from months ago doesn't stretch the time window.
    const placed = state.features.map((f) => f.properties);
    for (const s of stations) for (const m of s.measurements) state.labels[m.key] = m.label;
    // Label by unit, not by the storage key: the temperature channel is named "F" but
    // every value shown is Celsius, so "Temperature (F)" would contradict the chart.
    for (const s of stations) for (const m of s.measurements) {
      keys.set(m.key, m.label === m.key ? m.key : `${m.label} (${fmt.unit(m.unit)})`);
    }
    for (const s of placed.length ? placed : stations) {
      if (s.first_reading) min = Math.min(min, clockMs(s.first_reading));
      if (s.last_reading) max = Math.max(max, clockMs(s.last_reading));
    }
    $("measurement").innerHTML = "";
    for (const [k, label] of keys) {
      const o = new Option(label, k);
      $("measurement").add(o);
    }
    // A link or bookmark can name a station that has since been deleted; fall back to the
    // first one, but say why rather than quietly showing somebody else's readings.
    const requested = state.selected;
    if (requested && !stations.some((s) => s.code === requested)) {
      window.AQ.toast(`${requested} has been deleted. Showing another station instead.`, 6000);
    }
    const params = new URLSearchParams(location.search);
    state.measurement = params.get("measurement") && keys.has(params.get("measurement")) ? params.get("measurement") : (keys.has("PM2.5") ? "PM2.5" : [...keys.keys()][0]);
    $("measurement").value = state.measurement;
    const lo = Math.floor(min / DAY) * DAY, hi = Math.ceil((max + 1) / DAY) * DAY;
    state.extent = [lo, hi];   // everything the placed stations hold; the slider's bounds
    slider.setBounds(lo, hi, HOUR);
    setWindow(Math.max(lo, hi - 7 * DAY), hi, false);
    await loadStations();
    if (!state.selected || !stations.some((s) => s.code === state.selected)) {
      state.selected = (state.features[0] || { properties: stations[0] }).properties.code;
    }
    select(state.selected, { pan: false });
  })().catch((err) => { $("count-chip").textContent = err.message; });
})();
