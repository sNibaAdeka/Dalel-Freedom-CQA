/* Call playback uses server-provided recording offsets; no transcript timing is inferred here. */
const mounted = new WeakMap();
const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
const seconds = value => finite(value) && Number(value) >= 0 ? Number(value) : null;
const clock = value => {
  const n = Math.floor(Math.max(0, Number(value) || 0));
  return `${Math.floor(n / 60).toString().padStart(2, '0')}:${(n % 60).toString().padStart(2, '0')}`;
};
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function stylesheet() {
  const href = new URL('../css/audio.css', import.meta.url).href;
  if (!document.querySelector('link[data-cqa-style="audio"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = href; link.dataset.cqaStyle = 'audio';
    document.head.append(link);
  }
}
function mediaSource(src) {
  if (typeof src !== 'string' || !src.trim()) return null;
  try {
    const url = new URL(src, document.baseURI);
    return ['http:', 'https:', 'blob:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function mount(CQA = window.CQA) {
  if (!CQA?.bus || !CQA?.api) return () => {};
  if (mounted.has(CQA)) return mounted.get(CQA);
  stylesheet();
  let detail = null, session = 0, audio = null, view = null;
  let payload = null, pending = null, refreshUsed = false, requesting = false;

  function stop() {
    if (audio) {
      audio.pause(); audio.removeAttribute('src'); audio.load();
      audio = null;
    }
  }
  function alive(token) {
    return token === session && view?.host.isConnected &&
      (!CQA.state?.activeConversationId || CQA.state.activeConversationId === detail?.id);
  }
  function unavailable(text = 'Запись недоступна. Доказательства можно проверить в транскрипте.') {
    if (!view) return;
    view.host.dataset.state = 'unavailable';
    view.status.textContent = text;
    view.media.hidden = true;
    view.rate.disabled = true;
    if (pending) showCue(pending);
  }
  function showCue(request) {
    if (!view || !detail) return null;
    const message = detail.messages?.find(item => item.id === request.message_id);
    const offset = seconds(request.rec_sec) ?? seconds(payload?.offsets?.[request.message_id]) ?? seconds(message?.rec_sec);
    const label = offset !== null ? clock(offset) : message?.ts_raw || 'время не указано';
    view.cue.hidden = false;
    view.cue.textContent = `Реплика: ${label}${offset !== null ? ' · начало на 2 с раньше' : ''}`;
    CQA.bus.emit('audio:position', {conv_id: detail.id, message_id: request.message_id, rec_sec: offset});
    return offset;
  }
  async function playPending(token) {
    if (!alive(token) || !audio || !pending) return;
    const offset = showCue(pending);
    if (offset === null) {
      view.status.textContent = 'Для этой реплики нет времени в записи. Используйте транскрипт.';
      pending = null;
      return;
    }
    if (!audio.getAttribute('src') || audio.readyState < 1) return;
    const desired = Math.max(0, offset - 2);
    const end = finite(audio.duration) ? Math.max(0, audio.duration - 0.05) : desired;
    try { audio.currentTime = Math.min(desired, end); } catch { return; }
    pending = null;
    try {
      await audio.play();
      if (alive(token)) view.status.textContent = 'Воспроизведение с запасом 2 с перед репликой.';
    } catch {
      if (alive(token)) view.status.textContent = 'Фрагмент выбран. Нажмите «Воспроизвести» в плеере.';
    }
  }
  async function requestAudio(token, refresh = false) {
    if (!alive(token) || requesting) return;
    requesting = true;
    view.host.dataset.state = 'loading';
    view.status.textContent = refresh ? 'Обновляем ссылку на запись…' : 'Загружаем запись…';
    try {
      const response = await CQA.api.audio(detail.id);
      if (!alive(token)) return;
      payload = response || {};
      const src = mediaSource(payload.src);
      if (pending) showCue(pending);
      if (!src) { unavailable(); return; }
      view.media.hidden = false;
      view.rate.disabled = false;
      view.host.dataset.state = 'ready';
      view.status.textContent = 'Выберите доказательство, чтобы перейти к нужной реплике.';
      audio.src = src;
      audio.load();
    } catch {
      if (alive(token)) unavailable('Не удалось получить запись. Время и текст реплики остаются доступны.');
    } finally {
      if (token === session) requesting = false;
    }
  }
  function render(next) {
    session += 1;
    stop();
    detail = next; payload = null; pending = null; refreshUsed = false; requesting = false;
    const host = document.getElementById('cqa-audio');
    view = null;
    if (!host) return;
    host.replaceChildren();
    const isCall = next?.channel === 'call' || next?.channel === 'voice call';
    host.hidden = !isCall;
    if (!isCall) return;
    host.classList.add('cqa-audio');
    const token = session;
    const heading = element('div', 'cqa-audio__heading');
    const title = element('h3', '', 'Запись звонка');
    const hint = element('span', 'cqa-audio__hint', 'Запас 2 с');
    heading.append(title, hint);
    const status = element('p', 'cqa-audio__status');
    status.setAttribute('role', 'status');
    const cue = element('p', 'cqa-audio__cue'); cue.hidden = true;
    const media = element('div', 'cqa-audio__media'); media.hidden = true;
    audio = document.createElement('audio');
    audio.controls = true; audio.preload = 'auto';
    audio.setAttribute('aria-label', 'Запись звонка: воспроизведение и перемотка');
    const label = element('label', 'cqa-audio__speed', 'Скорость');
    const rate = document.createElement('select');
    rate.setAttribute('aria-label', 'Скорость воспроизведения');
    rate.append(new Option('1×', '1'), new Option('1,5×', '1.5'));
    rate.disabled = true; label.append(rate);
    rate.addEventListener('change', () => { if (audio) audio.playbackRate = Number(rate.value); });
    audio.addEventListener('loadedmetadata', () => {
      if (!alive(token)) return;
      audio.playbackRate = Number(rate.value);
      void playPending(token);
    });
    audio.addEventListener('error', () => {
      if (!alive(token)) return;
      if (!refreshUsed && audio.getAttribute('src')) {
        refreshUsed = true;
        if (!pending && finite(audio.currentTime)) {
          pending = {message_id: '__resume', rec_sec: audio.currentTime + 2};
        }
        void requestAudio(token, true);
      } else unavailable('Запись не воспроизводится. Проверьте доказательство по тексту и времени реплики.');
    });
    media.append(audio, label);
    host.append(heading, media, cue, status);
    view = {host, status, cue, media, rate};
    void requestAudio(token);
  }
  const unsubs = [
    CQA.bus.on('conv:loaded', render),
    CQA.bus.on('conv:open', id => {
      const target = typeof id === 'string' ? id : id?.id;
      if (detail && target && target !== detail.id) {
        session += 1; stop(); requesting = false; pending = null;
      }
    }),
    CQA.bus.on('audio:seek', request => {
      if (!detail || request?.conv_id !== detail.id || !request.message_id) return;
      pending = request; showCue(request);
      if (view?.host.dataset.state === 'unavailable') return;
      void playPending(session);
    })
  ];
  const cleanup = () => {
    session += 1; stop();
    unsubs.forEach(fn => { if (typeof fn === 'function') fn(); });
    mounted.delete(CQA);
  };
  mounted.set(CQA, cleanup);
  const current = CQA.state?.conversation || CQA.state?.currentConversation;
  if (current) render(current);
  return cleanup;
}
