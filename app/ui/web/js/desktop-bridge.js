// Big PDF Desktop Bridge — QWebChannel wrapper.
// Reemplaza fetch('/api/...') por llamadas directas a Python.
//
// Operaciones pesadas:
//   - import/compress usan QThreadPool;
//   - exportWorkspace usa un proceso independiente cancelable.
// El slot devuelve {job_id, async:true}; las señales Qt resuelven la Promise.

// ─── Response parsing ─────────────────────────────────────────────────────────

function _parseResponse(raw) {
  let data = raw;
  if (typeof raw === 'string') {
    try { data = JSON.parse(raw); } catch (e) { throw new Error(raw || 'Respuesta inválida del bridge.'); }
  }
  if (!data || typeof data !== 'object') throw new Error('Respuesta inválida del bridge.');
  if (data.ok === false) throw new Error(data.detail || 'Error en Big PDF.');
  return data;
}

// ─── Sync call (para slots rápidos) ──────────────────────────────────────────

function _call(method, payload = {}) {
  return window.bigpdfReady.then(() => new Promise((resolve, reject) => {
    try {
      const fn = window.bigpdfBridge?.[method];
      if (typeof fn !== 'function') {
        reject(new Error(`Método no disponible: ${method}`));
        return;
      }
      fn(JSON.stringify(payload || {}), (raw) => {
        try { resolve(_parseResponse(raw)); }
        catch (err) { reject(err); }
      });
    } catch (err) {
      reject(err);
    }
  }));
}

// ─── Async call (para slots pesados con job background) ──────────────────────

// Timeout de seguridad: si un job no responde en este tiempo, la promise se rechaza
// y el overlay se cierra para que la UI quede usable. El worker Python puede seguir
// corriendo en background, pero JS ya no espera su señal.
const _JOB_TIMEOUT_MS = 10 * 60 * 1000; // operaciones comunes
const _EXPORT_JOB_TIMEOUT_MS = 90 * 60 * 1000; // lotes grandes

// Mapa job_id → {resolve, reject, timerId}. Se resuelve cuando llega jobFinished.
const _pendingJobs = new Map();

// overlayOpts: {title, message} para overlay visual.
// overlayOpts.nonBlocking: true → chip bottom-right, no overlay bloqueante.
//   Cuando _saveChipPinned=true (lote en curso), _showSaveChip actualiza texto
//   pero _hideSaveChip es noop → sin parpadeo entre partes.
function _callAsync(method, payload, overlayOpts = {}) {
  const nonBlocking = overlayOpts.nonBlocking === true;
  const cancellable = overlayOpts.cancellable === true;
  const timeoutMs = Number(overlayOpts.timeoutMs) > 0
    ? Number(overlayOpts.timeoutMs)
    : _JOB_TIMEOUT_MS;

  return window.bigpdfReady.then(() => new Promise((resolve, reject) => {
    try {
      const fn = window.bigpdfBridge?.[method];
      if (typeof fn !== 'function') {
        reject(new Error(`Método no disponible: ${method}`));
        return;
      }
      fn(JSON.stringify(payload || {}), (raw) => {
        try {
          const data = _parseResponse(raw);
          if (data.async && data.job_id) {
            if (nonBlocking) {
              _showSaveChip(overlayOpts.message || 'Procesando…', data.job_id, cancellable);
            } else {
              _showOverlay(data.job_id, { ...overlayOpts, cancellable });
            }

            const timerId = setTimeout(() => {
              if (!_pendingJobs.has(data.job_id)) return;
              const pending = _pendingJobs.get(data.job_id);
              _pendingJobs.delete(data.job_id);
              // Siempre pedir la cancelación: si no, Python seguía trabajando (p. ej.
              // importando) y el resultado quedaba huérfano, sin aparecer en pantalla.
              _call('cancelJob', { job_id: data.job_id }).catch(() => {});
              if (pending?.nonBlocking) {
                if (![..._pendingJobs.values()].some(item => item.nonBlocking)) _hideSaveChip();
              } else {
                if (![..._pendingJobs.values()].some(item => !item.nonBlocking)) _hideOverlay();
              }
              reject(new Error('La operación excedió el tiempo máximo de espera y fue cancelada.'));
            }, timeoutMs);

            _pendingJobs.set(data.job_id, {
              resolve, reject, timerId, nonBlocking, cancellable,
              method, overlayOpts, promoted: false,
            });
          } else {
            resolve(data);
          }
        } catch (err) { reject(err); }
      });
    } catch (err) {
      reject(err);
    }
  }));
}


