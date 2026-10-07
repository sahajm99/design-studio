// Design Studio: small, page-specific behaviour. No build step, no framework.
// Every page works from its server-rendered HTML alone; this file only adds
// the live-updating touches (the theme toggle, the bar condensing as the page
// scrolls, the library's choices and progress line, the Studio form's auto
// limits and "Research first", the run and session pages' polling, the session
// page's directions, the post page's copy button).
(() => {
  "use strict";

  const STEP_LABELS = {
    load_context: "Gather context",
    art_director: "Design the post",
    get_photo: "Get the photo",
    render: "Render",
    save_post: "Save",
    write_prompt: "Write the prompt",
    save_draft: "Save the draft",
    load_prompt: "Load the prompt",
    generate_samples: "Make the samples",
    review_samples: "Review the samples",
    save_round: "Save the round",
    load_feedback: "Read the feedback",
    rewrite_prompt: "Rewrite the prompt",
    load_pick: "Load the chosen photo",
    rewrite_words: "Rewrite the words",
    rank_samples: "Rank the samples",
    shortlist: "Choose fitting layouts",
    render_layouts: "Render the layouts",
    save_layouts: "Save the layouts",
    plan: "Plan the run",
    draft: "Draft the prompt",
    round_loop: "Make and judge the rounds",
    compose: "Compose the layouts",
    final_check: "Check the finished post",
    finish: "Finish the post",
    report: "Report",
    // v4: the scout run, and the auto run's research stage.
    check_brief: "Check the brief",
    plan_queries: "Plan the searches",
    search: "Search the web",
    propose: "Propose directions",
    save_directions: "Save the directions",
    research: "Research",
  };

  // A step name two kinds of run share, labelled for one of them, as the server does.
  const STEP_LABELS_BY_KIND = {
    scout: { report: "Write the research" },
  };

  function stepLabel(kind, step) {
    const own = STEP_LABELS_BY_KIND[kind];
    return (own && own[step]) || STEP_LABELS[step] || step;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function metaText(startedAt, endedAt, provider, attempts) {
    const parts = [];
    if (endedAt) {
      const seconds = (new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000;
      parts.push(`${seconds.toFixed(1)}s`);
    }
    if (provider) parts.push(`with ${provider}`);
    let text = parts.join(" ");
    if (attempts > 1) {
      const extra = `${attempts} attempts`;
      text = text ? `${text}, ${extra}` : extra;
    }
    return text;
  }

  // -------------------------------------------------------------------- theme

  // The theme the page shows: the one chosen with the toggle, or else the system's.
  function currentTheme() {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "dark" || attr === "light") return attr;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // The button's words offer the other theme; its icon is the moon in light, the sun in dark.
  function updateThemeToggle(button, theme) {
    const label = theme === "dark" ? "Switch to light" : "Switch to dark";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.querySelectorAll("[data-theme-icon]").forEach((icon) => {
      icon.hidden = icon.dataset.themeIcon !== theme;
    });
  }

  function initTheme() {
    const button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    updateThemeToggle(button, currentTheme());

    button.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("theme", next);
      } catch {
        /* the choice just won't survive a reload */
      }
      updateThemeToggle(button, next);
    });

    // With nothing chosen the page follows the system setting as it changes; so does the button.
    const system = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (system && system.addEventListener) {
      system.addEventListener("change", () => updateThemeToggle(button, currentTheme()));
    }
  }

  // ---------------------------------------------------------------------- bar

  // Past 8px of scroll the bar condenses from 60px to 52px; its slot keeps its height,
  // so the page never jumps.
  function initBar() {
    const bar = document.querySelector("[data-bar]");
    if (!bar) return;
    const sync = () => bar.classList.toggle("is-condensed", window.scrollY > 8);
    window.addEventListener("scroll", sync, { passive: true });
    sync();
  }

  // ---------------------------------------------------------- studio page

  // The auto limits only mean something in auto mode, so they show, and are
  // sent, only while Auto is chosen. They are disabled while hidden, so a value
  // the designer can no longer see never blocks the form.
  function initStudioPage() {
    const limits = document.querySelector("[data-auto-limits]");
    const form = limits && limits.closest("form");
    if (!form) return;

    const radios = Array.from(form.querySelectorAll('input[name="mode"]'));
    const syncLimits = () => {
      const auto = radios.some((radio) => radio.checked && radio.value === "auto");
      limits.hidden = !auto;
      limits.querySelectorAll("input").forEach((input) => {
        input.disabled = !auto;
      });
    };
    radios.forEach((radio) => radio.addEventListener("change", syncLimits));
    syncLimits();

    // "Research first" follows the mode as it changes: off for Manual, on for Auto. It is
    // not touched otherwise, so the designer can change it after choosing a mode. Its hidden
    // companion is enabled, so the server follows the box even when it is unticked.
    const research = form.querySelector("[data-research-first]");
    const researchChoice = form.querySelector("[data-research-choice]");
    if (researchChoice) researchChoice.disabled = false;
    if (research) {
      radios.forEach((radio) =>
        radio.addEventListener("change", () => {
          if (radio.checked) research.checked = radio.value === "auto";
        }),
      );
    }

    const budget = limits.querySelector('input[name="photo_budget"]');
    const hint = limits.querySelector("[data-auto-hint]");
    if (!budget || !hint) return;
    const syncHint = () => setText(hint, autoHintText(budget));
    budget.addEventListener("input", syncHint);
    syncHint();
  }

  // The number the server will use: the field's own default when it is empty,
  // otherwise the typed number clamped to the field's range, as the server does.
  function autoHintText(input) {
    const typed = parseInt(input.value, 10);
    const photos = Number.isNaN(typed)
      ? parseInt(input.defaultValue, 10)
      : Math.min(Number(input.max), Math.max(Number(input.min), typed));
    return `Auto mode makes up to ${photos === 1 ? "1 photo" : `${photos} photos`} on its own.`;
  }

  // -------------------------------------------------------------- library

  function initLibrary() {
    const grid = document.querySelector("[data-ref-grid]");
    const filterBar = document.querySelector("[data-filter-bar]");
    const progress = document.querySelector("[data-analysis-progress]");
    if (!grid && !filterBar && !progress) return;

    if (grid) grid.addEventListener("click", onChoiceClick);
    if (filterBar) filterBar.addEventListener("click", onFilterClick);
    if (progress && progress.dataset.running === "true") pollLibraryStatus();
  }

  async function onChoiceClick(event) {
    const button = event.target.closest(".choice-btn");
    const card = button && button.closest(".ref-card");
    if (!button || !card) return;

    const requested = button.dataset.choice;
    const next = card.dataset.choice === requested ? "none" : requested;

    let response;
    try {
      response = await fetch(`/api/references/${card.dataset.refId}/choice`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice: next }),
      });
    } catch {
      return; // offline, or the server went away: leave the card as it was
    }
    if (!response.ok) return;
    const taste = await response.json();

    card.dataset.choice = next;
    card.querySelectorAll(".choice-btn").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.choice === next));
    });
    card.classList.remove("just-updated");
    requestAnimationFrame(() => card.classList.add("just-updated"));

    updateTastePanel(taste);
    applyFilter(currentFilter());
  }

  function updateTastePanel(taste) {
    setText(document.querySelector("[data-taste-summary]"), taste.summary);
    setText(document.querySelector("[data-taste-liked]"), taste.liked_count);
    setText(document.querySelector("[data-taste-disliked]"), taste.disliked_count);
  }

  function currentFilter() {
    const active = document.querySelector(".filter-btn.is-active");
    return active ? active.dataset.filter : "all";
  }

  function onFilterClick(event) {
    const button = event.target.closest(".filter-btn");
    if (!button) return;
    document.querySelectorAll(".filter-btn").forEach((btn) => btn.classList.toggle("is-active", btn === button));
    applyFilter(button.dataset.filter);
  }

  function applyFilter(filter) {
    document.querySelectorAll(".ref-card").forEach((card) => {
      const show = filter === "all" || card.dataset.choice === filter;
      card.classList.toggle("is-hidden", !show);
    });
  }

  async function pollLibraryStatus() {
    const progress = document.querySelector("[data-analysis-progress]");
    if (!progress) return;
    for (;;) {
      let response;
      try {
        response = await fetch("/api/library/status");
      } catch {
        return;
      }
      if (!response.ok) return;
      const data = await response.json();
      setText(document.querySelector("[data-analysed]"), data.progress.analysed);
      setText(document.querySelector("[data-total]"), data.progress.total);
      updateTastePanel(data.taste);
      progress.dataset.running = String(data.progress.running);
      if (!data.progress.running) return;
      await sleep(1500);
    }
  }

  // ------------------------------------------------------------- run page

  function initRunPage() {
    const page = document.getElementById("run-page");
    if (!page || page.dataset.poll !== "true") return;
    pollRun(page.dataset.runId, page.dataset.runKind || "");
  }

  async function pollRun(runId, kind) {
    for (;;) {
      await sleep(1000);
      let response;
      try {
        response = await fetch(`/api/runs/${runId}`);
      } catch {
        continue; // a transient fetch error; try again on the next tick
      }
      if (!response.ok) continue;
      const data = await response.json();
      renderSteps(data.events, kind);
      renderChildRuns(data.children);
      if (data.run.status === "running") continue;
      finishRun(data.run);
      return;
    }
  }

  function buildStepRow(step, kind) {
    const row = document.createElement("li");
    row.className = "step-row";
    row.dataset.step = step;
    row.innerHTML =
      '<span class="step-status-dot" aria-hidden="true"></span>' +
      '<div class="step-body"><p class="step-name">' +
      '<span data-cell="label"></span>' +
      '<span class="status-pill" data-cell="status"></span>' +
      "</p></div>";
    setText(row.querySelector('[data-cell="label"]'), stepLabel(kind, step));
    return row;
  }

  function setOptionalLine(body, cell, text) {
    let line = body.querySelector(`[data-cell="${cell}"]`);
    if (!text) {
      if (line) line.remove();
      return;
    }
    if (!line) {
      line = document.createElement("p");
      line.dataset.cell = cell;
      line.className = cell === "meta" ? "step-meta" : cell === "note" ? "step-note" : "step-error";
      body.appendChild(line);
    }
    line.textContent = text;
  }

  function renderSteps(events, kind) {
    const body = document.querySelector("[data-steps-body]");
    if (!body) return;
    events.forEach((event) => {
      let row = body.querySelector(`[data-step="${event.step}"]`);
      if (!row) {
        row = buildStepRow(event.step, kind);
        body.appendChild(row);
      }
      row.querySelector(".step-status-dot").className = `step-status-dot status-${event.status}`;
      const statusCell = row.querySelector('[data-cell="status"]');
      statusCell.textContent = event.status;
      statusCell.className = `status-pill status-${event.status}`;

      const stepBody = row.querySelector(".step-body");
      setOptionalLine(stepBody, "meta", metaText(event.started_at, event.ended_at, event.provider, event.attempts));
      setOptionalLine(stepBody, "note", event.note);
      setOptionalLine(stepBody, "error", event.error);
    });
  }

  function buildChildRunRow(child) {
    const row = document.createElement("li");
    row.className = "run-row";
    const link = document.createElement("a");
    link.className = "run-link";
    link.href = `/runs/${child.id}`;
    link.textContent = child.kind_label;
    const status = document.createElement("span");
    status.className = `status-pill status-${child.status}`;
    status.textContent = child.status;
    row.append(link, status);
    return row;
  }

  // An automatic session's run gains a child run per stage as it goes, so the
  // list is rebuilt from the server's on every poll.
  function renderChildRuns(children) {
    const box = document.querySelector("[data-run-children]");
    const list = box && box.querySelector("[data-run-children-list]");
    if (!list || !Array.isArray(children)) return;
    list.replaceChildren(...children.map(buildChildRunRow));
    box.hidden = children.length === 0;
  }

  function finishRun(run) {
    const working = document.querySelector("[data-run-working]");
    if (working) working.hidden = true;

    const statusLabel = document.querySelector("[data-run-status-label]");
    if (statusLabel) {
      statusLabel.textContent = run.status;
      statusLabel.className = `status-pill status-${run.status}`;
    }

    if (run.status === "succeeded") {
      const done = document.querySelector("[data-run-done]");
      if (done) done.hidden = false;
      // Session-stage runs (draft, samples, revise_prompt) have no post of their
      // own; only follow the post link when the run actually made one.
      const destination = run.post_id ? `/posts/${run.post_id}` : run.session_id ? `/sessions/${run.session_id}` : null;
      const postLink = document.querySelector("[data-run-post-link]");
      if (postLink && run.post_id) postLink.setAttribute("href", `/posts/${run.post_id}`);
      const sessionLink = document.querySelector("[data-run-session-link]");
      if (sessionLink && run.session_id) sessionLink.setAttribute("href", `/sessions/${run.session_id}`);
      if (destination) {
        setTimeout(() => {
          window.location.href = destination;
        }, 1200);
      }
      return;
    }

    const failed = document.querySelector("[data-run-failed]");
    setText(document.querySelector("[data-run-error]"), run.error || "");
    if (failed) failed.hidden = false;
  }

  // --------------------------------------------------------- session page

  function initSessionPage() {
    const page = document.querySelector("[data-session-page]");
    if (!page) return;

    document.addEventListener("click", onSampleReactionClick);
    document.addEventListener("focusout", onSampleCommentBlur);
    initAdjustForms();
    initDirections();

    if (page.dataset.poll === "true") pollSession(page.dataset.sessionId);
  }

  async function postSampleReaction(sampleId, reaction, comment) {
    let response;
    try {
      response = await fetch(`/api/samples/${sampleId}/reaction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reaction, comment }),
        // A comment saved as its field loses focus may meet the page being drawn again.
        keepalive: true,
      });
    } catch {
      return null; // offline, or the server went away: leave the card as it was
    }
    if (!response.ok) return null;
    return response.json();
  }

  function applySampleReaction(card, sample) {
    card.dataset.reaction = sample.reaction;
    card.dataset.status = sample.status;
    card.classList.toggle("is-rejected", sample.status === "rejected");
    card.querySelectorAll(".choice-btn").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.choice === sample.reaction));
    });
  }

  async function onSampleReactionClick(event) {
    const button = event.target.closest(".choice-btn");
    const card = button && button.closest(".sample-card");
    if (!button || !card) return;

    const requested = button.dataset.choice;
    const next = card.dataset.reaction === requested ? "none" : requested;
    const commentField = card.querySelector("[data-sample-comment]");
    const comment = commentField ? commentField.value : "";

    const sample = await postSampleReaction(card.dataset.sampleId, next, comment);
    if (sample) applySampleReaction(card, sample);
  }

  async function onSampleCommentBlur(event) {
    const field = event.target;
    if (!field.matches || !field.matches("[data-sample-comment]")) return;
    const card = field.closest(".sample-card");
    if (!card) return;
    await postSampleReaction(card.dataset.sampleId, card.dataset.reaction, field.value);
  }

  async function pollSession(sessionId) {
    // The status line and an auto run's decisions follow every tick. The photos, the layouts
    // and the status pill are drawn by the server, so the page is drawn again when the
    // progress differs from the first tick's, and when the run is over.
    let first = null;
    for (;;) {
      await sleep(1500);
      let response;
      try {
        response = await fetch(`/api/sessions/${sessionId}`);
      } catch {
        continue; // a transient fetch error; try again on the next tick
      }
      if (!response.ok) continue;
      const data = await response.json();
      // The run's latest step, then, during an auto run, what its stage in flight is doing.
      const note = [data.step_note, data.child_note].filter(Boolean).join(" · ");
      setText(document.querySelector("[data-status-note]"), note ? ` · ${note}` : "");
      if (data.auto_status) setText(document.querySelector("[data-status-text]"), data.auto_status);
      renderAutoDecisions(data.auto_state);
      const progress = sessionProgress(data);
      if (first === null) first = progress;
      if (data.run && data.run.status === "running" && progress === first) continue;
      // Drawing the page again would drop a sample comment being typed; the comment is saved
      // when its field loses focus, and the page is drawn again on a later tick.
      if (sampleCommentHasFocus()) continue;
      window.location.reload();
      return;
    }
  }

  function sampleCommentHasFocus() {
    const active = document.activeElement;
    return Boolean(active && active.matches && active.matches("[data-sample-comment]"));
  }

  // What the page draws from the server, as one value to compare between ticks: the rounds,
  // the photos, the picked photo's layouts, the session's status and the decisions so far.
  function sessionProgress(data) {
    const decisions = data.auto_state ? (data.auto_state.decisions || []).length : 0;
    return JSON.stringify([data.rounds, data.samples, data.candidates, data.session.status, decisions]);
  }

  // An auto run adds a decision line as it goes; the list is rebuilt from the
  // session's auto state on every poll.
  function renderAutoDecisions(autoState) {
    const list = document.querySelector("[data-auto-decisions]");
    if (!list || !autoState) return;
    const lines = autoState.decisions || [];
    list.replaceChildren(
      ...lines.map((line) => {
        const item = document.createElement("li");
        item.textContent = line;
        return item;
      }),
    );
    list.hidden = lines.length === 0;
  }

  // The parameters each template actually reads (see studio/render/compose.py's
  // _key); a select for anything else does nothing for that template, so it is
  // disabled rather than left to suggest a choice that has no effect.
  const TEMPLATE_ENABLED_FIELDS = {
    hero: ["logo_position"],
    full_bleed: ["logo_position", "text_position", "text_align", "scrim"],
    split: ["logo_position", "text_align", "photo_side"],
    corner: ["logo_position"],
    caption_strip: ["logo_position", "scrim"],
    type_only: ["logo_position"],
  };

  function applyAdjustRules(form) {
    const templateField = form.querySelector('[data-adjust-field="template"]');
    if (!templateField) return;
    const enabled = TEMPLATE_ENABLED_FIELDS[templateField.value] || [];
    form.querySelectorAll("[data-adjust-field]").forEach((field) => {
      const name = field.dataset.adjustField;
      if (name === "template") return;
      field.disabled = !enabled.includes(name);
    });
  }

  function initAdjustForms() {
    document.querySelectorAll(".adjust-form").forEach((form) => {
      applyAdjustRules(form);
      const templateField = form.querySelector('[data-adjust-field="template"]');
      if (templateField) templateField.addEventListener("change", () => applyAdjustRules(form));
    });

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-adjust-toggle]");
      if (!button) return;
      const form = document.getElementById(button.dataset.adjustToggle);
      if (form) form.hidden = !form.hidden;
    });
  }

  // The Directions part: "Change direction" opens the folded choices, "Edit and use" turns a
  // card's fields into its form's inputs (and "Cancel" back), and a source chip opens the
  // Research part, folded once a direction is chosen, so the link lands on its source.
  function initDirections() {
    document.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-directions-toggle]");
      if (toggle) {
        const choices = document.getElementById(toggle.dataset.directionsToggle);
        if (!choices) return;
        choices.hidden = !choices.hidden;
        toggle.setAttribute("aria-expanded", String(!choices.hidden));
        if (!choices.hidden) openResearch();
        return;
      }
      const edit = event.target.closest("[data-direction-edit]");
      if (edit) {
        showDirectionForm(edit.closest("[data-direction-card]"), true);
        return;
      }
      const cancel = event.target.closest("[data-direction-cancel]");
      if (cancel) {
        showDirectionForm(cancel.closest("[data-direction-card]"), false);
        return;
      }
      if (event.target.closest(".source-chip")) openResearch();
    });
  }

  function showDirectionForm(card, editing) {
    const view = card && card.querySelector("[data-direction-view]");
    const form = card && card.querySelector("[data-direction-form]");
    if (!view || !form) return;
    view.hidden = editing;
    form.hidden = !editing;
    const focusTarget = editing ? form.querySelector("input, select, textarea") : view.querySelector("[data-direction-edit]");
    if (focusTarget) focusTarget.focus();
  }

  function openResearch() {
    const research = document.getElementById("research");
    if (research && research.tagName === "DETAILS") research.open = true;
  }

  // ----------------------------------------------------------- archive page

  function initArchivePage() {
    const selectButton = document.querySelector("[data-confirm-selected]");
    if (selectButton) {
      selectButton.addEventListener("click", (event) => {
        const count = document.querySelectorAll('#delete-selected-form input[name="ids"]:checked').length;
        const label = count === 1 ? "1 photo" : `${count} photos`;
        if (count === 0 || !window.confirm(`Delete ${label}?`)) {
          event.preventDefault();
        }
      });
    }

    const dislikedButton = document.querySelector("[data-confirm-disliked]");
    if (dislikedButton) {
      dislikedButton.addEventListener("click", (event) => {
        const total = Number(dislikedButton.dataset.confirmDisliked || "0");
        const label = total === 1 ? "1 disliked photo" : `${total} disliked photos`;
        if (total === 0 || !window.confirm(`Delete ${label}?`)) {
          event.preventDefault();
        }
      });
    }
  }

  // ------------------------------------------------------------ post page

  function initPostPage() {
    const button = document.querySelector("[data-copy-caption]");
    const text = document.querySelector("[data-caption-text]");
    if (!button || !text) return;

    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text.textContent.trim());
      } catch {
        return; // clipboard access can be blocked; the caption is still there to select by hand
      }
      const original = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => {
        button.textContent = original;
      }, 1500);
    });
  }

  initTheme();
  initBar();
  initStudioPage();
  initLibrary();
  initRunPage();
  initSessionPage();
  initArchivePage();
  initPostPage();
})();
