/* Shared helpers: API calls, formatting, dual range slider. */
(function () {
  "use strict";

  function csrfToken() {
    const field = document.querySelector("[name=csrfmiddlewaretoken]");
    if (field) return field.value;
    const c = document.cookie.split("; ").find((x) => x.startsWith("csrftoken="));
    return c ? decodeURIComponent(c.split("=")[1]) : "";
  }

  async function api(url, opts = {}) {
    const init = { headers: { Accept: "application/json" }, ...opts };
    if (init.method && init.method !== "GET") init.headers["X-CSRFToken"] = csrfToken();
    if (init.json !== undefined) {
      init.body = JSON.stringify(init.json);
      init.headers["Content-Type"] = "application/json";
      delete init.json;
    }
    const res = await fetch(url, init);
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok && res.status !== 409) {
      const err = new Error((data && (data.error || data.message)) || `Request failed (${res.status})`);
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* Timestamps are the sensor's source clock (no timezone). The API encodes them as
     if they were UTC, so every formatter below renders in UTC to show them unchanged. */
  const clockMs = (iso) => (iso ? Date.parse(iso.length === 10 ? iso + "T00:00:00Z" : iso.replace(/Z?$/, "Z")) : null);
  const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
  const isoClock = (ms) => new Date(ms).toISOString().slice(0, 19);
  const DAY = 86400000;

  const fmt = {
    num(v, digits = 1) {
      return v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString("en-IE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    },
    int(v) { return v === null || v === undefined ? "–" : Number(v).toLocaleString("en-IE"); },
    day(ms, withYear = false) {
      return new Date(ms).toLocaleDateString("en-GB", { day: "2-digit", month: "short", ...(withYear ? { year: "numeric" } : {}), timeZone: "UTC" });
    },
    dateTime(ms) {
      return new Date(ms).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
    },
    time(ms) { return new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" }); },
    range(startMs, endMsExclusive) {
      const a = new Date(startMs), b = new Date(endMsExclusive - 1);
      if (a.toISOString().slice(0, 10) === b.toISOString().slice(0, 10)) return fmt.day(startMs, true);
      const sameMonth = a.getUTCMonth() === b.getUTCMonth() && a.getUTCFullYear() === b.getUTCFullYear();
      const left = sameMonth ? String(a.getUTCDate()).padStart(2, "0") : fmt.day(startMs, a.getUTCFullYear() !== b.getUTCFullYear());
      return `${left} – ${fmt.day(b.getTime(), true)}`;
    },
    // Temperatures arrive already converted to Celsius; these are the bare-letter forms
    // an older export or a hand-made CSV might still carry.
    unit(u) { return u === "C" ? "°C" : u === "F" ? "°F" : u || ""; },
  };

  function toast(message, ms = 4200) {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  const debounce = (fn, wait = 250) => {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
  };

  /* Two-thumb range slider built from two native range inputs (keyboard accessible). */
  class DualRange {
    constructor(el, { min = 0, max = 100, step = 1, label = "Range", onInput, onChange } = {}) {
      this.el = el;
      this.onInput = onInput;
      this.onChange = onChange;
      el.classList.add("range");
      el.innerHTML = `<div class="track"></div><div class="fill"></div>
        <input type="range" aria-label="${label} start"><input type="range" aria-label="${label} end">`;
      [this.a, this.b] = el.querySelectorAll("input");
      this.fill = el.querySelector(".fill");
      this.setBounds(min, max, step);
      const handle = (e, commit) => {
        let lo = +this.a.value, hi = +this.b.value;
        if (lo > hi - this.step) {
          if (e.target === this.a) { lo = hi - this.step; this.a.value = lo; } else { hi = lo + this.step; this.b.value = hi; }
        }
        this.paint();
        (commit ? this.onChange : this.onInput)?.(this.values);
      };
      for (const input of [this.a, this.b]) {
        input.addEventListener("input", (e) => handle(e, false));
        input.addEventListener("change", (e) => handle(e, true));
      }
    }
    setBounds(min, max, step) {
      this.min = min; this.max = Math.max(max, min + step); this.step = step;
      for (const input of [this.a, this.b]) { input.min = min; input.max = this.max; input.step = step; }
      this.paint();
    }
    setValues(lo, hi) {
      this.a.value = Math.max(this.min, lo);
      this.b.value = Math.min(this.max, hi);
      this.paint();
    }
    get values() { return [+this.a.value, +this.b.value]; }
    paint() {
      const span = this.max - this.min || 1;
      const l = ((+this.a.value - this.min) / span) * 100, r = ((+this.b.value - this.min) / span) * 100;
      this.fill.style.left = l + "%";
      this.fill.style.width = Math.max(0, r - l) + "%";
      this.a.style.zIndex = +this.a.value > this.max - (this.max - this.min) * 0.05 ? 3 : 2;
    }
  }

  window.AQ = { api, fmt, toast, debounce, DualRange, clockMs, isoDay, isoClock, DAY };
})();