// Llamado cuando Python emite jobFinished.
function _onJobFinished(raw) {
  let data;
  try { data = JSON.parse(raw); } catch (_) { return; }
  const { job_id, ok, result, error, cancelled } = data;
  const pending = _pendingJobs.get(job_id);
  _pendingJobs.delete(job_id);

  if (pending?.timerId) clearTimeout(pending.timerId);
  if (pending?.nonBlocking) {
    if (![..._pendingJobs.values()].some(item => item.nonBlocking)) _hideSaveChip();
  } else {
    if (![..._pendingJobs.values()].some(item => !item.nonBlocking)) _hideOverlay();
  }
  if (!pending) return;

  if (ok) {
    pending.resolve(result);
    return;
  }
  const err = new Error(error || 'Error en proceso en segundo plano.');
  if (cancelled) {
    err.name = 'AbortError';
    err.cancelled = true;
  }
  pending.reject(err);
}


// Llamado cuando Python intercepta un drop de archivos locales desde el OS.
// _last_selected_paths ya fue seteado en Python antes de emitir esta señal.
// Llamar _bigpdfHandleNativeDrop (seteado por organize.js) para importar sin base64.
function _onNativeDrop() {
  if (typeof window._bigpdfHandleNativeDrop === 'function') {
    window._bigpdfHandleNativeDrop();
  }
}

// ─── Feedback visual de drag nativo ──────────────────────────────────────────

let _nativeDragEl = null;

function _ensureNativeDragEl() {
  if (_nativeDragEl) return;

  // Inyectar CSS en el head — mantiene estilos junto al código que los usa.
  const style = document.createElement('style');
  style.textContent = `
    #bp-native-drag-overlay {
      position: fixed;
      inset: 8px;
      z-index: 10400;
      pointer-events: none;
      border: 2px dashed var(--primary, #2A6FE0);
      border-radius: 12px;
      background: rgba(42, 111, 224, 0.05);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 14px;
      opacity: 0;
      transition: opacity 0.15s ease;
    }
    body.native-file-dragging #bp-native-drag-overlay {
      opacity: 1;
    }
    .bp-nd-icon {
      color: var(--primary, #2A6FE0);
      opacity: 0.75;
    }
    .bp-nd-text {
      font-size: 15px;
      font-weight: 600;
      color: var(--primary, #2A6FE0);
      opacity: 0.85;
      text-align: center;
      padding: 0 24px;
      letter-spacing: 0.01em;
    }
    html.theme-dark #bp-native-drag-overlay {
      border-color: rgba(100, 160, 255, 0.55);
      background: rgba(100, 160, 255, 0.07);
    }
    html.theme-dark .bp-nd-icon,
    html.theme-dark .bp-nd-text {
      color: rgba(140, 190, 255, 0.9);
    }
  `;
  document.head.appendChild(style);

  _nativeDragEl = document.createElement('div');
  _nativeDragEl.id = 'bp-native-drag-overlay';
  _nativeDragEl.innerHTML = `
    <svg class="bp-nd-icon" xmlns="http://www.w3.org/2000/svg" width="44" height="44"
         viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/>
      <line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
    <div class="bp-nd-text">Soltá los archivos para agregarlos a BIGPDF</div>
  `;
  document.body.appendChild(_nativeDragEl);
}

// Activado/desactivado por la señal nativeDragStateChanged de Python.
// Añade/quita clase en body → CSS muestra/oculta overlay con transición.
function _onNativeDragStateChanged(active) {
  _ensureNativeDragEl();
  document.body.classList.toggle('native-file-dragging', !!active);
}

// Llamado cuando Python emite jobProgress.
function _formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return minutes ? `${minutes}:${String(secs).padStart(2, '0')}` : `${secs} s`;
}

