/**
 * Conversation queue. Only CQA.api performs network requests.
 * mount() is idempotent and returns a controller with reload/destroy methods.
 */
const mounted = new WeakMap();
const collator = new Intl.Collator("ru", { numeric: true, sensitivity: "base" });
const number = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });
export function formatScore(value, digits = 2) { return finite(value) ? value.toLocaleString("ru-RU", { maximumFractionDigits: Number.isInteger(digits) ? Math.max(0, Math.min(6, digits)) : 2 }) : "—"; }
const svg = {
  search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.2 7a7 7 0 0 1 11.6-1L20 9M4 15l2.2 3A7 7 0 0 0 18 17"/></svg>',
  call: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 3 3 5-2 2a14 14 0 0 0 6 6l2-2 5 3-1 4C10 22 2 14 3 4z"/></svg>',
  chat: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z"/><path d="M8 8h8M8 12h5"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>',
};
const cellIcons = {
  yes: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
  no: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg>',
  partial: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>',
  critical: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3h8l5 5v8l-5 5H8l-5-5V8zM12 7v6m0 3v1"/></svg>',
  clear: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
};
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}
export function evaluationState(row) {
  if (row.status === "error") return "error";
  if (row.status === "running" || row.status === "evaluating") return "running";
  if (row.status === "no_applicable") return "no_applicable";
  if (!finite(row.total)) return "pending";
  if (row.appealed) return "appealed";
  if (row.critical) return "critical";
  if (row.needs_review) return "review";
  return "approved";
}
/** Read-only fallback: each row keeps the version that actually supplied its score. */
export async function withPreviousScores(rows, versions, api, isStale = () => false) {
  const previous = versions.map(v => Number(v.version)).filter(Number.isInteger).sort((a, b) => b - a);
  let cursor = 0;
  const result = rows.map(row => ({ ...row }));
  await Promise.all(Array.from({ length: Math.min(6, rows.length) }, async () => {
    while (!isStale() && cursor < rows.length) {
      const index = cursor++, row = rows[index];
      if (finite(row.total) || !['pending', 'incomplete'].includes(row.status)) continue;
      for (const version of previous.filter(v => v < Number(row.checklist_version))) {
        if (isStale()) return;
        try {
          const detail = await api.conversation(row.id, version);
          if (isStale()) return;
          if (!finite(detail.score?.total) || detail.score?.status === 'incomplete') continue;
          result[index] = { ...row, total: detail.score.total, critical: Boolean(detail.score.critical),
            needs_review: Boolean(detail.needs_review), status: detail.status,
            appealed: (detail.appeals || []).some(a => a.status === 'open'),
            cells: detail.evals || [], checklist_version: version, previous_score: true };
          break;
        } catch { /* Keep the active-version row when historical data is unavailable. */ }
      }
    }
  }));
  return result;
}
const statusLabels = {
  error: "Ошибка оценки",
  no_applicable: "Нет применимых критериев",
  appealed: "Оспорено",
  running: "Оценивается",
  pending: "Не оценён",
  critical: "Грубая ошибка",
  review: "На проверку",
  approved: "Авто-одобрено",
};
export function cellDescription(cell, index, definition = {}) {
  cell = cell && typeof cell === "object" ? cell : {};
  const criterion = String(cell.criterion_id || `C${index + 1}`);
  const answer = cell.answer;
  let kind = "pending", symbol = "·", label = "Нет оценки";
  if (answer === "yes") [kind, symbol, label] = ["yes", "✓", "Да"];
  else if (answer === "no") [kind, symbol, label] = ["no", "×", "Нет"];
  else if (answer === "partial") [kind, symbol, label] = ["partial", "−", "Частично"];
  else if (answer === "na") [kind, symbol, label] = ["na", "/", "Не применимо"];
  else if (finite(answer)) [kind, symbol, label] = ["numeric", number.format(answer), `Оценка ${number.format(answer)}`];
  const low = finite(cell.confidence) && cell.confidence < .8;
  const review = low || Boolean(cell.needs_review);
  const isFlag = definition.critical === true;
  const hit = isFlag ? answer === (definition.critical_when ?? "yes") : Boolean(cell.critical_hit);
  if (isFlag && answer != null) [kind, symbol, label] = hit ? ["critical", "◆", "Нарушение выявлено"] : ["clear", "—", "Нарушений нет"];
  else if (finite(answer) && Array.isArray(definition.scale) && definition.scale.length) {
    const max = Math.max(...definition.scale.filter(finite));
    kind = answer >= max ? "yes" : answer <= 0 ? "no" : "partial";
    label = `Оценка ${number.format(answer)}/${number.format(max)}`;
  }
  if (hit) [kind, symbol] = ["critical", "◆"];
  const confidence = finite(cell.confidence) ? `, уверенность ${Math.round(Math.max(0, Math.min(1, cell.confidence)) * 100)}%` : "";
  return { kind, symbol, review, label: `${criterion}: ${label}${confidence}${hit ? ", грубая ошибка" : ""}${review ? ", нужна проверка" : ""}` };
}
function attachStyle() {
  if (document.getElementById("cqa-list-styles")) return;
  const link = document.createElement("link");
  link.id = "cqa-list-styles";
  link.rel = "stylesheet";
  link.href = new URL("../css/list.css", import.meta.url).href;
  document.head.append(link);
}
function typingTarget(target) {
  return target instanceof Element && Boolean(target.closest("input, textarea, select, [contenteditable='true'], [role='textbox']"));
}

