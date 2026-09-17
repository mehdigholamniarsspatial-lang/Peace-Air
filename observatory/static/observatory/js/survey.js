/* Citizen feedback survey — progressive enhancement only.

   The form is a plain HTML POST and stays fully usable with this file blocked or broken:
   everything below improves the experience (live counters, conditional fields, inline
   errors, a saved draft) but the server validates and stores regardless. */
(function () {
  "use strict";
  const form = document.getElementById("survey-form");
  if (!form) return;

  const $ = (id) => document.getElementById(id);
  const DRAFT_KEY = "aq-feedback-draft";
  const IMPROVEMENTS_LIMIT = 3;
  const TRUST_WITH_DOUBTS = ["neither", "distrust_somewhat", "distrust_a_lot", "not_sure"];
  const field = (name) => form.querySelectorAll(`[name="${name}"]`);
  const checked = (name) => [...field(name)].filter((i) => i.checked).map((i) => i.value);
  const radio = (name) => ([...field(name)].find((i) => i.checked) || {}).value || "";

  // ---------------- consent gate
  const submit = $("survey-submit");
  function refreshGate() {
    const ready = $("id_consent").checked && $("id_age_confirm").checked;
    submit.disabled = !ready;
    $("submit-help").textContent = ready
      ? "Your answers are sent anonymously."
      : "The button becomes available once you have ticked both boxes at the top.";
  }
  ["id_consent", "id_age_confirm"].forEach((id) => $(id).addEventListener("change", refreshGate));
  refreshGate();

  // ---------------- "answers on its own" options
  const EXCLUSIVE = {
    pollution_sources: ["none", "not_sure"],
    trust_concerns: ["no_concerns"],
    participation: ["not_interested"],
  };
  function applyExclusive(name) {
    const boxes = [...field(name)];
    const exclusive = EXCLUSIVE[name];
    const active = boxes.find((b) => b.checked && exclusive.includes(b.value));
    boxes.forEach((b) => {
      if (b === active) return;
      // "None" stays clickable whatever else is ticked — choosing it is how somebody
      // changes their mind, and it clears the rest rather than being blocked by them.
      if (active) { b.checked = false; b.disabled = true; } else { b.disabled = false; }
    });
  }
  Object.keys(EXCLUSIVE).forEach((name) => {
    field(name).forEach((box) => box.addEventListener("change", () => { applyExclusive(name); saveDraft(); }));
    applyExclusive(name);
  });

  // ---------------- Q3: at most three improvements, plus the "something else" box
  const otherWrap = $("wrap-improvements-other");
  const otherInput = $("id_dashboard_improvements_other");
  function applyImprovements() {
    const boxes = [...field("dashboard_improvements")];
    const count = boxes.filter((b) => b.checked).length;
    const atLimit = count >= IMPROVEMENTS_LIMIT;
    boxes.forEach((b) => { b.disabled = atLimit && !b.checked; });
    $("limit-dashboard-improvements").textContent = atLimit
      ? `Choose up to ${IMPROVEMENTS_LIMIT} — limit reached, untick one to change your answer`
      : `Choose up to ${IMPROVEMENTS_LIMIT} (${count} chosen)`;
    const wantsOther = boxes.some((b) => b.checked && b.value === "other");
    otherWrap.hidden = !wantsOther;
    if (!wantsOther) otherInput.value = "";
  }
  field("dashboard_improvements").forEach((b) => b.addEventListener("change", () => { applyImprovements(); saveDraft(); }));
  applyImprovements();

  // ---------------- Q4: the doubts question only appears if a doubt was expressed
  const concernsWrap = $("wrap-trust-concerns");
  function applyTrust() {
    const show = TRUST_WITH_DOUBTS.includes(radio("sensor_trust"));
    concernsWrap.hidden = !show;
    if (!show) field("trust_concerns").forEach((b) => { b.checked = false; b.disabled = false; });
  }
  field("sensor_trust").forEach((r) => r.addEventListener("change", () => { applyTrust(); saveDraft(); }));
  applyTrust();

  // ---------------- optional email
  const emailWrap = $("wrap-contact-email");
  const emailInput = $("id_contact_email");
  function applyEmail() {
    const wants = $("id_wants_results").checked;
    emailWrap.hidden = !wants;
    if (!wants) emailInput.value = "";
  }
  $("id_wants_results").addEventListener("change", () => { applyEmail(); saveDraft(); });
  applyEmail();

  // ---------------- character counter
  const comments = $("id_additional_comments");
  function countComments() {
    const left = 1000 - comments.value.length;
    $("count-comments").textContent = `${left} character${left === 1 ? "" : "s"} remaining`;
  }
  comments.addEventListener("input", countComments);
  countComments();

  // ---------------- draft, in sessionStorage only and cleared on success
  function saveDraft() {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify(collect()));
    } catch (_) { /* private browsing, a full quota: the form still works */ }
  }
  function restoreDraft() {
    let draft;
    try { draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null"); } catch (_) { return; }
    if (!draft) return;
    const a = draft.answers || {};
    const setRadio = (name, value) => field(name).forEach((i) => { i.checked = i.value === value; });
    const setBoxes = (name, values) => field(name).forEach((i) => { i.checked = (values || []).includes(i.value); });
    if (a.county) $("id_county").value = a.county;
    setRadio("area_type", a.area_type);
    setRadio("air_quality_rating", a.air_quality_rating);
    setRadio("dashboard_usefulness", a.dashboard_usefulness);
    setRadio("sensor_trust", a.sensor_trust);
    setRadio("open_data_support", a.open_data_support);
    setBoxes("pollution_sources", a.pollution_sources);
    setBoxes("dashboard_improvements", a.dashboard_improvements);
    setBoxes("trust_concerns", a.trust_concerns);
    setBoxes("participation", a.participation);
    $("id_near_border").checked = Boolean(a.near_border);
    otherInput.value = a.dashboard_improvements_other || "";
    comments.value = a.additional_comments || "";
    // Consent is never restored: it has to be given deliberately, every time.
    Object.keys(EXCLUSIVE).forEach(applyExclusive);
    applyImprovements();
    applyTrust();
    countComments();
  }
  form.addEventListener("change", saveDraft);
  comments.addEventListener("input", saveDraft);
  restoreDraft();

  // ---------------- payload
  /* `answers` and `contact` are separate objects so the server can store them in separate
     tables. Nothing may key one to the other: a respondent who asks for the results must
     not become re-identifiable through the answers they gave. */
  function collect() {
    const improvements = checked("dashboard_improvements");
    const wantsResults = $("id_wants_results").checked;
    const payload = {
      schema_version: "1.0",
      submitted_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      consent: { given: $("id_consent").checked, age_confirmed: $("id_age_confirm").checked,
                 consent_text_version: "1.0" },
      answers: {
        county: $("id_county").value,
        area_type: radio("area_type"),
        near_border: $("id_near_border").checked,
        air_quality_rating: radio("air_quality_rating"),
        pollution_sources: checked("pollution_sources"),
        dashboard_usefulness: radio("dashboard_usefulness"),
        dashboard_improvements: improvements,
        dashboard_improvements_other: improvements.includes("other") ? otherInput.value.trim() : null,
        sensor_trust: radio("sensor_trust"),
        trust_concerns: checked("trust_concerns"),
        participation: checked("participation"),
        open_data_support: radio("open_data_support"),
        additional_comments: comments.value,
      },
    };
    if (wantsResults) payload.contact = { wants_results: true, email: emailInput.value.trim() };
    return payload;
  }

  // ---------------- validation
  const REQUIRED = [
    ["county", "Please choose a county.", () => $("id_county").value, "id_county"],
    ["area_type", "Please choose the kind of area you live in.", () => radio("area_type")],
    ["air_quality_rating", "Please describe the air quality where you live.", () => radio("air_quality_rating")],
    ["dashboard_usefulness", "Please say how useful you find the dashboard.", () => radio("dashboard_usefulness")],
    ["sensor_trust", "Please say how much you trust the sensor readings.", () => radio("sensor_trust")],
    ["open_data_support", "Please say whether you agree that readings should be published openly.",
     () => radio("open_data_support")],
  ];
  const slug = (name) => name.replace(/_/g, "-");

  function showError(container, slugName, message, target) {
    let box = document.getElementById(`err-${slugName}`);
    if (!box) {
      box = document.createElement("p");
      box.className = "survey-error";
      box.id = `err-${slugName}`;
      const legend = container.querySelector("legend");
      legend ? legend.insertAdjacentElement("afterend", box) : container.prepend(box);
    }
    box.textContent = message;
    // aria-describedby on a <fieldset> is patchily supported, so tie the message to each
    // radio in the group instead — whichever one the reader lands on announces the error.
    const marks = target ? [target] : [...container.querySelectorAll("input")];
    marks.forEach((el) => {
      el.setAttribute("aria-invalid", "true");
      el.setAttribute("aria-describedby", box.id);
    });
  }
  function clearErrors() {
    form.querySelectorAll(".survey-error").forEach((e) => e.remove());
    form.querySelectorAll('[aria-invalid="true"]').forEach((e) => e.removeAttribute("aria-invalid"));
    $("form-status").textContent = "";
  }

  function validate() {
    clearErrors();
    const problems = [];
    for (const [name, message, read, inputId] of REQUIRED) {
      if (read()) continue;
      const target = inputId ? $(inputId) : null;
      const container = target ? target.closest(".survey-field") : form.querySelector(`[name="${name}"]`).closest("fieldset");
      showError(container, slug(name), message, target);
      problems.push(target || form.querySelector(`[name="${name}"]`));
    }
    if (checked("dashboard_improvements").includes("other") && !otherInput.value.trim()) {
      showError(otherWrap, "improvements-other", "Please say what else would help.", otherInput);
      problems.push(otherInput);
    }
    if ($("id_wants_results").checked) {
      const value = emailInput.value.trim();
      if (!value || !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value)) {
        showError(emailWrap, "contact-email",
                  value ? "Please enter an email address in the form name@example.com."
                        : "Please enter an email address, or untick the box above.", emailInput);
        problems.push(emailInput);
      }
    }
    if (problems.length) {
      $("form-status").textContent =
        problems.length === 1
          ? "Your answers were not sent. One question needs attention."
          : `Your answers were not sent. ${problems.length} questions need attention.`;
      problems[0].focus();
      problems[0].scrollIntoView({ block: "center", behavior: "smooth" });
    }
    return problems.length === 0;
  }

  // Re-check a field on blur once it has already errored, never on every keystroke.
  form.addEventListener("blur", (event) => {
    const el = event.target;
    if (el.getAttribute && el.getAttribute("aria-invalid") === "true") validate();
  }, true);

  // ---------------- submit
  const csrf = () => (form.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  let sending = false;

  function succeed() {
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (_) { /* nothing to clear */ }
    const panel = document.createElement("div");
    panel.className = "card card-pad survey-done";
    panel.setAttribute("role", "status");
    panel.innerHTML = "<h1>Thank you. Your answers help make the case for cleaner air across the island of Ireland.</h1>"
      + '<p class="muted">You can close this page. If you asked for a summary of the results we will email it once '
      + "the analysis is finished, and delete your address afterwards.</p>";
    document.querySelector(".survey").replaceChildren(panel);
    panel.setAttribute("tabindex", "-1");
    panel.focus();
  }

  form.addEventListener("submit", async (event) => {
    if (!validate()) { event.preventDefault(); return; }
    event.preventDefault();
    if (sending) return;                       // no double submission
    sending = true;
    submit.disabled = true;
    submit.textContent = "Sending…";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify(collect()),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const first = Object.entries(body.errors || {})[0];
        throw new Error(first ? `${first[1]}` : "The server could not accept your answers.");
      }
      succeed();
    } catch (error) {
      // Answers stay on the page and in the draft; nothing is dropped silently.
      sending = false;
      submit.disabled = false;
      submit.textContent = "Try sending again";
      $("form-status").textContent =
        `Your answers could not be sent (${error.message}). They are still here — please try again.`;
      $("form-status").scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });
})();