function _onJobProgress(raw) {
  let data;
  try { data = JSON.parse(raw); } catch (_) { return; }
  const { job_id, pct, message, title, stage, stage_index, stage_total, elapsed_seconds, large_batch } = data;
  const pending = _pendingJobs.get(job_id);
  if (!pending) return;

  // Los lotes grandes pasan automáticamente del chip discreto a un panel completo
  // con etapa, tiempo, barra y cancelación, sin bloquear el event loop de la app.
  if (large_batch && pending.nonBlocking && !pending.promoted) {
    pending.nonBlocking = false;
    pending.promoted = true;
    _hideSaveChipForce();
    _showOverlay(job_id, {
      title: title || 'Procesando lote grande',
      message: message || 'Preparando lote grande…',
      cancellable: pending.cancellable,
    });
  }

  const details = {
    pct, message, title, stage, stage_index, stage_total,
    elapsed: _formatElapsed(elapsed_seconds),
  };
  if (pending.nonBlocking) _updateSaveChip(message, details);
  else _updateOverlayProgress(details);
}


// ─── Overlay de progreso ──────────────────────────────────────────────────────

let _overlayEl = null;
let _overlayTitleEl = null;
let _overlayMsgEl = null;
let _overlayBarWrapEl = null;
let _overlayBarEl = null;
let _overlayElapsedEl = null;
let _overlayStageEl = null;
let _overlayCancelBtn = null;
let _overlayCurrentJob = null;
let _overlayShowVersion = 0; // cancela RAFs huérfanos de _showOverlay

function _ensureOverlay() {
  if (_overlayEl) return;
  _overlayEl = document.createElement('div');
  _overlayEl.id = 'bp-job-overlay';
  _overlayEl.innerHTML = `
    <div class="bp-job-card">
      <div class="bp-job-spinner"></div>
      <div class="bp-job-title"></div>
      <div class="bp-job-stage"></div>
      <div class="bp-job-message"></div>
      <div class="bp-job-elapsed"></div>
      <div class="bp-job-bar-wrap" style="width:100%;background:rgba(148,163,184,0.22);border-radius:4px;height:5px;margin-top:8px;overflow:hidden;display:none">
        <div class="bp-job-bar" style="height:5px;background:var(--primary,#4f8ef7);border-radius:4px;width:0%;transition:width 0.25s ease"></div>
      </div>
      <button type="button" class="bp-job-cancel">Cancelar procesamiento</button>
    </div>
  `;
  _overlayTitleEl   = _overlayEl.querySelector('.bp-job-title');
  _overlayMsgEl       = _overlayEl.querySelector('.bp-job-message');
  _overlayElapsedEl   = _overlayEl.querySelector('.bp-job-elapsed');
  _overlayStageEl     = _overlayEl.querySelector('.bp-job-stage');
  _overlayBarWrapEl   = _overlayEl.querySelector('.bp-job-bar-wrap');
  _overlayBarEl       = _overlayEl.querySelector('.bp-job-bar');
  _overlayCancelBtn   = _overlayEl.querySelector('.bp-job-cancel');
  _overlayCancelBtn?.addEventListener('click', () => _cancelPendingJob(_overlayCurrentJob));
  document.body.appendChild(_overlayEl);
}

function _showOverlay(jobId, opts) {
  _ensureOverlay();
  _overlayCurrentJob = jobId;
  if (_overlayTitleEl) _overlayTitleEl.textContent = opts.title || 'Procesando…';
  if (_overlayMsgEl) _overlayMsgEl.textContent = opts.message || 'Esto puede tardar unos segundos.';
  if (_overlayStageEl) _overlayStageEl.textContent = '';
  if (_overlayElapsedEl) _overlayElapsedEl.textContent = 'Tiempo transcurrido: 0 s';
  if (_overlayCancelBtn) {
    _overlayCancelBtn.hidden = !opts.cancellable;
    _overlayCancelBtn.disabled = false;
    _overlayCancelBtn.textContent = 'Cancelar procesamiento';
  }
  // Resetear barra al iniciar un nuevo job.
  if (_overlayBarEl)     _overlayBarEl.style.width = '0%';
  if (_overlayBarWrapEl) _overlayBarWrapEl.style.display = 'none';
  const v = ++_overlayShowVersion;
  requestAnimationFrame(() => {
    if (_overlayEl && _overlayShowVersion === v) _overlayEl.classList.add('visible');
  });
}

