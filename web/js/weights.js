/* Visualize the score returned by the engine; this module never computes the official total. */
const mounted = new WeakMap();
const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : 0;
const nonnegative = value => Math.max(0, number(value));
const format = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(number(value));
function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function stylesheet() {
  if (document.querySelector('link[data-cqa-style="weights"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = new URL('../css/weights.css', import.meta.url).href;
  link.dataset.cqaStyle = 'weights'; document.head.append(link);
}

export function mount(CQA = window.CQA) {
  if (!CQA?.bus) return () => {};
  if (mounted.has(CQA)) return mounted.get(CQA);
  stylesheet();
  let detail = null, effective = true, animations = [], ghosts = [];
  function stopMotion() { animations.forEach(animation => animation.cancel()); animations = []; ghosts.forEach(node => node.remove()); ghosts = []; }
  function render(next, preserveMode = false) {
    if (!next) return;
    stopMotion(); detail = next;
    if (!preserveMode) effective = true;
    const host = document.getElementById('cqa-weights');
    if (!host) return;
    host.classList.add('cqa-weights'); host.replaceChildren();
    const criteria = new Map((next.checklist?.criteria || []).map(item => [item.id, item]));
    const lines = (Array.isArray(next.score?.lines) ? next.score.lines : []).filter(line => !criteria.get(line.criterion_id)?.critical && nonnegative(line.weight) > 0);
    const title = element('h3', '', 'Вес критериев');
    if (!lines.length) {
      host.append(title, element('p', 'cqa-weights__note', next.score?.status === 'no_applicable' || next.score?.lines?.length ? 'Нет критериев с весом. Итог по весам не рассчитывается.' : 'Лента появится после оценки диалога.'));
      return;
    }
    const isNA = line => line.is_na === true || line.answer === 'na';
    const totalWeight = lines.reduce((sum, line) => sum + nonnegative(line.weight), 0);
    const totalEffective = lines.reduce((sum, line) => sum + (isNA(line) ? 0 : nonnegative(line.weight_effective)), 0);
    const excluded = lines.filter(isNA).reduce((sum, line) => sum + nonnegative(line.weight), 0);
    const applicable = lines.filter(line => !isNA(line) && nonnegative(line.weight) > 0).length;
    const excludedPercent = totalWeight > 0 ? excluded / totalWeight * 100 : 0;
    const unit = totalEffective > 0 && totalEffective <= 1.00001 ? 100 : 1;
    const heading = element('div', 'cqa-weights__heading');
    const toggle = element('button', 'cqa-weights__toggle');
    toggle.type = 'button';
    toggle.disabled = excluded === 0 || applicable === 0 || totalEffective === 0;
    heading.append(title, toggle);
    const bar = element('div', 'cqa-weights__bar');
    bar.setAttribute('aria-hidden', 'true');
    const caption = element('p', 'cqa-weights__note');
    caption.setAttribute('role', 'status');
    if (applicable === 0) caption.textContent = 'Нет применимых критериев. Итог по весам не рассчитывается.';
    else if (totalEffective === 0) caption.textContent = 'Эффективные веса пока не рассчитаны.';
    else if (!excluded) caption.textContent = 'Все критерии применимы. Перераспределение не требуется.';
    else caption.textContent = `${format(excludedPercent)}% исходного веса распределено пропорционально между ${applicable} применимыми критериями.`;
    const legend = element('ul', 'cqa-weights__legend');
    const segments = lines.map((line, index) => {
      const na = isNA(line), max = nonnegative(line.max_points);
      const ratio = max > 0 ? Math.min(1, Math.max(0, number(line.points) / max)) : 0;
      const verdict = na ? 'na' : ratio >= 1 ? 'yes' : ratio > 0 ? 'partial' : 'no';
      const name = criteria.get(line.criterion_id)?.title || line.title || line.criterion_id;
      const segment = element('div', 'cqa-weights__segment');
      segment.dataset.verdict = verdict; segment.dataset.criterionId = line.criterion_id;
      const fill = element('span', 'cqa-weights__fill');
      fill.style.transform = `scaleX(${na ? 1 : ratio})`;
      const label = element('span', 'cqa-weights__segment-label', String(index + 1));
      segment.append(fill, label); bar.append(segment);
      const entry = element('li');
      const button = element('button', 'cqa-weights__criterion');
      button.type = 'button'; button.dataset.verdict = verdict;
      const marker = element('span', 'cqa-weights__marker', String(index + 1));
      const body = element('span', 'cqa-weights__criterion-body');
      const nameNode = element('span', 'cqa-weights__criterion-name', String(name));
      const values = element('span', 'cqa-weights__criterion-values',
        `Вес ${format(line.weight)} → ${na ? 'N/A' : format(number(line.weight_effective) * unit) + '%'} · ${na ? 'не применимо' : format(line.points) + '/' + format(line.max_points) + ' баллов'}`);
      body.append(nameNode, values); button.append(marker, body);
      button.addEventListener('click', () => CQA.bus.emit('criterion:focus', {conv_id: detail.id, criterion_id: line.criterion_id}));
      entry.append(button); legend.append(entry);
      return {segment, line};
    });
    const formulas = element('details', 'cqa-weights__formula');
    formulas.append(element('summary', '', 'Как распределён вес'));
    const steps = element('ol');
    const formulaSteps = Array.isArray(next.score?.formula_steps) ? next.score.formula_steps : [];
    formulaSteps.forEach(step => steps.append(element('li', '', String(step))));
    if (!formulaSteps.length) steps.append(element('li', '', 'Подробная формула не получена. Показаны веса из результата оценки.'));
    formulas.append(steps);
    host.append(heading, bar, caption, legend, formulas);
    function layout(animate = false) {
      stopMotion();
      const previous = segments.map(({segment}) => segment.getBoundingClientRect());
      toggle.textContent = effective ? 'Показать до N/A' : 'Показать после N/A';
      toggle.setAttribute('aria-pressed', String(!effective));
      bar.dataset.mode = effective ? 'effective' : 'original';
      for (const {segment, line} of segments) {
        const fraction = effective
          ? (totalEffective > 0 && !isNA(line) ? nonnegative(line.weight_effective) / totalEffective : 0)
          : (totalWeight > 0 ? nonnegative(line.weight) / totalWeight : 0);
        segment.style.width = `${fraction * 100}%`;
        segment.style.opacity = fraction > 0 ? '1' : '0';
      }
      if (!animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches || !Element.prototype.animate) return;
      segments.forEach(({segment}, i) => {
        const before = previous[i], after = segment.getBoundingClientRect();
        if (after.width > .1) {
          const animation = segment.animate([
            {transform: `translateX(${before.left - after.left}px) scaleX(${before.width / after.width})`, opacity: before.width > .1 ? 1 : 0},
            {transform: 'translateX(0) scaleX(1)', opacity: 1}
          ], {duration: 300, easing: 'cubic-bezier(.22,1,.36,1)'});
          animations.push(animation);
        } else if (before.width > .1) {
          const ghost = segment.cloneNode(true);
          ghost.classList.add('cqa-weights__ghost');
          ghost.style.position = 'absolute'; ghost.style.top = '0'; ghost.style.bottom = '0';
          ghost.style.left = `${before.left - bar.getBoundingClientRect().left}px`;
          ghost.style.width = `${before.width}px`; ghost.style.opacity = '1';
          bar.append(ghost); ghosts.push(ghost);
          const animation = ghost.animate([{transform:'scaleX(1)',opacity:1},{transform:'scaleX(0)',opacity:0}],
            {duration:180,easing:'ease-in',fill:'forwards'});
          animations.push(animation);
          animation.onfinish = () => ghost.remove();
        }
      });
    }
    toggle.addEventListener('click', () => { effective = !effective; layout(true); });
    layout();
  }
  const unsubs = [
    CQA.bus.on('conv:loaded', next => render(next, detail?.id === next?.id)),
    CQA.bus.on('score:updated', update => {
      if (!detail || !update?.score) return;
      const id = update.conv_id || update.id || update.score.conv_id;
      if (id && id !== detail.id) return;
      render({...detail, ...update, id: detail.id}, true);
    })
  ];
  const cleanup = () => {
    stopMotion(); unsubs.forEach(fn => { if (typeof fn === 'function') fn(); }); mounted.delete(CQA);
  };
  mounted.set(CQA, cleanup);
  const current = CQA.state?.conversation || CQA.state?.currentConversation;
  if (current) render(current);
  return cleanup;
}
