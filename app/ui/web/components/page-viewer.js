import { svgIcon } from '../js/utils.js';
import { effectiveRotation } from './page-grid.js';

const SCALE_STEPS = [0.4, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
const DEFAULT_SCALE_IDX = 3; // 1.0
const ZOOM_DEBOUNCE_MS  = 280;
const MAX_CANVAS_PX     = 4096; // cap per canvas axis at high zoom — prevents GPU OOM
const WHEEL_ZOOM_STEP_PX = 80;  // rueda: delta acumulado por cada nivel de zoom (1 "clic" de rueda ≈ 100)
const LOADING_HTML = () => `${svgIcon('layers', 32)}<span>Cargando...</span>`;

let _viewerOpen = false; // evita abrir dos visores superpuestos (doble clic rápido)

// onRotate(pageId) — optional callback fired when user rotates in the viewer
// onBeforeRotate(pageId) — optional, se llama ANTES de rotar (registro de deshacer)
export function openPageViewer(pages, startId, getOrLoadDoc, onRotate, onBeforeRotate) {
  if (_viewerOpen || document.querySelector('.pg-viewer-overlay')) return;
  const active = pages.filter(p => !p.deleted);
  if (!active.length) return;
  _viewerOpen = true;
  const previousFocus = document.activeElement;

  let idx = active.findIndex(p => p.id === startId);
  if (idx < 0) idx = 0;
  let scaleIdx = DEFAULT_SCALE_IDX;
  let panX = 0, panY = 0, isPanning = false;
  let _panStartX = 0, _panStartY = 0, _lastIdx = -1;

  // ─── Render state ────────────────────────────────────────────────────────────
  let _renderTask   = null; // active PDF.js RenderTask — cancellable
  let _renderToken  = 0;    // incremented each render — stale renders see mismatch and bail
  let _zoomDebounce = null; // debounce timer for high-quality re-render after zoom
  let _cachedPage   = null; // { docId, pageNum, pdfPage } — skip getPage() on zoom-only changes
  let _lastFitScale = null; // fitScale from last completed render — used for instant CSS zoom
  let _lastBaseVp   = null; // baseVp from last completed render — used for instant CSS zoom

  // ─── Build overlay ──────────────────────────────────────────────────────────

  const overlay = document.createElement('div');
  overlay.className = 'pg-viewer-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'Visor de página');

  overlay.innerHTML = `
    <div class="pg-viewer-modal">
      <div class="pg-viewer-header">
        <div class="pg-viewer-info">
          <span class="pv-filename" id="pv-filename"></span>
          <span class="pv-sep">·</span>
          <span id="pv-pagenum"></span>
          <span class="pv-rotation-badge" id="pv-rotation"></span>
        </div>
        <div class="pg-viewer-controls">
          <button class="pv-btn" id="pv-zoom-out" title="Alejar (−)" aria-label="Alejar">${svgIcon('zoom-out', 15)}</button>
          <span class="pv-zoom-label" id="pv-zoom-label">100%</span>
          <button class="pv-btn" id="pv-zoom-in"  title="Acercar (+)" aria-label="Acercar">${svgIcon('zoom-in', 15)}</button>
          <button class="pv-btn" id="pv-fit"  title="Ajustar (0)" aria-label="Ajustar a la ventana">${svgIcon('maximize', 15)}</button>
          <span class="pv-sep" aria-hidden="true" style="margin:0 4px;opacity:0.35">|</span>
          <button class="pv-btn" id="pv-rot-left"  title="Girar izquierda (Shift+R)" aria-label="Girar a la izquierda">${svgIcon('rotate-ccw', 15)}</button>
          <button class="pv-btn" id="pv-rot-right" title="Girar derecha (R)" aria-label="Girar a la derecha">${svgIcon('rotate-cw', 15)}</button>
          <span class="pv-sep" aria-hidden="true" style="margin:0 4px;opacity:0.35">|</span>
          <button class="pv-btn pv-close" id="pv-close" title="Cerrar (Esc)" aria-label="Cerrar visor">${svgIcon('x-circle', 17)}</button>
        </div>
      </div>
      <div class="pg-viewer-body">
        <button class="pv-nav-btn" id="pv-prev" title="Anterior (←)" aria-label="Página anterior">${svgIcon('chevron-left', 28)}</button>
        <div class="pg-viewer-canvas-wrap" id="pv-canvas-wrap">
          <div class="pv-loading" id="pv-loading">${LOADING_HTML()}</div>
          <canvas id="pv-canvas" style="display:none"></canvas>
        </div>
        <button class="pv-nav-btn" id="pv-next" title="Siguiente (→)" aria-label="Página siguiente">${svgIcon('chevron-right', 28)}</button>
      </div>
      <div class="pg-viewer-footer">
        <span id="pv-counter"></span>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';

  const canvas  = overlay.querySelector('#pv-canvas');
  const loading = overlay.querySelector('#pv-loading');
  const wrap    = overlay.querySelector('#pv-canvas-wrap');

  // ─── Cancel active PDF.js render task ───────────────────────────────────────
  function _cancelRenderTask() {
    if (_renderTask) {
      try { _renderTask.cancel(); } catch (_) {}
      _renderTask = null;
    }
  }

  // ─── Pan helpers ─────────────────────────────────────────────────────────────
  function clampPan() {
    const wrapW = wrap.clientWidth;
    const wrapH = wrap.clientHeight;
    const cW = canvas.clientWidth || canvas.width;
    const cH = canvas.clientHeight || canvas.height;
    const margin = 20;
    const maxPanX = Math.max(0, (cW - wrapW) / 2) + margin;
    const maxPanY = Math.max(0, (cH - wrapH) / 2) + margin;
    panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
    panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
  }
  function applyPan() { canvas.style.transform = `translate(${panX}px,${panY}px)`; }

  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    isPanning = true;
    _panStartX = e.clientX - panX;
    _panStartY = e.clientY - panY;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = 'grabbing';
    e.preventDefault();
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!isPanning) return;
    panX = e.clientX - _panStartX;
    panY = e.clientY - _panStartY;
    clampPan();
    applyPan();
  });
  canvas.addEventListener('pointerup', () => {
    if (!isPanning) return;
    isPanning = false;
    canvas.style.cursor = 'grab';
  });
  canvas.addEventListener('pointercancel', () => {
    isPanning = false;
    canvas.style.cursor = 'grab';
  });

  // ─── Meta update (instant, no render needed) ─────────────────────────────────
  function _updateMeta() {
    const page = active[idx];
    overlay.querySelector('#pv-filename').textContent  = page.sourceFilename || '';
    overlay.querySelector('#pv-pagenum').textContent   = `Pág. ${page.pageNum + 1}`;
    overlay.querySelector('#pv-rotation').textContent  = page.rotation ? `${page.rotation}°` : '';
    overlay.querySelector('#pv-counter').textContent   = `${idx + 1} / ${active.length}`;
    overlay.querySelector('#pv-prev').disabled          = idx === 0;
    overlay.querySelector('#pv-next').disabled          = idx === active.length - 1;
    overlay.querySelector('#pv-zoom-label').textContent = `${Math.round(SCALE_STEPS[scaleIdx] * 100)}%`;
  }

  // ─── Instant CSS-only zoom preview ───────────────────────────────────────────
  // Resizes canvas CSS dimensions immediately using stored fitScale + baseVp.
  // No PDF.js call — pure CSS, 0ms latency. High-quality re-render follows via debounce.
  function _applyInstantZoom() {
    if (!_lastFitScale || !_lastBaseVp) return;
    const finalScale = _lastFitScale * SCALE_STEPS[scaleIdx];
    canvas.style.width  = `${Math.round(finalScale * _lastBaseVp.width)}px`;
    canvas.style.height = `${Math.round(finalScale * _lastBaseVp.height)}px`;
    clampPan();
    applyPan();
  }

  // ─── High-quality async render ───────────────────────────────────────────────
  // Renders to offscreen canvas, swaps atomically on completion.
  // Checks _renderToken after each await to discard stale results.
  async function _doRender(isPageChange) {
    _cancelRenderTask();
    const token = ++_renderToken;

    if (isPageChange) {
      panX = 0; panY = 0;
      _lastIdx      = idx;
      _lastFitScale = null;
      _lastBaseVp   = null;
      canvas.style.display = 'none';
      // Resetea un posible "No se pudo cargar la página" de la página anterior.
      loading.innerHTML = LOADING_HTML();
      loading.style.display = 'flex';
    }

    const page = active[idx];

    try {
      // Reuse cached pdfPage when only zoom changed — avoids async getPage() overhead
      let pdfPage;
      if (_cachedPage?.docId === page.sourceDocumentId && _cachedPage?.pageNum === page.pageNum) {
        pdfPage = _cachedPage.pdfPage;
      } else {
        const pdfDoc = await getOrLoadDoc(page.sourceDocumentId);
        if (token !== _renderToken) return;
        pdfPage = await pdfDoc.getPage(page.pageNum + 1);
        if (token !== _renderToken) return;
        _cachedPage = { docId: page.sourceDocumentId, pageNum: page.pageNum, pdfPage };
      }

      // page.rotation es un delta del usuario sobre la rotación propia de la página.
      const rotation = effectiveRotation(pdfPage, page.rotation);
      const baseVp = pdfPage.getViewport({ scale: 1, rotation });

      let dpr = Math.min(window.devicePixelRatio || 1, 2);
      const wrapW     = wrap.clientWidth  - 48;
      const wrapH     = wrap.clientHeight - 48;
      const fitScale  = Math.min(wrapW / baseVp.width, wrapH / baseVp.height, 2.5);
      const finalScale = fitScale * SCALE_STEPS[scaleIdx];

      // Cap per-axis canvas size — prevents GPU OOM at high zoom on large PDFs
      const rawW = finalScale * dpr * baseVp.width;
      const rawH = finalScale * dpr * baseVp.height;
      if (rawW > MAX_CANVAS_PX || rawH > MAX_CANVAS_PX) {
        dpr *= Math.min(MAX_CANVAS_PX / rawW, MAX_CANVAS_PX / rawH);
      }

      const vp = pdfPage.getViewport({ scale: finalScale * dpr, rotation });

      // Render to offscreen canvas — visible canvas stays intact, no flicker during render
      const offscreen = document.createElement('canvas');
      offscreen.width  = Math.round(vp.width);
      offscreen.height = Math.round(vp.height);

      const renderTask = pdfPage.render({ canvasContext: offscreen.getContext('2d'), viewport: vp });
      _renderTask = renderTask;
      await renderTask.promise;
      _renderTask = null;

      if (token !== _renderToken) return; // stale result — discard silently

      // Atomic swap: copy offscreen → visible canvas, update CSS size
      canvas.width  = offscreen.width;
      canvas.height = offscreen.height;
      canvas.getContext('2d').drawImage(offscreen, 0, 0);
      canvas.style.width  = `${Math.round(finalScale * baseVp.width)}px`;
      canvas.style.height = `${Math.round(finalScale * baseVp.height)}px`;

      _lastFitScale = fitScale;
      _lastBaseVp   = baseVp;

      loading.style.display = 'none';
      canvas.style.display  = 'block';
      clampPan();
      canvas.style.transform = panX || panY ? `translate(${panX}px,${panY}px)` : '';
      canvas.style.cursor    = 'grab';

    } catch (err) {
      if (token !== _renderToken) return;
      if (err?.name === 'RenderingCancelledException') return; // expected on cancel — not an error
      loading.innerHTML = `${svgIcon('alert', 28)}<span>No se pudo cargar la página.</span>`;
      loading.style.display = 'flex';
      canvas.style.display = 'none';
    }
  }

  // ─── renderPage: public entry point ──────────────────────────────────────────
  // isZoom=true  → instant CSS resize + debounced high-quality re-render
  // isZoom=false → immediate full render with loader (page change, rotation, fit-reset)
  function renderPage(isZoom = false) {
    const isPageChange = idx !== _lastIdx;
    // Al pasar a otra página el zoom vuelve al 100 %: la página nueva se ve completa.
    if (isPageChange) scaleIdx = DEFAULT_SCALE_IDX;
    _updateMeta();
    const canInstant   = isZoom && !isPageChange && canvas.style.display !== 'none' && _lastFitScale != null;
    if (canInstant) {
      _applyInstantZoom();
      clearTimeout(_zoomDebounce);
      _zoomDebounce = setTimeout(() => _doRender(false), ZOOM_DEBOUNCE_MS);
    } else {
      clearTimeout(_zoomDebounce);
      _doRender(isPageChange);
    }
  }

  // ─── Rotation ───────────────────────────────────────────────────────────────

  function rotate(delta) {
    const page = active[idx];
    onBeforeRotate?.(page.id);
    page.rotation = ((page.rotation || 0) + delta + 360) % 360;
    // Force page-change behavior: show loader, reset pan, re-fetch viewport
    _lastIdx = -1;
    renderPage(false);
    onRotate?.(page.id); // triggers thumbnail cache invalidation + re-render in grid
  }

  // ─── Events ─────────────────────────────────────────────────────────────────

  overlay.querySelector('#pv-close').addEventListener('click', close);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

  overlay.querySelector('#pv-prev').addEventListener('click', () => {
    if (idx > 0) { idx--; renderPage(false); }
  });
  overlay.querySelector('#pv-next').addEventListener('click', () => {
    if (idx < active.length - 1) { idx++; renderPage(false); }
  });

  overlay.querySelector('#pv-zoom-in').addEventListener('click', () => {
    if (scaleIdx < SCALE_STEPS.length - 1) { scaleIdx++; renderPage(true); }
  });
  overlay.querySelector('#pv-zoom-out').addEventListener('click', () => {
    if (scaleIdx > 0) { scaleIdx--; renderPage(true); }
  });
  overlay.querySelector('#pv-fit').addEventListener('click', () => {
    panX = 0; panY = 0;
    scaleIdx = DEFAULT_SCALE_IDX;
    renderPage(true); // instant CSS resize to fit, then debounced crisp render
  });

  overlay.querySelector('#pv-rot-left').addEventListener('click', () => rotate(-90));
  overlay.querySelector('#pv-rot-right').addEventListener('click', () => rotate(90));

  // Rueda del mouse = zoom hacia donde está el puntero. Con la rueda no se aleja
  // más allá del 100 % (la página entera ajustada a la ventana); los botones −/+
  // siguen permitiendo alejar más. Para moverse por la página ampliada: arrastrar.
  // El delta se acumula para que un trackpad no recorra todos los niveles de golpe.
  let _wheelZoomAcc = 0;
  wrap.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (canvas.style.display === 'none') return;
    const unit = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? wrap.clientHeight : 1);
    _wheelZoomAcc += e.deltaY * unit;
    if (Math.abs(_wheelZoomAcc) < WHEEL_ZOOM_STEP_PX) return;
    const zoomIn = _wheelZoomAcc < 0;
    _wheelZoomAcc = 0;

    const prevIdx = scaleIdx;
    if (zoomIn) {
      scaleIdx = Math.min(SCALE_STEPS.length - 1, scaleIdx + 1);
    } else if (scaleIdx > DEFAULT_SCALE_IDX) {
      scaleIdx--;
    } else if (scaleIdx < DEFAULT_SCALE_IDX) {
      // Si se había alejado con los botones, la rueda vuelve directo al 100 %.
      scaleIdx = DEFAULT_SCALE_IDX;
    }
    if (scaleIdx === prevIdx) return;

    // Mantiene fijo el punto de la página que está bajo el puntero.
    const rect = wrap.getBoundingClientRect();
    const cx = e.clientX - (rect.left + rect.width / 2);
    const cy = e.clientY - (rect.top + rect.height / 2);
    const ratio = SCALE_STEPS[scaleIdx] / SCALE_STEPS[prevIdx];
    panX = cx - (cx - panX) * ratio;
    panY = cy - (cy - panY) * ratio;
    if (scaleIdx <= DEFAULT_SCALE_IDX) { panX = 0; panY = 0; }
    renderPage(true);
  }, { passive: false });

  function onKey(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    // Foco atrapado dentro del visor (modal).
    if (e.key === 'Tab') {
      const focusables = [...overlay.querySelectorAll('button:not([disabled])')];
      if (!focusables.length) return;
      const i = focusables.indexOf(document.activeElement);
      e.preventDefault();
      const next = e.shiftKey
        ? focusables[(i <= 0 ? focusables.length : i) - 1]
        : focusables[(i + 1) % focusables.length];
      next.focus();
      return;
    }
    // Ctrl/Cmd+tecla queda para el sistema (Ctrl+R no debe rotar, etc.).
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    switch (e.key) {
      case 'Escape':     close(); break;
      case 'ArrowLeft':  if (idx > 0) { idx--; renderPage(false); } break;
      case 'ArrowRight': if (idx < active.length - 1) { idx++; renderPage(false); } break;
      case '+': case '=':
        if (scaleIdx < SCALE_STEPS.length - 1) { scaleIdx++; renderPage(true); } break;
      case '-':
        if (scaleIdx > 0) { scaleIdx--; renderPage(true); } break;
      case '0':
        panX = 0; panY = 0; scaleIdx = DEFAULT_SCALE_IDX; renderPage(true); break;
      case 'r':
      case 'R':
        e.shiftKey ? rotate(-90) : rotate(90); break;
    }
  }

  document.addEventListener('keydown', onKey);

  function close() {
    clearTimeout(_zoomDebounce);
    _cancelRenderTask();
    ++_renderToken; // any pending _doRender() will see mismatch and bail
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
    overlay.remove();
    _viewerOpen = false;
    // Devuelve el foco a donde estaba (normalmente la tarjeta o el botón "Ver").
    if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
      try { previousFocus.focus(); } catch (_) {}
    }
  }

  // Foco inicial dentro del visor (lectores de pantalla + atajos de teclado).
  overlay.querySelector('#pv-close')?.focus();

  // Initial render
  requestAnimationFrame(() => renderPage(false));
}