// Actualiza overlay con datos de un evento jobProgress sin cambiar la visibilidad.
function _updateOverlayProgress(opts) {
  if (_overlayMsgEl && opts.message != null) _overlayMsgEl.textContent = opts.message;
  if (_overlayTitleEl && opts.title != null) _overlayTitleEl.textContent = opts.title;
  if (_overlayElapsedEl && opts.elapsed) _overlayElapsedEl.textContent = `Tiempo transcurrido: ${opts.elapsed}`;
  if (_overlayStageEl) {
    const numbered = opts.stage_index && opts.stage_total
      ? `${opts.stage_index}/${opts.stage_total} · `
      : '';
    _overlayStageEl.textContent = `${numbered}${opts.stage || ''}`;
  }
  if (typeof opts.pct === 'number' && opts.pct >= 0 && opts.pct <= 100) {
    if (_overlayBarWrapEl) _overlayBarWrapEl.style.display = 'block';
    if (_overlayBarEl) _overlayBarEl.style.width = opts.pct + '%';
  }
}


function _hideOverlay() {
  if (!_overlayEl) return;
  ++_overlayShowVersion; // cancela cualquier RAF pendiente de _showOverlay
  _overlayEl.classList.remove('visible');
  if (_overlayBarEl)     _overlayBarEl.style.width = '0%';
  if (_overlayBarWrapEl) _overlayBarWrapEl.style.display = 'none';
  _overlayCurrentJob = null;
}


async function _cancelPendingJob(jobId) {
  if (!jobId || !_pendingJobs.has(jobId)) return;
  const pending = _pendingJobs.get(jobId);
  if (!pending?.cancellable || pending.cancelRequested) return;
  pending.cancelRequested = true;

  if (_overlayCancelBtn && _overlayCurrentJob === jobId) {
    _overlayCancelBtn.disabled = true;
    _overlayCancelBtn.textContent = 'Cancelando…';
  }
  if (_saveChipCancelBtn && _saveChipJobId === jobId) {
    _saveChipCancelBtn.disabled = true;
    _saveChipCancelBtn.textContent = 'Cancelando…';
  }
  try {
    await _call('cancelJob', { job_id: jobId });
  } catch (error) {
    pending.cancelRequested = false;
    if (_overlayCancelBtn) {
      _overlayCancelBtn.disabled = false;
      _overlayCancelBtn.textContent = 'Cancelar procesamiento';
    }
  }
}

// ─── Save chip (indicador no bloqueante, bottom-right) ───────────────────────
// Pill flotante — no tapa la grilla, no bloquea clicks, pointer-events:none.
// Tiene dos estados: spinner (en curso) y check (éxito efímero).
// _saveChipPinned: cuando true, _hideSaveChip() es noop → útil para lotes.

let _saveChipEl = null;
let _saveChipTextEl = null;
let _saveChipProgressEl = null;
let _saveChipStageEl = null;
let _saveChipCancelBtn = null;
let _saveChipJobId = null;
let _saveChipPinned = false;
let _saveChipSuccessTimer = null;

