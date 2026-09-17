/* Chart.js wrappers for the time series (with CI band) and the histogram (with median line). */
(function () {
  "use strict";
  const { fmt, DAY } = window.AQ;
  const css = getComputedStyle(document.documentElement);
  const C = {
    teal: css.getPropertyValue("--teal").trim() || "#178a94",
    band: "rgba(23, 138, 148, 0.20)",
    violet: css.getPropertyValue("--violet").trim() || "#8a6be3",
    violetFill: "rgba(138, 107, 227, 0.72)",
    grid: "#e6ecf0",
    ink: "#3d5163",
  };
  Chart.defaults.font.family = css.getPropertyValue("--font");
  Chart.defaults.font.size = 12.5;
  Chart.defaults.color = C.ink;
  Chart.defaults.animation = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 250 };

  // Ticks on round clock times (midnight for multi-day spans), shown in source-clock time.
  function timeTicks(axis) {
    const span = axis.max - axis.min;
    if (!(span > 0)) return;
    const hour = 3600000;
    const candidates = [hour, 3 * hour, 6 * hour, 12 * hour, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
    const target = Math.max(3, Math.floor(axis.width / 90));
    const step = candidates.find((s) => span / s <= target) || 60 * DAY;
    const ticks = [];
    for (let t = Math.ceil(axis.min / step) * step; t <= axis.max; t += step) ticks.push({ value: t });
    axis.ticks = ticks;
    axis._stepMs = step;
  }

  /* Concentrations are read against zero, so their axis starts there. Temperature and
     humidity are not: a day spanning 17–33 °C drawn from 0 is a flat line squeezed into
     the top third of the panel, which hides exactly the variation the chart is for. */
  function zeroBased(measurement, unit) {
    const u = (unit || "").trim().toLowerCase();
    if (/^(f|c|rh)$/i.test((measurement || "").trim())) return false;
    return !(u === "°c" || u === "°f" || u === "%");
  }

  /* The band the plot actually draws: the confidence interval where there is one, so a
     lone outlier beyond it cannot stretch the axis and flatten everything else. */
  function seriesBounds(s) {
    const finite = (arr) => (arr || []).filter((v) => v != null && Number.isFinite(v));
    const lows = s.has_ci ? finite(s.lower) : [];
    const highs = s.has_ci ? finite(s.upper) : [];
    const means = finite(s.mean);
    if (!means.length) return null;
    const lo = Math.min(...(lows.length ? lows : means));
    const hi = Math.max(...(highs.length ? highs : means));
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    const pad = (hi - lo) * 0.08 || Math.max(Math.abs(hi) * 0.02, 0.5);
    return { min: lo - pad, max: hi + pad };
  }

  function series(canvas) {
    return new Chart(canvas, {
      type: "line",
      data: { datasets: [
        { label: "upper", data: [], borderWidth: 0, pointRadius: 0, fill: false, spanGaps: false },
        { label: "lower", data: [], borderWidth: 0, pointRadius: 0, backgroundColor: C.band, fill: "-1", spanGaps: false },
        { label: "mean", data: [], borderColor: C.teal, pointBackgroundColor: C.teal, pointBorderColor: C.teal, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, spanGaps: false },
      ] },
      options: {
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            filter: (item) => item.datasetIndex === 2,
            callbacks: {
              title: (items) => (items.length ? fmt.dateTime(items[0].parsed.x) : ""),
              label: (item) => {
                const meta = item.chart.$meta || {};
                const i = item.dataIndex;
                const lo = meta.lower?.[i], hi = meta.upper?.[i];
                const ci = lo != null && hi != null ? `  (${meta.ciLabel}: ${fmt.num(lo)}–${fmt.num(hi)})` : "";
                return `${fmt.num(item.parsed.y)} ${meta.unit || ""}${ci}`;
              },
              footer: (items) => {
                const n = items.length ? item0n(items[0]) : null;
                return n && n > 1 ? `${n} readings` : "";
              },
            },
          },
        },
        scales: {
          x: { type: "linear", grid: { color: C.grid }, afterBuildTicks: timeTicks,
               ticks: { maxRotation: 0, callback(v) { return this._stepMs >= DAY ? fmt.day(v) : fmt.time(v); } } },
          y: { beginAtZero: true, grid: { color: C.grid }, title: { display: true, text: "" } },
        },
      },
    });
  }
  const item0n = (item) => item.chart.$meta?.n?.[item.dataIndex];

  function setSeries(chart, s, { unit = "", yTitle = "", ciLabel = "95% CI", range, measurement = "" } = {}) {
    const pts = (arr) => s.t.map((t, i) => ({ x: t, y: arr[i] }));
    // Break the line where readings are missing for more than 3 typical steps.
    const gapped = (arr) => {
      const out = pts(arr);
      if (s.t.length < 3) return out;
      const step = (s.t[s.t.length - 1] - s.t[0]) / (s.t.length - 1);
      const res = [];
      out.forEach((p, i) => {
        if (i && p.x - out[i - 1].x > Math.max(step * 3, 3 * 3600000 / 6)) res.push({ x: (p.x + out[i - 1].x) / 2, y: null });
        res.push(p);
      });
      return res;
    };
    chart.data.datasets[0].data = s.has_ci ? gapped(s.upper) : [];
    chart.data.datasets[1].data = s.has_ci ? gapped(s.lower) : [];
    chart.data.datasets[2].data = gapped(s.mean);
    // A handful of points (e.g. one daily mean) would be invisible as a bare line.
    const few = s.t.length <= 40;
    chart.data.datasets[2].pointRadius = few ? 3.5 : 0;
    // Tooltip lookups by index must match the gapped arrays.
    const idx = gapped(s.mean);
    const byX = new Map(s.t.map((t, i) => [t, i]));
    chart.$meta = {
      unit, ciLabel,
      lower: idx.map((p) => (byX.has(p.x) ? s.lower[byX.get(p.x)] : null)),
      upper: idx.map((p) => (byX.has(p.x) ? s.upper[byX.get(p.x)] : null)),
      n: idx.map((p) => (byX.has(p.x) ? s.n[byX.get(p.x)] : null)),
    };
    const y = chart.options.scales.y;
    y.title.text = yTitle;
    const bounds = zeroBased(measurement, unit) ? null : seriesBounds(s);
    y.beginAtZero = !bounds;
    if (bounds) { y.min = bounds.min; y.max = bounds.max; } else { delete y.min; delete y.max; }
    if (range) { chart.options.scales.x.min = range[0]; chart.options.scales.x.max = range[1]; }
    chart.update();
  }

  const medianLine = {
    id: "medianLine",
    afterDatasetsDraw(chart) {
      const m = chart.$median;
      const edges = chart.$edges;
      if (m == null || !edges || edges.length < 2) return;
      const x = chart.scales.x;
      const w = edges[1] - edges[0];
      const frac = (m - edges[0]) / w - 0.5; // category index of bin centres
      const p0 = x.getPixelForValue(0), p1 = x.getPixelForValue(Math.min(1, edges.length - 2)) ;
      const px = edges.length > 2 ? p0 + frac * (p1 - p0) : p0;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = C.violet;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
      ctx.restore();
    },
  };

  function histogram(canvas) {
    return new Chart(canvas, {
      type: "bar",
      data: { labels: [], datasets: [{ data: [], backgroundColor: C.violetFill, borderColor: C.violet, borderWidth: 0, barPercentage: 1, categoryPercentage: 0.92 }] },
      plugins: [medianLine],
      options: {
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: (items) => {
              const e = items[0].chart.$edges, i = items[0].dataIndex;
              return `${fmt.num(e[i])} – ${fmt.num(e[i + 1])} ${items[0].chart.$unit || ""}`;
            },
            label: (item) => `${fmt.int(item.parsed.y)} readings`,
          } },
        },
        scales: {
          x: { grid: { display: false }, title: { display: true, text: "" },
               ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 14 } },
          y: { beginAtZero: true, grid: { color: C.grid }, title: { display: true, text: "Count" }, ticks: { precision: 0 } },
        },
      },
    });
  }

  function setHistogram(chart, h, { median, unit = "", xTitle = "" } = {}) {
    const edges = h.edges || [];
    const w = h.bin_width || 1;
    const digits = w >= 1 ? 0 : w >= 0.1 ? 1 : 2;
    chart.data.labels = (h.counts || []).map((_, i) => fmt.num((edges[i] + edges[i + 1]) / 2, digits));
    chart.data.datasets[0].data = h.counts || [];
    chart.$edges = edges;
    chart.$median = median;
    chart.$unit = unit;
    chart.options.scales.x.title.text = xTitle;
    chart.update();
  }

  window.AQ.charts = { series, setSeries, histogram, setHistogram, seriesBounds, zeroBased, colors: C };
})();
