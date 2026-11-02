(() => {
  'use strict';

  const status = document.getElementById('connection');
  const statusLabel = document.getElementById('connection-label');
  const toast = document.getElementById('toast');
  const modifierStatus = document.getElementById('modifier-status');
  const params = new URLSearchParams(location.search);
  const suppliedToken = params.get('token') || '';

  if (suppliedToken) {
    try { localStorage.setItem('phone-pc-keyboard-token', suppliedToken); } catch {}
  }
  let token = suppliedToken;
  if (!token) {
    try { token = localStorage.getItem('phone-pc-keyboard-token') || ''; } catch {}
  }

  // Each page load gets its own ID namespace. Commands retain their IDs while
  // reconnecting so the server can safely de-duplicate a retry.
  const pageId = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let sequence = 0;
  const pending = new Map();
  const MAX_PENDING = 250;
  const RETRY_AFTER_MS = 1800;
  const MAX_RETRIES = 4;

  let ws = null;
  let socketGeneration = 0;
  let retryDelay = 700;
  let reconnectTimer = null;
  let pingTimer = null;
  let retryTimer = null;
  let toastTimer = null;
  let roundTripMs = null;
  const modifiers = new Set();
  const modifierOrder = ['ctrl', 'shift', 'alt', 'win'];
  const modifierLabels = { ctrl: 'Ctrl', shift: 'Shift', alt: 'Alt', win: 'Win' };

  function setStatus(state, label) {
    status.dataset.state = state;
    statusLabel.textContent = label;
    document.querySelectorAll('button[data-key], button[data-hotkey], button[data-modifier]').forEach(button => {
      button.disabled = state !== 'online';
    });
  }

  function updateOnlineLabel() {
    if (status.dataset.state === 'online') {
      statusLabel.textContent = roundTripMs === null ? 'На связи' : `На связи · ${roundTripMs} мс`;
    }
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
  }

  function queueCommand(payload) {
    if (pending.size >= MAX_PENDING) {
      showToast('Слишком много команд в очереди. Проверь связь с компьютером.');
      return false;
    }
    const id = `${pageId}:${++sequence}`;
    pending.set(id, {
      packet: { ...payload, id },
      sentGeneration: 0,
      lastSentAt: 0,
      attempts: 0,
    });
    flushQueue();
    return true;
  }

  function flushQueue() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const now = Date.now();
    for (const [id, entry] of pending) {
      const firstSendOnThisSocket = entry.sentGeneration !== socketGeneration;
      const retryAge = now - entry.lastSentAt;
      const retryDue = !firstSendOnThisSocket
        && entry.attempts <= MAX_RETRIES
        && retryAge >= RETRY_AFTER_MS;
      if (!firstSendOnThisSocket && entry.attempts > MAX_RETRIES && retryAge >= RETRY_AFTER_MS * 2) {
        // A half-open TCP connection can remain OPEN in WebKit. Force a fresh
        // connection so pending commands are replayed and server-deduplicated.
        try { ws.close(); } catch {}
        return;
      }
      if (!firstSendOnThisSocket && !retryDue) continue;
      if (ws.bufferedAmount > 32768) break;
      try {
        ws.send(JSON.stringify(entry.packet));
        entry.sentGeneration = socketGeneration;
        entry.lastSentAt = now;
        entry.attempts += 1;
      } catch {
        break;
      }
    }
  }

  function renderModifiers() {
    document.querySelectorAll('[data-modifier]').forEach(button => {
      const active = modifiers.has(button.dataset.modifier);
      button.classList.toggle('selected', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    const selected = modifierOrder.filter(name => modifiers.has(name)).map(name => modifierLabels[name]);
    modifierStatus.textContent = selected.length
      ? `Готово к следующей клавише: ${selected.join(' + ')}`
      : 'Модификаторы не выбраны';
  }

  function clearModifiers() {
    modifiers.clear();
    renderModifiers();
  }

  function pressKey(name) {
    const selected = modifierOrder.filter(modifier => modifiers.has(modifier));
    const accepted = selected.length
      ? queueCommand({ type: 'hotkey', keys: [...selected, name] })
      : queueCommand({ type: 'key', key: name });
    if (accepted && selected.length) clearModifiers();
  }

  function pressShortcut(button) {
    const keys = button.dataset.hotkey.split(',').filter(Boolean);
    if (queueCommand({ type: 'hotkey', keys })) clearModifiers();
  }

  function connect() {
    if (!token) {
      setStatus('offline', 'Нет кода');
      showToast('Открой ссылку с кодом подключения из программы на ПК');
      return;
    }
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${scheme}//${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws = socket;
    socketGeneration += 1;
    const thisGeneration = socketGeneration;
    setStatus('connecting', 'Подключение…');

    socket.addEventListener('open', () => {
      retryDelay = 700;
      flushQueue();
    });

    socket.addEventListener('message', event => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      if (data.type === 'connected') {
        setStatus('online', 'На связи');
        flushQueue();
        return;
      }
      if (data.type === 'ack' || data.type === 'error') {
        if (typeof data.id === 'string') pending.delete(data.id);
        if (data.type === 'error') showToast(data.message || 'Не удалось выполнить клавишу');
        return;
      }
      if (data.type === 'pong' && typeof data.sent === 'number') {
        roundTripMs = Math.max(0, Math.round(performance.now() - data.sent));
        updateOnlineLabel();
      }
    });

    socket.addEventListener('close', () => {
      if (ws !== socket) return;
      ws = null;
      setStatus('offline', 'Нет связи');
      clearInterval(pingTimer);
      clearInterval(retryTimer);
      for (const entry of pending.values()) {
        entry.sentGeneration = 0;
        entry.attempts = 0;
        entry.lastSentAt = 0;
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(6000, Math.round(retryDelay * 1.7));
    });

    socket.addEventListener('error', () => {
      if (ws === socket) setStatus('offline', 'Ошибка связи');
    });

    // Application-level RTT indicator and a lightweight recovery mechanism.
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws === socket && socket.readyState === WebSocket.OPEN) {
        try { socket.send(JSON.stringify({ type: 'ping', sent: performance.now() })); } catch {}
      }
    }, 5000);

    clearInterval(retryTimer);
    retryTimer = setInterval(() => {
      if (ws === socket && socket.readyState === WebSocket.OPEN) flushQueue();
    }, 250);

    // Keep this generation reference alive for debuggers and older WebKit.
    void thisGeneration;
  }

  function attachFastTap(button, callback) {
    // pointerdown fires immediately on iOS, unlike the historical delayed click.
    // preventDefault also avoids synthetic duplicate clicks and accidental zoom.
    if ('PointerEvent' in window) {
      button.addEventListener('pointerdown', event => {
        if (event.pointerType === 'mouse' && event.button !== 0) return;
        event.preventDefault();
        callback();
      }, { passive: false });
    } else if ('ontouchstart' in window) {
      button.addEventListener('touchstart', event => {
        event.preventDefault();
        callback();
      }, { passive: false });
    } else {
      button.addEventListener('click', callback);
    }
  }

  document.querySelectorAll('button[data-layout]').forEach(button => {
    button.addEventListener('click', () => {
      document.querySelectorAll('button[data-layout]').forEach(tab => {
        const selected = tab === button;
        tab.classList.toggle('active', selected);
        tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      });
      document.querySelectorAll('.keyboard-layout').forEach(panel => {
        const selected = panel.id === `layout-${button.dataset.layout}`;
        panel.classList.toggle('active', selected);
        panel.hidden = !selected;
      });
    });
  });

  document.querySelectorAll('button[data-modifier]').forEach(button => {
    attachFastTap(button, () => {
      const name = button.dataset.modifier;
      if (modifiers.has(name)) modifiers.delete(name);
      else modifiers.add(name);
      renderModifiers();
    });
  });

  document.querySelectorAll('button[data-key]').forEach(button => {
    attachFastTap(button, () => pressKey(button.dataset.key));
  });

  document.querySelectorAll('button[data-hotkey]').forEach(button => {
    attachFastTap(button, () => pressShortcut(button));
  });

  // A context menu on a key can swallow the next touch in iOS Safari.
  document.addEventListener('contextmenu', event => {
    if (event.target.closest && event.target.closest('button')) event.preventDefault();
  });

  // Wake Lock is available on some mobile browsers; graceful fallback elsewhere.
  let wakeLock = null;
  async function requestWakeLock() {
    if (!('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch { wakeLock = null; }
  }
  requestWakeLock();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') requestWakeLock();
  });

  renderModifiers();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  connect();
})();