function _ensureSaveChip() {
  if (_saveChipEl) return;
  const style = document.createElement('style');
  style.textContent = `
    #bp-save-chip {
      position: fixed; bottom: 18px; left: 18px; right: auto; z-index: 9500;
      display: flex; align-items: center; gap: 7px;
      padding: 6px 14px;
      background: var(--bg-card, #fff);
      border: 1px solid var(--border, #dde1e7);
      border-radius: 20px;
      box-shadow: 0 2px 16px rgba(0,0,0,0.10);
      color: var(--text-secondary, #555);
      font-size: 12px; font-weight: 500;
      pointer-events: none; white-space: nowrap;
      max-width: min(620px, calc(100vw - 36px));
      opacity: 0; transform: translateY(6px);
      transition: opacity 0.18s ease, transform 0.18s ease,
                  color 0.15s ease, border-color 0.15s ease;
    }
    #bp-save-chip.visible { opacity: 1; transform: translateY(0); }
    #bp-save-chip.cancellable { pointer-events: auto; }
    .bp-sc-main { display:flex; flex-direction:column; gap:3px; min-width:180px; }
    .bp-sc-stage { font-size:10px; opacity:.72; overflow:hidden; text-overflow:ellipsis; }
    .bp-sc-text { overflow:hidden; text-overflow:ellipsis; }
    .bp-sc-progress { height:3px; background:rgba(148,163,184,.22); border-radius:3px; overflow:hidden; display:none; }
    .bp-sc-progress > div { height:100%; width:0%; background:var(--primary,#2A6FE0); transition:width .2s ease; }
    .bp-sc-cancel { border:1px solid var(--border,#dde1e7); background:transparent; color:inherit; border-radius:7px; padding:4px 8px; font:inherit; font-size:10px; cursor:pointer; }
    .bp-sc-cancel:hover { border-color:#ef4444; color:#ef4444; }
    .bp-sc-cancel:disabled { opacity:.55; cursor:default; }
    #bp-save-chip.success {
      color: #16a34a; border-color: rgba(22,163,74,0.35);
    }
    #bp-save-chip.error {
      color: #dc2626; border-color: rgba(220,38,38,0.35);
    }
    .bp-sc-spinner {
      width: 11px; height: 11px; flex-shrink: 0;
      border: 2px solid var(--border, #dde1e7);
      border-top-color: var(--primary, #2A6FE0);
      border-radius: 50%; animation: bp-sc-spin 0.8s linear infinite;
      will-change: transform; transform: translateZ(0); backface-visibility: hidden;
    }
    .bp-sc-check { display: none; width: 11px; height: 11px; flex-shrink: 0; }
    .bp-sc-error-icon { display: none; width: 11px; height: 11px; flex-shrink: 0; }
    #bp-save-chip.success .bp-sc-spinner    { display: none; }
    #bp-save-chip.success .bp-sc-check      { display: block; color: #16a34a; }
    #bp-save-chip.error .bp-sc-spinner      { display: none; }
    #bp-save-chip.error .bp-sc-error-icon   { display: block; color: #dc2626; }
    @keyframes bp-sc-spin { to { transform: rotate(360deg); } }
    html.theme-dark #bp-save-chip {
      background: var(--bg-card, #1e2533);
      border-color: var(--border, #2d3748);
      color: rgba(255,255,255,0.65);
    }
    html.theme-dark #bp-save-chip.success {
      color: #4ade80; border-color: rgba(74,222,128,0.35);
    }
    html.theme-dark #bp-save-chip.success .bp-sc-check { color: #4ade80; }
    html.theme-dark #bp-save-chip.error {
      color: #f87171; border-color: rgba(248,113,113,0.35);
    }
    html.theme-dark #bp-save-chip.error .bp-sc-error-icon { color: #f87171; }
  `;
  document.head.appendChild(style);
  _saveChipEl = document.createElement('div');
  _saveChipEl.id = 'bp-save-chip';
  _saveChipEl.innerHTML = `
    <div class="bp-sc-spinner"></div>
    <svg class="bp-sc-check" viewBox="0 0 12 12" fill="none" stroke="currentColor"
         stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="1.5,6.5 4.5,9.5 10.5,2.5"/>
    </svg>
    <svg class="bp-sc-error-icon" viewBox="0 0 12 12" fill="none" stroke="currentColor"
         stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/>
    </svg>
    <div class="bp-sc-main">
      <span class="bp-sc-stage"></span>
      <span class="bp-sc-text"></span>
      <div class="bp-sc-progress"><div></div></div>
    </div>
    <button type="button" class="bp-sc-cancel" hidden>Cancelar</button>
  `;
  document.body.appendChild(_saveChipEl);
  _saveChipTextEl = _saveChipEl.querySelector('.bp-sc-text');
  _saveChipStageEl = _saveChipEl.querySelector('.bp-sc-stage');
  _saveChipProgressEl = _saveChipEl.querySelector('.bp-sc-progress > div');
  _saveChipCancelBtn = _saveChipEl.querySelector('.bp-sc-cancel');
  _saveChipCancelBtn?.addEventListener('click', () => _cancelPendingJob(_saveChipJobId));
}

function _showSaveChip(msg, jobId = null, cancellable = false) {
  _ensureSaveChip();
  if (_saveChipSuccessTimer) { clearTimeout(_saveChipSuccessTimer); _saveChipSuccessTimer = null; }
  _saveChipJobId = jobId;
  _saveChipEl.classList.remove('success', 'error', 'cancellable');
  _saveChipEl.classList.toggle('cancellable', !!cancellable);
  if (_saveChipTextEl) _saveChipTextEl.textContent = msg || 'Procesando…';
  if (_saveChipStageEl) _saveChipStageEl.textContent = '';
  const progressWrap = _saveChipProgressEl?.parentElement;
  if (progressWrap) progressWrap.style.display = 'none';
  if (_saveChipProgressEl) _saveChipProgressEl.style.width = '0%';
  if (_saveChipCancelBtn) {
    _saveChipCancelBtn.hidden = !cancellable;
    _saveChipCancelBtn.disabled = false;
    _saveChipCancelBtn.textContent = 'Cancelar';
  }
  _saveChipEl.classList.add('visible');
}