export function mount(CQA = window.CQA) {
  const host = document.getElementById("cqa-list");
  if (!host) return null;
  if (mounted.has(host)) return mounted.get(host);
  if (!CQA?.api?.conversations || !CQA?.bus?.on || !CQA?.bus?.emit) {
    throw new Error("Conversation queue needs CQA.api.conversations and CQA.bus.");
  }
  attachStyle();
  CQA.state ||= {};
  const controller = new AbortController();
  const eventOptions = { signal: controller.signal };
  const cleanups = [];
  const state = {
    rows: [], visible: [], selected: String(CQA.state.activeConversationId || ""),
    filters: { query: "", language: "", channel: "", status: "", sort: "review" },
    showMap: false,
    loading: false, loaded: false, error: false, appReady: Boolean(CQA.state.ready),
    request: 0, initialSelectionDone: false,
  };
  host.innerHTML = `
    <section class="cq-list" aria-label="Список диалогов">
      <div class="cq-list-heading"><div><h2>Диалоги <span class="cq-list-total">—</span></h2></div>
        <button class="cq-list-refresh" type="button" aria-label="Обновить список" title="Обновить список">${svg.refresh}</button>
      </div>
      <div class="cq-list-stats" aria-label="Сводка по всем диалогам">
        <div><span>Оценено</span><strong data-stat="evaluated">—</strong></div>
        <div><span title="Авто-одобрено">Авто</span><strong data-stat="approved">—</strong></div>
        <div><span title="На проверку">Проверка</span><strong data-stat="review">—</strong></div>
        <div><span title="Грубые ошибки">Грубые</span><strong data-stat="critical">—</strong></div>
      </div>
      <form class="cq-list-filters" role="search" aria-label="Фильтры диалогов">
        <label class="cq-list-search"><span class="cq-list-visually-hidden">Найти диалог</span>${svg.search}<input type="search" name="query" placeholder="Найти диалог…" autocomplete="off"></label>
        <details class="cq-list-filter-details"><summary>Фильтры</summary>
        <div class="cq-list-filter-row">
          <label>Язык<select name="language"><option value="">Все языки</option><option value="RU">Русский · RU</option><option value="KZ">Қазақша · KZ</option><option value="EN">English · EN</option></select></label>
          <label>Канал<select name="channel"><option value="">Все каналы</option><option value="chat">Чаты</option><option value="call">Звонки</option></select></label>
        </div>
        <div class="cq-list-filter-row">
          <label>Статус<select name="status"><option value="">Все статусы</option><option value="review">На проверку</option><option value="critical">Грубая ошибка</option><option value="approved">Авто-одобрено</option><option value="pending">Не оценён</option><option value="no_applicable">Нет применимых</option><option value="appealed">Оспорено</option><option value="running">Оценивается</option><option value="error">Ошибка оценки</option></select></label>
          <label>Порядок<select name="sort"><option value="review">Нужна проверка</option><option value="score-asc">Сначала низкий балл</option><option value="score-desc">Сначала высокий балл</option><option value="id">По номеру диалога</option></select></label>
        </div>
        <label class="cq-list-map-toggle"><input type="checkbox" name="showMap">Показывать карту критериев</label>
        </details>
      </form>
      <div class="cq-list-results-heading"><p class="cq-list-count" role="status" aria-live="polite">Загружаем диалоги…</p><button class="cq-list-reset" type="button" hidden>Сбросить</button></div>
      <div class="cq-list-notice" role="status" hidden></div>
      <ul class="cq-list-rows" aria-label="Диалоги" aria-busy="true"></ul>
      <footer class="cq-list-footer" hidden>
        <details class="cq-list-legend"><summary>Как читать карту критериев</summary><div>
          <span><i class="cq-list-cell is-yes" aria-hidden="true">✓</i>Да</span>
          <span><i class="cq-list-cell is-no" aria-hidden="true">×</i>Нет</span>
          <span><i class="cq-list-cell is-partial" aria-hidden="true">−</i>Частично</span>
          <span><i class="cq-list-cell is-na" aria-hidden="true">/</i>N/A</span>
          <span><i class="cq-list-cell is-pending is-uncertain" aria-hidden="true">·</i>Нужна проверка</span>
          <span><i class="cq-list-cell is-critical" aria-hidden="true">◆</i>Грубая ошибка</span>
        </div></details>
        <p class="cq-list-shortcuts"><kbd>J</kbd><kbd>K</kbd> перейти <span>·</span> <kbd>Enter</kbd> открыть</p>
      </footer>
    </section>`;
  const root = host.querySelector(".cq-list");
  const rowsNode = root.querySelector(".cq-list-rows");
  const notice = root.querySelector(".cq-list-notice");
  const form = root.querySelector("form");
  const refreshButton = root.querySelector(".cq-list-refresh");
  const resetButton = root.querySelector(".cq-list-reset");
  const count = root.querySelector(".cq-list-count");
  function subscribe(name, callback) {
    const off = CQA.bus.on(name, callback);
    if (typeof off === "function") cleanups.push(off);
  }
  function selectedButton() {
    return Array.from(rowsNode.querySelectorAll(".cq-list-row")).find(node => node.dataset.id === state.selected);
  }
  function reflectSelection() {
    const tabStop = state.visible.some(row => row.id === state.selected) ? state.selected : state.visible[0]?.id;
    rowsNode.querySelectorAll(".cq-list-row").forEach(node => {
      node.tabIndex = node.dataset.id === tabStop ? 0 : -1;
      const active = node.dataset.id === state.selected;
      node.classList.toggle("is-selected", active);
      if (active) node.setAttribute("aria-current", "true");
      else node.removeAttribute("aria-current");
    });
  }
  function open(id, focus = false) {
    if (!state.rows.some(row => row.id === id)) return;
    state.selected = id;
    CQA.state.activeConversationId = id;
    reflectSelection();
    if (focus) {
      const button = selectedButton();
      button?.focus({ preventScroll: true });
      button?.scrollIntoView({ block: "nearest", behavior: "auto" });
    }
    CQA.bus.emit("conv:open", id);
  }
  function chooseFirstOnce() {
    if (!state.appReady || !state.loaded || state.initialSelectionDone || !state.visible.length) return;
    if (CQA.state.tab && CQA.state.tab !== "dialogues") return;
    state.initialSelectionDone = true;
    const selected = state.visible.find(row => row.id === state.selected) || state.visible[0];
    open(selected.id);
  }
  function summary() {
    const evaluated = state.rows.filter(row => !["pending", "running", "error"].includes(evaluationState(row)));
    const counts = {
      evaluated: evaluated.length,
      approved: evaluated.filter(row => evaluationState(row) === "approved").length,
      review: state.rows.filter(row => row.needs_review || row.critical || row.appealed).length,
      critical: state.rows.filter(row => row.critical).length,
    };
    root.querySelector(".cq-list-total").textContent = state.loaded ? String(state.rows.length) : "—";
    for (const [key, value] of Object.entries(counts)) root.querySelector(`[data-stat="${key}"]`).textContent = state.loaded ? String(value) : "—";
  }
  function updateCount() {
    count.textContent = state.loading && !state.loaded ? "Загружаем диалоги…" : `Показано ${state.visible.length} из ${state.rows.length}`;
    resetButton.hidden = !["query", "language", "channel", "status"].some(key => state.filters[key]);
  }
  function setNotice(message, action, callback) {
    notice.replaceChildren();
    notice.hidden = !message;
    if (!message) return;
    notice.append(element("p", "", message));
    if (action) {
      const button = element("button", "cq-list-action", action);
      button.type = "button";
      button.addEventListener("click", callback, { once: true });
      notice.append(button);
    }
  }
  function rowElement(row) {
    const li = element("li");
    const button = element("button", "cq-list-row");
    button.type = "button";
    button.dataset.id = row.id;
    const status = evaluationState(row);
    const channel = row.channel === "call" ? "Звонок" : "Чат";
    const checklist = CQA.state.checklists?.find(item => item.version === (row.checklist_version ?? CQA.state.checklistVersion));
    const definitions = new Map((checklist?.criteria || []).map(item => [item.id, item]));
    const score = formatScore(row.total, checklist?.rounding?.total);
    button.setAttribute("aria-label", `${row.id}, ${channel}, ${row.language}, ${finite(row.total) ? `оценка ${score} из 100` : statusLabels[status]}, ${statusLabels[status]}`);
    const content = element("span", "cq-list-row-content");
    const top = element("span", "cq-list-row-top");
    const icon = element("span", "cq-list-channel");
    icon.innerHTML = row.channel === "call" ? svg.call : svg.chat;
    icon.append(element("span", "", channel));
    top.append(icon, element("span", "cq-list-language", row.language));
    const id = element("span", "cq-list-id", row.id); id.title = row.id;
    const statusNode = element("span", `cq-list-status is-${status}`);
    statusNode.append(element("span", "cq-list-status-text", statusLabels[status]));
    if (row.critical) {
      const diamond = element('span', 'cq-list-diamond');
      diamond.innerHTML = '<svg viewBox="0 0 24 24" role="img" aria-label="Грубая ошибка"><path d="M12 2 22 12 12 22 2 12Z M12 7v6m0 3v.1"/></svg>';
      statusNode.prepend(diamond);
    }
    content.append(top, id, statusNode);
    const scoreBox = element("span", "cq-list-score");
    scoreBox.classList.toggle('is-previous', Boolean(row.previous_score));
    scoreBox.append(element("strong", "", row.previous_score ? `v${row.checklist_version} · ${score}` : score));
    if (row.previous_score) button.setAttribute('aria-label', `${button.getAttribute('aria-label')}. Предыдущая оценка по v${row.checklist_version}`);
    const main = element("span", "cq-list-row-main");
    main.append(content, scoreBox);
    button.append(main);
    li.append(button);
    if (!state.showMap) return li;
    const map = element("span", "cq-list-map");
    const cells = row.cells?.length ? row.cells : Array.from({ length: 8 }, (_, i) => ({ criterion_id: `C${i + 1}` }));
    const descriptions = [];
    for (const [i, cell] of cells.entries()) {
      const info = cellDescription(cell, i, definitions.get(cell.criterion_id || `C${i + 1}`));
      const square = element("span", `cq-list-cell is-${info.kind}${info.review ? " is-uncertain" : ""}`, info.symbol);
      if (cellIcons[info.kind] && !finite(cell?.answer)) square.innerHTML = cellIcons[info.kind];
      square.title = info.label;
      square.setAttribute("aria-hidden", "true");
      map.append(square);
      descriptions.push(info.label);
    }
    map.setAttribute("role", "img");
    map.setAttribute("aria-label", descriptions.join(". "));
    map.id = `cqa-list-map-${state.rows.findIndex(item => item.id === row.id)}`;
    button.setAttribute("aria-describedby", map.id);
    const footer = element("span", "cq-list-row-bottom");
    footer.append(map);
    button.append(footer);
    return li;
  }
  function applyFilters({ preserveFocus = false } = {}) {
    const focusId = preserveFocus && document.activeElement?.classList.contains("cq-list-row") ? document.activeElement.dataset.id : null;
    const f = state.filters;
    const query = f.query.trim().toLocaleLowerCase("ru");
    state.visible = state.rows.filter(row => {
      if (f.language && row.language !== f.language) return false;
      if (f.channel && row.channel !== f.channel) return false;
      const status = evaluationState(row);
      if (f.status === "review" && !(row.needs_review || row.critical || row.appealed)) return false;
      if (f.status && f.status !== "review" && status !== f.status) return false;
      return !query || [row.id, row.language, row.channel === "call" ? "звонок" : "чат", row.title || ""].join(" ").toLocaleLowerCase("ru").includes(query);
    });
    const severity = row => ({ critical: 0, error: 1, appealed: 2, review: 3, running: 4, pending: 5, no_applicable: 6, approved: 7 })[evaluationState(row)];
    state.visible.sort((a, b) => {
      if (f.sort === "review") {
        const difference = severity(a) - severity(b);
        if (difference) return difference;
      }
      if (f.sort !== "id") {
        const av = finite(a.total), bv = finite(b.total);
        if (av !== bv) return av ? -1 : 1;
        if (av && a.total !== b.total) return f.sort === "score-desc" ? b.total - a.total : a.total - b.total;
      }
      return collator.compare(a.id, b.id);
    });
    rowsNode.replaceChildren(...state.visible.map(rowElement));
    reflectSelection();
    updateCount();
    if (!state.error && !state.loading) {
      if (!state.rows.length) setNotice("Диалогов пока нет. Загрузите набор данных, чтобы начать проверку.");
      else if (!state.visible.length) setNotice("Диалоги не найдены. Измените запрос или сбросьте фильтры.", "Сбросить фильтры", resetFilters);
      else {
        const previous = state.rows.filter(row => row.previous_score).length;
        setNotice(previous ? `Оценки предыдущих версий: ${previous}. Версия указана рядом с баллом; счётчики учитывают показанные оценки.` : '');
      }
    }
    if (focusId) Array.from(rowsNode.querySelectorAll(".cq-list-row")).find(node => node.dataset.id === focusId)?.focus({ preventScroll: true });
    chooseFirstOnce();
  }
  function resetFilters() {
    Object.assign(state.filters, { query: "", language: "", channel: "", status: "", sort: "review" });
    form.reset();
    form.elements.showMap.checked = state.showMap;
    applyFilters();
    form.elements.query.focus();
  }
  async function reload() {
    const request = ++state.request;
    CQA.state.listScoreVersions = {};
    const isStale = () => request !== state.request || controller.signal.aborted;
    state.loading = true;
    state.error = false;
    refreshButton.disabled = true;
    rowsNode.setAttribute("aria-busy", "true");
    setNotice("");
    updateCount();
    if (!state.loaded) {
      rowsNode.replaceChildren(...Array.from({ length: 4 }, () => {
        const item = element("li", "cq-list-skeleton");
        item.setAttribute("aria-hidden", "true");
        item.append(element("span"), element("span"), element("span"));
        return item;
      }));
    }
    try {
      const activeRows = await CQA.api.conversations();
      if (isStale()) return;
      // Checklist failure must not discard a successful authoritative queue response.
      let versions = CQA.state.checklists || [];
      try { if (typeof CQA.api.checklists === 'function') versions = await CQA.api.checklists(); }
      catch { versions = []; }
      if (isStale()) return;
      const result = await withPreviousScores(activeRows, versions, CQA.api, isStale);
      if (isStale()) return;
      if (!Array.isArray(result)) throw new Error("Expected a conversation summary array.");
      const ids = new Set();
      state.rows = result.filter(row => row && typeof row.id === "string" && !ids.has(row.id) && ids.add(row.id)).map(row => ({
        ...row, language: String(row.language || "—").toUpperCase(),
        channel: row.channel === "call" ? "call" : "chat",
        cells: Array.isArray(row.cells) ? row.cells : [],
      }));
      CQA.state.listScoreVersions = Object.fromEntries(state.rows.filter(row => row.previous_score).map(row => [row.id, row.checklist_version]));
      state.loaded = true;
      state.loading = false;
      summary();
      applyFilters({ preserveFocus: true });
    } catch {
      if (request !== state.request || controller.signal.aborted) return;
      state.error = true;
      state.loading = false;
      rowsNode.replaceChildren(...state.visible.map(rowElement));
      reflectSelection();
      updateCount();
      setNotice(state.loaded ? "Список не обновился. Показаны ранее загруженные диалоги." : "Не удалось загрузить диалоги. Попробуйте ещё раз.", "Повторить", reload);
    } finally {
      if (request === state.request && !controller.signal.aborted) {
        refreshButton.disabled = false;
        rowsNode.setAttribute("aria-busy", "false");
      }
    }
  }
  form.addEventListener("submit", event => event.preventDefault(), eventOptions);
  form.addEventListener("input", event => {
    const name = event.target.name;
    if (!(name in state.filters)) return;
    state.filters[name] = event.target.value;
    applyFilters();
  }, eventOptions);
  // Select change also supports browsers that do not emit input on selection.
  form.addEventListener("change", event => {
    const name = event.target.name;
    if (name === "showMap") {
      state.showMap = event.target.checked;
      root.querySelector(".cq-list-footer").hidden = !state.showMap;
      applyFilters();
      return;
    }
    if (!(name in state.filters) || state.filters[name] === event.target.value) return;
    state.filters[name] = event.target.value;
    applyFilters();
  }, eventOptions);
  rowsNode.addEventListener("click", event => {
    const row = event.target.closest(".cq-list-row");
    if (row) open(row.dataset.id);
  }, eventOptions);
  refreshButton.addEventListener("click", reload, eventOptions);
  resetButton.addEventListener("click", resetFilters, eventOptions);
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || typingTarget(event.target) || !host.getClientRects().length) return;
    const key = ({KeyJ:"j", KeyK:"k"})[event.code] || (typeof event.key === "string" ? event.key.toLowerCase() : "");
    const inRow = event.target instanceof Element && event.target.closest(".cq-list-row");
    const direction = key === "j" || (inRow && key === "arrowdown") ? 1 : key === "k" || (inRow && key === "arrowup") ? -1 : 0;
    if (!direction || !state.visible.length) return;
    event.preventDefault();
    const current = state.visible.findIndex(row => row.id === state.selected);
    const next = current < 0 ? (direction > 0 ? 0 : state.visible.length - 1) : Math.max(0, Math.min(state.visible.length - 1, current + direction));
    open(state.visible[next].id, true);
  }, eventOptions);
  subscribe("tab:changed", tab => { if (tab === "dialogues") chooseFirstOnce(); });
  subscribe("app:ready", () => { state.appReady = true; applyFilters(); chooseFirstOnce(); });
  subscribe("checklist:selected", () => applyFilters({ preserveFocus: true }));
  subscribe("checklists:updated", () => applyFilters({ preserveFocus: true }));
  subscribe("conv:open", payload => {
    const id = typeof payload === "string" ? payload : payload?.id;
    if (!id) return;
    state.selected = id;
    CQA.state.activeConversationId = id;
    reflectSelection();
  });
  subscribe("score:updated", payload => {
    const id = payload?.conv_id || payload?.id || payload?.score?.conv_id;
    if (CQA.state.listScoreVersions) delete CQA.state.listScoreVersions[id];
    const row = state.rows.find(item => item.id === id);
    if (!row) return;
    // A different version must first be reconciled with the server's active list.
    // Do not turn a labelled historical score into an unlabelled optimistic result.
    const version = payload.checklist?.version ?? payload.score?.checklist_version;
    if ((version == null && row.previous_score) || (version != null && Number(version) !== Number(row.checklist_version))) {
      if (!state.loading) void reload();
      return;
    }
    const score = payload.score || payload;
    if ("total" in score) row.total = score.total;
    if ("critical" in score) row.critical = Boolean(score.critical);
    if ("needs_review" in payload) row.needs_review = Boolean(payload.needs_review);
    if (score.status) row.status = score.status;
    if (payload.checklist?.version) row.checklist_version = payload.checklist.version;
    if (payload.checklist?.active === true || (version != null && Number(version) === Number(CQA.state.activeChecklistVersion))) row.previous_score = false;
    if (Array.isArray(payload.appeals)) row.appealed = payload.appeals.some(item => item.status === "open");
    const cells = Array.isArray(score.lines) ? score.lines : payload.evals;
    if (Array.isArray(cells)) row.cells = cells.map(item => {
      const original = payload.evals?.find(e => e.criterion_id === item.criterion_id) || row.cells.find(e => e.criterion_id === item.criterion_id) || {};
      return { ...original, ...item, criterion_id: item.criterion_id, answer: item.answer };
    });
    summary();
    applyFilters({ preserveFocus: true });
  });
  subscribe("conversations:updated", reload);
  subscribe("conversations:refresh", reload);
  subscribe("checklist:activated", reload);
  const api = {
    reload,
    getVisibleIds: () => state.visible.map(row => row.id),
    destroy() {
      ++state.request;
      controller.abort();
      cleanups.forEach(off => off());
      mounted.delete(host);
      host.replaceChildren();
    },
  };
  mounted.set(host, api);
  void reload();
  return api;
}
