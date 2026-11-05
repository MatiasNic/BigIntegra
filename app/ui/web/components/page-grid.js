import { svgIcon, formatBytes, escapeHtml } from '../js/utils.js';
import { API } from '../js/api.js';
import { thumbMethods } from './page-grid-thumbs.js';
import { dndMethods } from './page-grid-dnd.js';

// Se re-exporta desde acá porque page-viewer.js la importa de page-grid.js.
export { effectiveRotation } from './page-grid-thumbs.js';

const PDFJS_URL = new URL('../vendor/pdfjs/pdf.min.mjs', import.meta.url).href;
const WORKER_URL = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
// CMaps y fuentes estándar de pdf.js 4.4.168 (vendorizados): sin ellos, PDFs con
// fuentes CJK o estándar no embebidas pueden mostrar texto faltante en miniaturas/visor.
const CMAP_URL = new URL('../vendor/pdfjs/cmaps/', import.meta.url).href;
const STANDARD_FONT_URL = new URL('../vendor/pdfjs/standard_fonts/', import.meta.url).href;

function _cssEscape(value) {
  return window.CSS?.escape ? CSS.escape(String(value)) : String(value).replace(/["\\]/g, '\\$&');
}

let _lib = null;
async function getPdfJs() {
  if (_lib) return _lib;
  _lib = await import(PDFJS_URL);
  _lib.GlobalWorkerOptions.workerSrc = WORKER_URL;
  return _lib;
}

export class PageGrid {
  constructor(container, state, callbacks = {}) {
    this.container = container;
    this.state = state;
    this.callbacks = callbacks;

    // docId → Promise<PDFDocumentProxy>. Se cachea la PROMESA (no el documento ya
    // resuelto) para que las miniaturas concurrentes compartan una sola carga.
    this._pdfDocs = new Map();
    this._pdfLoadingTasks = new Map(); // docId → loadingTask de pdf.js (para destroy())
    this._destroyed = false;
    this._thumbCache = new Map();    // key='pageId:rotation:px' or 'file:docId:px' → {url, w, h, bytes}
    this._thumbCacheBytes = 0;
    this._thumbUrls = new Set();     // todas las object URLs creadas (se revocan en destroy())
    this._thumbInFlight = new Set(); // claves encoladas/en curso — evita encolar duplicados
    this._observer = null;           // cercanía (600px): dispara renders
    this._nearCards = new Set();     // tarjetas actualmente dentro del margen cercano
    this._renderGen = 0;             // se incrementa en cada render() completo
    this._gridSize = localStorage.getItem('artemis-grid-size') || 'auto';
    this._selectionAnchorId = null;  // ancla para Shift+clic
    this._resizeTimer = null;
    this._onWindowResize = () => {
      clearTimeout(this._resizeTimer);
      this._resizeTimer = setTimeout(() => this._refreshThumbSizes(), 250);
    };

    // Concurrency queue for rendering
    this._qActive = 0;
    this._qPending = [];
  }

  async init() {
    await getPdfJs();
    if (this._destroyed) return;
    this._setupObserver();
    window.addEventListener('resize', this._onWindowResize);
    this.render();
  }

  render() {
    // Las tareas encoladas apuntan a tarjetas que se van a descartar: se limpia la
    // cola y el registro de "en curso" para que las tarjetas nuevas se encolen.
    // Las tareas que ya están corriendo terminan sobre su tarjeta vieja (inofensivo)
    // y dejan su bitmap en la caché, que la tarjeta nueva reutiliza.
    this._renderGen++;
    this._qPending = [];
    this._thumbInFlight.clear();
    this._nearCards.clear();
    this._observer?.disconnect();
    if (this.state.viewType === 'files') {
      this._renderFilesView();
    } else {
      this._renderPagesView();
    }
  }

  _findPageCard(pageId) {
    return this.container.querySelector(`.pg-card[data-page-id="${_cssEscape(pageId)}"]`);
  }

  rerenderCard(pageId) {
    const card = this._findPageCard(pageId);
    if (!card) return Promise.resolve();
    const page = this.state.pages.find(p => p.id === pageId);
    if (!page) return Promise.resolve();
    // Invalidate cache for this page (all rotations)
    this._deleteThumbCacheByPrefix(`${pageId}:`);
    card.dataset.rotation = page.rotation; // ya sincronizado: _applyCardState no re-encola
    this._applyCardState(page, card);
    delete card.dataset.rendered;
    // Solo se re-renderiza lo visible (o cercano). Las demás quedan invalidadas y
    // se renderizan cuando entran en la vista: rotar 500 páginas no dispara 500 renders.
    if (!this._nearCards.has(card)) {
      this._invalidateCard(card);
      return Promise.resolve();
    }
    return this._renderThumbnail(card);
  }

  _applyCardState(page, card) {
    if (!page || !card) return;
    card.classList.toggle('selected', page.selected);
    card.classList.toggle('deleted', page.deleted);
    const cb = card.querySelector('.pg-checkbox');
    if (cb) cb.checked = page.selected;
    const rot = card.querySelector('.rotation-badge');
    if (rot) rot.textContent = page.rotation ? `${page.rotation}°` : '';
    if (String(card.dataset.rotation) !== String(page.rotation)) {
      card.dataset.rotation = page.rotation;
      // La rotación cambió (p. ej. deshacer): la miniatura actual quedó vieja.
      this._invalidateCard(card);
    }
  }

  updateCardState(pageId, card = null) {
    const resolvedCard = card || this.container.querySelector(`[data-page-id="${pageId}"]`);
    if (!resolvedCard) return;
    const page = this.state.pages.find(p => p.id === pageId);
    this._applyCardState(page, resolvedCard);
    // Eliminar/restaurar una hoja puede cambiar la numeración de las partes.
    if (this.state.splitMode) this.updateSplitMarkers();
  }

  updateAllStates() {
    const cardsById = new Map();
    this.container.querySelectorAll('[data-page-id]').forEach(card => {
      cardsById.set(card.dataset.pageId, card);
    });
    for (const page of this.state.pages) {
      this._applyCardState(page, cardsById.get(page.id));
    }
    if (this.state.splitMode) this.updateSplitMarkers();
  }

  updateSplitMarkers() {
    const controls = [...this.container.querySelectorAll('.pg-split-ctrl')];
    const activeControls = controls
      .filter(ctrl => {
        const cutPos = Number(ctrl.dataset.cutPos);
        return this.state.hasCutAt?.(cutPos) ?? this.state.splitCuts.has(cutPos);
      })
      .sort((a, b) => Number(a.dataset.cutPos) - Number(b.dataset.cutPos));

    // Numeración igual a la del panel lateral (getOutputGroups): un corte que no
    // separa hojas activas (p. ej. todas las de un lado eliminadas) no suma parte.
    const partByCutPos = this.state.getCutPartLabels?.() ?? new Map(
      activeControls.map((ctrl, index) => [Number(ctrl.dataset.cutPos), { left: index + 1, right: index + 2 }]),
    );

    controls.forEach(ctrl => {
      const cutPos = Number(ctrl.dataset.cutPos);
      const isCut = partByCutPos.has(cutPos);
      const parts = partByCutPos.get(cutPos) || null;
      const label = ctrl.querySelector('.pg-cut-label');

      ctrl.classList.toggle('cut-active', isCut);

      if (isCut && !parts) {
        ctrl.classList.remove('cut-hover-suppressed');
        if (label) {
          label.hidden = true;
          label.textContent = '';
          label.setAttribute('aria-hidden', 'true');
          label.removeAttribute('aria-label');
        }
        ctrl.title = 'Corte sin páginas de un lado (no genera una parte)';
        ctrl.setAttribute('aria-label', ctrl.title);
      } else if (isCut) {
        ctrl.classList.remove('cut-hover-suppressed');
        if (label) {
          label.hidden = false;
          label.textContent = `${parts.left} | ${parts.right}`;
          label.setAttribute('aria-hidden', 'false');
          label.setAttribute(
            'aria-label',
            `Fin de Parte ${parts.left}. Inicio de Parte ${parts.right}.`,
          );
        }
        ctrl.title = `Fin de Parte ${parts.left} · Inicio de Parte ${parts.right}`;
        ctrl.setAttribute('aria-label', ctrl.title);
      } else {
        if (label) {
          label.hidden = true;
          label.textContent = '';
          label.setAttribute('aria-hidden', 'true');
          label.removeAttribute('aria-label');
        }
        ctrl.title = 'Marcar corte aquí';
        ctrl.setAttribute('aria-label', ctrl.title);
      }
    });
  }

  // Update split mode WITHOUT rebuilding cards — thumbnails stay intact
  updateSplitMode(splitActive) {
    const grid = this.container.querySelector('.pg-grid');
    if (!grid) {
      // No pages grid (e.g. empty or files view) — fall back to full render
      this.state.splitMode = splitActive;
      this.render();
      return;
    }

    this.state.splitMode = splitActive;
    // Los cortes NO se borran al salir del modo Separar: quedan ocultos y vuelven a
    // aparecer al reactivarlo (fuera del modo Separar no afectan ninguna exportación).

    const pages = this.state.pages;
    grid.querySelectorAll('.pg-unit').forEach(unit => {
      const slot = unit.querySelector('.pg-inter-slot');
      if (!slot) return;
      const afterIdx = parseInt(slot.dataset.afterIdx);
      if (isNaN(afterIdx) || afterIdx >= pages.length) return;
      const page = pages[afterIdx];
      if (!page) return;
      slot.replaceWith(this._buildInterSlot(page, afterIdx, splitActive));
    });

    if (splitActive) this.updateSplitMarkers();
  }

  setGridSize(size) {
    this._gridSize = size;
    const grid = this.container.querySelector('.pg-grid');
    if (grid) grid.dataset.size = size;
    // El ancho de las tarjetas cambia: re-renderizar a la resolución nueva (tras layout).
    requestAnimationFrame(() => this._refreshThumbSizes());
  }

  setViewType(type) {
    this.state.viewType = type;
    this.render();
  }

  // Agregar archivos al final en la vista Hojas: se agregan solo las tarjetas nuevas
  // en vez de reconstruir toda la grilla (y re-encolar cientos de miniaturas).
  // Cualquier caso no trivial cae en render() completo.
  appendNewPages(count) {
    const grid = this.container.querySelector('.pg-grid:not(.pg-files-grid)');
    const pages = this.state.pages;
    const total = pages.length;
    const start = total - count;
    if (this.state.viewType !== 'pages' || !grid || count <= 0 || start <= 0) { this.render(); return; }
    const units = grid.querySelectorAll(':scope > .pg-unit:not(.pg-drag-placeholder)');
    if (units.length !== start) { this.render(); return; }

    const lastUnit = units[units.length - 1];
    if (!lastUnit.querySelector('.pg-inter-slot')) {
      lastUnit.appendChild(this._buildInterSlot(pages[start - 1], start - 1, this.state.splitMode));
    }
    const frag = document.createDocumentFragment();
    const newCards = [];
    for (let i = start; i < total; i++) {
      const unit = this._buildUnit(pages[i], i, total);
      frag.appendChild(unit);
      newCards.push(unit.querySelector('.pg-card'));
    }
    grid.appendChild(frag);
    newCards.forEach(card => this._observer?.observe(card));
    if (this.state.splitMode) this.updateSplitMarkers();
  }

  destroy() {
    this._destroyed = true;
    this._renderGen++;
    if (this._observer) { this._observer.disconnect(); this._observer = null; }
    window.removeEventListener('resize', this._onWindowResize);
    clearTimeout(this._resizeTimer);
    // loadingTask.destroy() sirve tanto para documentos ya cargados como para cargas
    // en curso (las cancela), así no quedan workers huérfanos con el PDF en memoria.
    for (const task of this._pdfLoadingTasks.values()) { try { task.destroy(); } catch (_) {} }
    this._pdfLoadingTasks.clear();
    for (const p of this._pdfDocs.values()) {
      p.then(d => { try { d.destroy(); } catch (_) {} }, () => {});
    }
    this._pdfDocs.clear();
    for (const url of this._thumbUrls) { try { URL.revokeObjectURL(url); } catch (_) {} }
    this._thumbUrls.clear();
    this._thumbCache.clear();
    this._thumbCacheBytes = 0;
    this._thumbInFlight.clear();
    this._nearCards.clear();
    this._qPending = [];
  }

  getOrLoad(docId) {
    const cached = this._pdfDocs.get(docId);
    if (cached) return cached;
    const promise = (async () => {
      const lib = await getPdfJs();
      const fileUrl = await API.getFileUrl(docId);
      if (this._destroyed) throw new Error('PageGrid destruida.');
      const task = lib.getDocument({
        url: fileUrl,
        cMapUrl: CMAP_URL,
        cMapPacked: true,
        standardFontDataUrl: STANDARD_FONT_URL,
      });
      this._pdfLoadingTasks.set(docId, task);
      return task.promise;
    })();
    this._pdfDocs.set(docId, promise);
    // Si la carga falla se quita del Map para poder reintentar más adelante.
    promise.catch(() => {
      if (this._pdfDocs.get(docId) === promise) {
        this._pdfDocs.delete(docId);
        const task = this._pdfLoadingTasks.get(docId);
        this._pdfLoadingTasks.delete(docId);
        try { task?.destroy(); } catch (_) {}
      }
    });
    return promise;
  }

  // ─── Pages view ──────────────────────────────────────────────────────────────

  _renderPagesView() {
    const pages = this.state.pages;
    this.container.innerHTML = '';

    const startZone = this._buildDropZone('start');
    this.container.appendChild(startZone);

    if (pages.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'grid-empty';
      empty.innerHTML = `${svgIcon('layers', 48)}<p>No hay páginas.</p>`;
      this.container.appendChild(empty);
      return;
    }

    const grid = document.createElement('div');
    grid.className = `pg-grid${this.state.viewMode === 'list' ? ' pg-list' : ''}`;
    grid.dataset.size = this._gridSize;

    pages.forEach((page, idx) => {
      grid.appendChild(this._buildUnit(page, idx, pages.length));
    });

    this.container.appendChild(grid);
    this.container.appendChild(this._buildDropZone('end'));

    // Re-fill already-rendered cards from cache (synchronous — no visible flash).
    // Todas las tarjetas quedan observadas: el observer dispara los renders
    // pendientes de las que se acercan a la vista.
    // El ancho se mide una sola vez (todas las tarjetas miden lo mismo): medirlo por
    // tarjeta intercalado con escrituras al DOM forzaría un reflow por tarjeta.
    const firstWrap = grid.querySelector('.pg-canvas-wrap');
    const pxW = firstWrap ? this._thumbPixelWidth(firstWrap) : null;
    grid.querySelectorAll('.pg-card').forEach(card => {
      this._drawCardFromCache(card, pxW);
      this._observer?.observe(card);
    });

    this._setupDnD(grid);
    if (this.state.splitMode) this.updateSplitMarkers();
  }

  _buildDropZone(pos) {
    const zone = document.createElement('div');
    zone.className = `pg-drop-zone pg-drop-zone-${pos}`;
    return zone;
  }

  _buildUnit(page, idx, total) {
    const unit = document.createElement('div');
    unit.className = 'pg-unit';
    unit.appendChild(this._buildCard(page, idx));
    if (idx < total - 1) {
      unit.appendChild(this._buildInterSlot(page, idx, this.state.splitMode));
    }
    return unit;
  }

  _buildInterSlot(page, idx, splitMode) {
    // El corte pertenece al espacio visual entre hojas, no a una hoja.
    // cutPos = cantidad de páginas antes del corte.
    const cutPos = idx + 1;
    const isCut = splitMode && (this.state.hasCutAt?.(cutPos) ?? this.state.splitCuts.has(cutPos));
    const slot = document.createElement('div');
    slot.className = `pg-inter-slot${splitMode ? ' split-active' : ''}`;
    slot.dataset.afterIdx = String(idx);
    slot.dataset.cutPos = String(cutPos);
    slot.innerHTML = `
      <div class="pg-split-ctrl${isCut ? ' cut-active' : ''}"
           data-cut-pos="${cutPos}"
           title="Marcar corte aquí"
           aria-label="Marcar corte aquí">
        <span class="pg-cut-label" aria-hidden="${isCut ? 'false' : 'true'}"${isCut ? '' : ' hidden'}></span>
        <div class="pg-split-ctrl-line"></div>
      </div>
      <button class="pg-insert-zone-btn" title="Agregar archivo aquí" aria-label="Agregar archivo aquí">${svgIcon('plus', 11)}</button>
    `;
    if (splitMode) {
      const ctrl = slot.querySelector('.pg-split-ctrl');
      ctrl.addEventListener('click', () => {
        this.state.pushHistory?.();
        const enabled = this.state.toggleCutAt
          ? this.state.toggleCutAt(cutPos)
          : this.state.toggleCut(cutPos);

        ctrl.classList.toggle('cut-active', enabled);

        // Si se desactiva y el mouse sigue encima, evitamos que el hover azul
        // oculte visualmente que el corte se apagó. Se re-habilita al salir.
        if (!enabled) {
          ctrl.classList.add('cut-hover-suppressed');
          ctrl.addEventListener('mouseleave', () => {
            ctrl.classList.remove('cut-hover-suppressed');
          }, { once: true });
        } else {
          ctrl.classList.remove('cut-hover-suppressed');
        }

        // Recalcula todas las etiquetas para que, al agregar o quitar un corte,
        // la numeración de partes siempre coincida con el panel lateral.
        this.updateSplitMarkers();
        this.callbacks.onSplitChange?.();
      });
    }
    slot.querySelector('.pg-insert-zone-btn').addEventListener('click', () => {
      this.callbacks.onInsert?.(idx);
    });
    return slot;
  }

  _buildCard(page, idx) {
    const card = document.createElement('div');
    card.className = `pg-card${page.selected ? ' selected' : ''}${page.deleted ? ' deleted' : ''}`;
    card.dataset.pageId = page.id;
    card.dataset.docId = page.sourceDocumentId;
    card.dataset.pageNum = page.pageNum;
    card.dataset.rotation = page.rotation;
    card.dataset.idx = idx;
    card.draggable = false; // pointer-based drag — no native HTML5 drag
    // Accesible por teclado: Tab llega a la tarjeta, Enter abre el visor y Espacio
    // selecciona (o restaura, si está eliminada). Flechas ←/→ mueven el foco.
    card.tabIndex = 0;
    card.setAttribute('aria-label', `Página ${page.pageNum + 1} de ${page.sourceFilename || 'documento'}`);
    const _pageDoc = this.state.documents.get(page.sourceDocumentId);
    card.dataset.colorIdx = String(_pageDoc?.colorIndex ?? 0);

    const shortName = this._shortName(page.sourceFilename);
    const pageId = page.id;
    const getPage = () => this.state.pages.find(p => p.id === pageId);

    card.innerHTML = `
      <div class="pg-card-top">
        <input type="checkbox" class="pg-checkbox" ${page.selected ? 'checked' : ''} aria-label="Seleccionar página ${page.pageNum + 1}">
        <div class="pg-canvas-wrap loading" aria-label="Página ${page.pageNum + 1}">
          <img class="pg-thumb-img" alt="" draggable="false">
          <div class="pg-skeleton"></div>
        </div>
        <div class="pg-hover-actions">
          <button class="pg-action-btn" data-action="view" title="Ver ampliado" aria-label="Ver ampliado">${svgIcon('maximize', 13)}</button>
          <button class="pg-action-btn" data-action="rot-left" title="Rotar izquierda" aria-label="Rotar izquierda">${svgIcon('rotate-ccw', 13)}</button>
          <button class="pg-action-btn" data-action="rot-right" title="Rotar derecha" aria-label="Rotar derecha">${svgIcon('rotate-cw', 13)}</button>
          <button class="pg-action-btn danger" data-action="delete" title="Eliminar" aria-label="Eliminar página">${svgIcon('trash', 13)}</button>
        </div>
        <button class="pg-restore-btn" data-action="restore" title="Restaurar página" aria-label="Restaurar página">${svgIcon('undo', 13)} Restaurar</button>
        <div class="pg-order-badge">${idx + 1}</div>
        <div class="rotation-badge">${page.rotation ? `${page.rotation}°` : ''}</div>
      </div>
      <div class="pg-card-info">
        <div class="pg-source-name" title="${escapeHtml(page.sourceFilename)}">${escapeHtml(shortName)}</div>
        <div class="pg-page-num">Pág. ${page.pageNum + 1}</div>
      </div>
    `;

    const toggleSelection = () => {
      this.state.toggleSelect(pageId);
      this._selectionAnchorId = pageId;
      this.updateCardState(pageId, card);
      this.callbacks.onSelectionChange?.();
    };
    const rotate = (deg) => {
      const p = getPage();
      if (!p || p.deleted) return;
      this.state.pushHistory?.();
      this.state.rotatePage(pageId, deg);
      this.rerenderCard(pageId);
      this.callbacks.onPageRotate?.(pageId);
    };
    const restore = () => {
      const p = getPage();
      if (!p || !p.deleted) return;
      this.state.pushHistory?.();
      this.state.restorePage(pageId);
      this.updateCardState(pageId, card);
      this.callbacks.onPageRestore?.(pageId);
    };

    card.querySelector('.pg-checkbox').addEventListener('change', (e) => {
      e.stopPropagation();
      toggleSelection();
    });
    card.querySelector('[data-action="view"]').addEventListener('click', (e) => {
      e.stopPropagation();
      this.callbacks.onPageView?.(pageId);
    });
    card.querySelector('[data-action="rot-left"]').addEventListener('click', (e) => {
      e.stopPropagation();
      rotate(-90);
    });
    card.querySelector('[data-action="rot-right"]').addEventListener('click', (e) => {
      e.stopPropagation();
      rotate(90);
    });
    card.querySelector('[data-action="delete"]').addEventListener('click', (e) => {
      e.stopPropagation();
      this.state.pushHistory?.();
      this.state.deletePage(pageId);
      this.updateCardState(pageId, card);
      this.callbacks.onPageDelete?.(pageId);
      this.callbacks.onSelectionChange?.();
    });
    card.querySelector('[data-action="restore"]').addEventListener('click', (e) => {
      e.stopPropagation();
      restore();
    });
    // 200ms timer separates single-click (select) from dblclick (expand).
    // dblclick handler clears the timer before selection executes.
    // Con Shift (rango) o Ctrl/Cmd (alternar) no hay ambigüedad: se aplica al instante.
    let _clickTimer = null;
    card.addEventListener('click', (e) => {
      if (e.target.closest('input, button')) return;
      e.stopPropagation();
      clearTimeout(_clickTimer);
      _clickTimer = null;
      // Clic sobre una página eliminada: se restaura.
      if (getPage()?.deleted) { restore(); return; }
      if (e.shiftKey && this._selectionAnchorId && this._selectionAnchorId !== pageId) {
        this.state.selectRange(this._selectionAnchorId, pageId);
        this.updateAllStates();
        this.callbacks.onSelectionChange?.();
        return;
      }
      if (e.ctrlKey || e.metaKey || e.shiftKey) { toggleSelection(); return; }
      _clickTimer = setTimeout(() => {
        _clickTimer = null;
        toggleSelection();
      }, 200);
    });

    card.addEventListener('dblclick', (e) => {
      if (e.target.closest('input, button')) return;
      e.stopPropagation();
      clearTimeout(_clickTimer);
      _clickTimer = null;
      if (getPage()?.deleted) return;
      this.callbacks.onPageView?.(pageId);
    });

    card.addEventListener('keydown', (e) => {
      if (e.target !== card) return; // teclas sobre botones/checkbox: comportamiento nativo
      if (e.key === 'Enter') {
        e.preventDefault();
        if (getPage()?.deleted) restore();
        else this.callbacks.onPageView?.(pageId);
      } else if (e.key === ' ') {
        e.preventDefault();
        if (getPage()?.deleted) restore();
        else if (e.shiftKey && this._selectionAnchorId && this._selectionAnchorId !== pageId) {
          this.state.selectRange(this._selectionAnchorId, pageId);
          this.updateAllStates();
          this.callbacks.onSelectionChange?.();
        } else toggleSelection();
      } else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        const cards = [...this.container.querySelectorAll('.pg-card')];
        const i = cards.indexOf(card);
        const next = cards[i + (e.key === 'ArrowRight' ? 1 : -1)];
        if (next) { e.preventDefault(); next.focus(); }
      }
    });

    return card;
  }

  // ─── Files view ──────────────────────────────────────────────────────────────

  _renderFilesView() {
    const blocks = this.state.getDocumentBlocks();
    this.container.innerHTML = '';

    if (blocks.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'grid-empty';
      empty.innerHTML = `${svgIcon('layers', 48)}<p>No hay archivos.</p>`;
      this.container.appendChild(empty);
      return;
    }

    const grid = document.createElement('div');
    if (this.state.viewMode === 'grid') {
      grid.className = 'pg-files-grid pg-grid';
      grid.dataset.size = this._gridSize;
    } else {
      grid.className = 'pg-files-grid';
    }

    blocks.forEach((block, idx) => {
      grid.appendChild(this._buildFileCard(block, idx));
    });

    this.container.appendChild(grid);

    grid.querySelectorAll('.pg-file-card').forEach(card => {
      this._observer?.observe(card);
    });

    this._setupFilesDnD(grid);
  }

  _buildFileCard(block, idx) {
    const card = document.createElement('div');
    card.className = 'pg-file-card';
    card.dataset.blockIdx = String(idx);
    card.draggable = false; // pointer-based drag — no native HTML5 drag
    const _fileDoc = this.state.documents.get(block.sourceDocumentId);
    card.dataset.colorIdx = String(_fileDoc?.colorIndex ?? 0);

    const sizeStr = block.sizeBytes ? formatBytes(block.sizeBytes) : '';
    const typeLabel = { pdf: 'PDF', image: 'Imagen', office: 'Office' }[block.originalType] || 'PDF';
    const pageTxt = `${block.pages.length} ${block.pages.length === 1 ? 'pág.' : 'págs.'}`;

    card.innerHTML = `
      <div class="pg-file-thumb loading" data-doc-id="${block.sourceDocumentId}">
        <img class="pg-thumb-img" alt="" draggable="false">
      </div>
      <div class="pg-file-info">
        <div class="pg-file-name" title="${escapeHtml(block.filename)}">${escapeHtml(this._shortName(block.filename, 36))}</div>
        <div class="pg-file-meta">${pageTxt}${sizeStr ? ' · ' + sizeStr : ''}</div>
        <span class="pg-file-type">${typeLabel}</span>
      </div>
      <div class="pg-file-actions">
        <button class="pg-action-btn" data-action="expand" title="Ver páginas" aria-label="Ver páginas del archivo">${svgIcon('layers', 13)}</button>
        <button class="pg-action-btn danger" data-action="delete-block" title="Eliminar archivo" aria-label="Eliminar archivo">${svgIcon('trash', 13)}</button>
      </div>
    `;

    card.querySelector('[data-action="expand"]').addEventListener('click', e => {
      e.stopPropagation();
      this.callbacks.onExpandBlock?.(block.sourceDocumentId);
    });
    card.querySelector('[data-action="delete-block"]').addEventListener('click', e => {
      e.stopPropagation();
      this.callbacks.onDeleteBlock?.(block.pages.map(p => p.id));
    });

    return card;
  }

  _shortName(name, max = 18) {
    if (!name) return '';
    const stem = name.replace(/\.[^.]+$/, '');
    return stem.length > max ? stem.slice(0, max - 2) + '..' : stem;
  }
}

// PageGrid está repartida en varios archivos para que sea más fácil de mantener:
//   page-grid.js         — armado de vistas (Hojas / Archivos), tarjetas, cortes, carga de PDFs.
//   page-grid-thumbs.js  — miniaturas: cola de render, caché WebP, observer, precarga ociosa.
//   page-grid-dnd.js     — arrastrar y soltar (páginas y bloques) y parches de DOM al soltar.
// Los métodos se copian al prototipo como no enumerables, igual que los de una clase.
function _mixin(target, source) {
  for (const [name, desc] of Object.entries(Object.getOwnPropertyDescriptors(source))) {
    if (Object.prototype.hasOwnProperty.call(target, name)) {
      throw new Error(`PageGrid: el método ${name} ya existe.`);
    }
    Object.defineProperty(target, name, { ...desc, enumerable: false });
  }
}
_mixin(PageGrid.prototype, thumbMethods);
_mixin(PageGrid.prototype, dndMethods);