function _updateSaveChip(msg, details = {}) {
  _ensureSaveChip();
  if (_saveChipEl.classList.contains('success') || _saveChipEl.classList.contains('error')) return;
  if (_saveChipTextEl) _saveChipTextEl.textContent = msg || '';
  if (_saveChipStageEl) {
    const numbered = details.stage_index && details.stage_total
      ? `${details.stage_index}/${details.stage_total} · `
      : '';
    const elapsed = details.elapsed ? ` · ${details.elapsed}` : '';
    _saveChipStageEl.textContent = `${numbered}${details.stage || ''}${elapsed}`;
  }
  if (typeof details.pct === 'number') {
    const progressWrap = _saveChipProgressEl?.parentElement;
    if (progressWrap) progressWrap.style.display = 'block';
    if (_saveChipProgressEl) _saveChipProgressEl.style.width = `${Math.max(0, Math.min(100, details.pct))}%`;
  }
}

function _hideSaveChip() {
  if (_saveChipPinned) return;
  _hideSaveChipForce();
}

function _hideSaveChipForce() {
  if (!_saveChipEl) return;
  if (_saveChipSuccessTimer) { clearTimeout(_saveChipSuccessTimer); _saveChipSuccessTimer = null; }
  _saveChipEl.classList.remove('visible', 'success', 'error', 'cancellable');
  _saveChipJobId = null;
  if (_saveChipCancelBtn) _saveChipCancelBtn.hidden = true;
}

function _showSaveChipSuccess(msg, ms = 2000) {
  _ensureSaveChip();
  _saveChipPinned = false;
  if (_saveChipSuccessTimer) clearTimeout(_saveChipSuccessTimer);
  _saveChipEl.classList.remove('success', 'error');
  if (_saveChipTextEl) _saveChipTextEl.textContent = msg || 'Guardado';
  _saveChipEl.classList.add('visible', 'success');
  _saveChipSuccessTimer = setTimeout(() => {
    _saveChipSuccessTimer = null;
    _hideSaveChipForce();
  }, ms);
}

function _showSaveChipError(msg, ms = 3500) {
  _ensureSaveChip();
  _saveChipPinned = false;
  if (_saveChipSuccessTimer) clearTimeout(_saveChipSuccessTimer);
  _saveChipEl.classList.remove('success', 'error');
  if (_saveChipTextEl) _saveChipTextEl.textContent = msg || 'Error';
  _saveChipEl.classList.add('visible', 'error');
  _saveChipSuccessTimer = setTimeout(() => {
    _saveChipSuccessTimer = null;
    _hideSaveChipForce();
  }, ms);
}

// ─── Señales Python → JS ──────────────────────────────────────────────────────

window.bigpdfReady = new Promise((resolve, reject) => {
  if (typeof qt === 'undefined' || typeof QWebChannel === 'undefined') {
    reject(new Error('QWebChannel no está disponible. Abrí Big PDF desde main.py.'));
    return;
  }
  new QWebChannel(qt.webChannelTransport, (channel) => {
    window.bigpdfBridge = channel.objects.bigpdfBridge;
    if (!window.bigpdfBridge) {
      reject(new Error('No se pudo inicializar el bridge de Big PDF.'));
      return;
    }
    // Conectar a señales de jobs en background.
    if (typeof window.bigpdfBridge.jobFinished?.connect === 'function') {
      window.bigpdfBridge.jobFinished.connect(_onJobFinished);
    }
    if (typeof window.bigpdfBridge.jobProgress?.connect === 'function') {
      window.bigpdfBridge.jobProgress.connect(_onJobProgress);
    }
    if (typeof window.bigpdfBridge.filesDropped?.connect === 'function') {
      window.bigpdfBridge.filesDropped.connect(_onNativeDrop);
    }
    if (typeof window.bigpdfBridge.nativeDragStateChanged?.connect === 'function') {
      window.bigpdfBridge.nativeDragStateChanged.connect(_onNativeDragStateChanged);
    }
    resolve(window.bigpdfBridge);
  });
});

window.__bigpdfFileUrls = window.__bigpdfFileUrls || {};

document.addEventListener('contextmenu', (e) => e.preventDefault());

// ─── File reading ─────────────────────────────────────────────────────────────

function _readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`No se pudo leer ${file.name}.`));
    reader.onload = () => {
      const text = String(reader.result || '');
      const comma = text.indexOf(',');
      resolve(comma >= 0 ? text.slice(comma + 1) : text);
    };
    reader.readAsDataURL(file);
  });
}

// Límites para la ruta de arrastre (base64 por QWebChannel).
// json.loads() corre en hilo Qt: payloads > ~100 MB congela la UI.
// Por archivo: 25 MB → 33 MB base64. Total acumulado: 70 MB → 93 MB JSON.
// Para archivos más grandes, usar el botón "Seleccionar archivos"
// que opera con rutas locales y nunca transfiere contenido por base64.
const _MAX_BASE64_FILE_MB = 25;
const _MAX_BASE64_TOTAL_MB = 70;

async function _filesToBase64Payload(files) {
  const out = [];
  let totalBytes = 0;
  for (const file of files) {
    const fileMb = Math.round(file.size / 1024 / 1024);
    if (file.size > _MAX_BASE64_FILE_MB * 1024 * 1024) {
      throw new Error(
        `"${file.name}" es demasiado grande para arrastrarlo (${fileMb} MB). ` +
        `Usá el botón Seleccionar archivos para archivos grandes.`
      );
    }
    totalBytes += file.size;
    if (totalBytes > _MAX_BASE64_TOTAL_MB * 1024 * 1024) {
      const totalMb = Math.round(totalBytes / 1024 / 1024);
      throw new Error(
        `El total de archivos arrastrables supera los ${_MAX_BASE64_TOTAL_MB} MB (${totalMb} MB combinados). ` +
        `Usá el botón Seleccionar archivos.`
      );
    }
    out.push({
      filename: file.name,
      size: file.size,
      type: file.type || '',
      data_base64: await _readFileAsBase64(file),
    });
  }
  return out;
}

// ─── API pública ──────────────────────────────────────────────────────────────

window.bigpdfApi = {
  call: _call,

  async closeApp() {
    return _call('closeApp');
  },

  async importFiles(files) {
    const list = [...files];
    const names = list.map(f => f.name);

    // Camino normal: QFileDialog interceptado por PySide.
    // Python tiene rutas reales — no se transfieren PDFs grandes por JS.
    // Ahora async: el bridge corre fitz/LibreOffice en background.
    // Si no hay rutas (o no coinciden con los archivos del input), Python responde
    // used_selected_paths:false SIN error y se usa el fallback base64. Si en cambio
    // la importación por ruta arrancó y falló (o se canceló / excedió el tiempo), el
    // error se propaga tal cual: reintentar por base64 tapaba el error real y podía
    // importar dos veces los archivos chicos.
    const selected = await _callAsync('importSelectedFiles', { names }, {
      title: 'Analizando archivos…',
      message: 'Preparando documentos...',
      // Muchos Word/Excel pueden tardar (hasta 2 min cada uno con LibreOffice).
      timeoutMs: _EXPORT_JOB_TIMEOUT_MS,
    });
    if (selected && selected.used_selected_paths) {
      for (const doc of selected.documents || []) {
        if (doc.file_url) window.__bigpdfFileUrls[doc.converted_pdf_id || doc.document_id] = doc.file_url;
      }
      return selected;
    }

    // Fallback: drag & drop o casos sin ruta local expuesta.
    const payloadFiles = await _filesToBase64Payload(list);
    const result = await _callAsync('importFileData', { files: payloadFiles }, {
      title: 'Cargando archivos…',
      message: 'Procesando documentos en segundo plano.',
    });
    for (const doc of (result || {}).documents || []) {
      if (doc.file_url) window.__bigpdfFileUrls[doc.converted_pdf_id || doc.document_id] = doc.file_url;
    }
    return result || { documents: [] };
  },

  async getDocument(id) {
    const result = await _callAsync('getDocument', { document_id: id }, {
      title: 'Analizando PDF…',
      message: 'Cargando documento pesado..',
    });
    if (result && result.file_url) window.__bigpdfFileUrls[id] = result.file_url;
    return result;
  },

  async getFileUrl(id) {
    if (window.__bigpdfFileUrls[id]) return window.__bigpdfFileUrls[id];
    const result = await _call('getFileUrl', { document_id: id });
    if (result.url) window.__bigpdfFileUrls[id] = result.url;
    return result.url;
  },

  // batchMode: true → no overlay bloqueante; usa chip no modal.
  // overlayMsg: texto del overlay (solo cuando batchMode=false).
  async exportWorkspace(req, overlayMsg = null, batchMode = false) {
    if (batchMode) {
      return _callAsync('exportWorkspace', req, {
        nonBlocking: true,
        cancellable: true,
        timeoutMs: _EXPORT_JOB_TIMEOUT_MS,
        message: overlayMsg || 'Generando…',
      });
    }
    return _callAsync('exportWorkspace', req, {
      title: 'Exportando PDF…',
      cancellable: true,
      timeoutMs: _EXPORT_JOB_TIMEOUT_MS,
      message: overlayMsg || 'Generando el documento. Esto puede tardar en archivos grandes.',
    });
  },

  async compress(req) {
    return _callAsync('compress', req, {
      title: 'Comprimiendo PDF…',
      message: 'Ghostscript procesando. Puede tardar varios minutos en archivos grandes.',
    });
  },

  async integraCatalog() {
    return _call('integraCatalog');
  },

  // Lee (texto u OCR), clasifica y analiza. Overlay bloqueante y cancelable: la
  // primera vez puede tardar ~2 s por hoja escaneada; después usa la caché.
  async integraAnalyze(req) {
    return _callAsync('integraAnalyze', req, {
      title: 'Analizando documentación…',
      message: 'Leyendo e identificando cada hoja.',
      cancellable: true,
      timeoutMs: _EXPORT_JOB_TIMEOUT_MS,
    });
  },

  isCancellation(error) {
    return !!error?.cancelled || error?.name === 'AbortError';
  },

  // Siempre no bloqueante — copia archivo ya generado.
  // chipMsg: texto del chip (ej: "Guardando parte 2 de 3…").
  async saveExport(exportId, filename, chipMsg = null) {
    return _callAsync('saveExport', { export_id: exportId, filename }, {
      message: chipMsg || 'Guardando…',
      nonBlocking: true,
    });
  },

  // ─── Status chip API (organize.js y cualquier caller externo) ────────────
  // showProgress / showStatus     → inicia chip con spinner, pínea durante lote
  // updateProgress / updateStatus → actualiza texto (noop si está en success/error)
  // showSuccess / showStatusSuccess → check verde, auto-oculta
  // showStatusError               → ✕ rojo, auto-oculta
  // hideProgress / hideStatus     → despínea y oculta

  showProgress(msg) {
    _saveChipPinned = true;
    _showSaveChip(msg || 'Preparando…');
  },
  showStatus(msg) { this.showProgress(msg); },

  updateProgress(msg) {
    _updateSaveChip(msg || '');
  },
  updateStatus(msg) { this.updateProgress(msg); },

  showSuccess(msg, ms = 2000) {
    _showSaveChipSuccess(msg, ms);
  },
  showStatusSuccess(msg, ms = 2000) { this.showSuccess(msg, ms); },

  showStatusError(msg, ms = 3500) {
    _showSaveChipError(msg, ms);
  },

  hideProgress() {
    _saveChipPinned = false;
    _hideSaveChipForce();
  },
  hideStatus() { this.hideProgress(); },

  async getDownloadLocationPref() {
    return _call('getDownloadLocationPref');
  },

  async setDownloadLocationPref(choose_location) {
    return _call('setDownloadLocationPref', { choose_location: !!choose_location });
  },

  async cleanup(document_ids = []) {
    return _call('cleanup', { document_ids });
  },
};

// BUG-7: cancela jobs pendientes al navegar entre rutas.
// Expuesto en window para que router.js lo llame sin necesidad de import cruzado
// (ambos son módulos ES en <script> separados).
function _clearPendingJobs() {
  for (const [, p] of _pendingJobs) {
    if (p?.timerId) clearTimeout(p.timerId);
    try { p.reject(new Error('Navegación: job cancelado.')); } catch (_) {}
  }
  _pendingJobs.clear();
}
window._bigpdfClearPendingJobs = _clearPendingJobs;
