import { svgIcon } from '../js/utils.js';

// Arrastrar y soltar de PageGrid (vista Hojas y vista Archivos) por eventos de
// puntero, más los "parches" de DOM que reordenan tarjetas sin reconstruir la
// grilla. Se mezcla en PageGrid.prototype (ver page-grid.js): los métodos usan
// `this` igual que si estuvieran declarados en la clase.

// Returns nearest scrollable ancestor, or documentElement as fallback.
function _findScrollParent(el) {
  let node = el.parentElement;
  while (node && node !== document.documentElement) {
    const oy = getComputedStyle(node).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight) return node;
    node = node.parentElement;
  }
  return document.documentElement;
}

// ─── Helpers compartidos por el arrastre de páginas y el de bloques ──────────

const AUTOSCROLL_ZONE = 60;
const AUTOSCROLL_SPEED = 8;

// Ubica el ghost (etiqueta flotante del arrastre) junto al puntero.
function _positionGhost(ghost, x, y) {
  if (ghost) ghost.style.transform = `translate3d(${x + 14}px,${y - 10}px,0)`;
}

// Autoscroll al acercar el puntero al borde del contenedor, y rueda del mouse
// durante el arrastre. `drag` es el estado del arrastre (active, scrollEl,
// lastX/lastY, autoScrollRaf); `refreshAt(el)` recalcula el destino según el
// elemento que quedó bajo el puntero después de desplazar.
function _createDragScroller(drag, refreshAt) {
  const refreshAtPointer = () => {
    const el = document.elementFromPoint(drag.lastX, drag.lastY);
    if (el) refreshAt(el);
  };
  const autoScroll = () => {
    if (!drag.active) return;
    const scrollEl = drag.scrollEl;
    const isRoot = scrollEl === document.documentElement;
    const scrollRect = isRoot ? null : scrollEl.getBoundingClientRect();
    const rectTop = isRoot ? 0 : scrollRect.top;
    const rectBottom = isRoot ? window.innerHeight : scrollRect.bottom;
    const y = drag.lastY;
    if (y < rectTop + AUTOSCROLL_ZONE) {
      scrollEl.scrollTop -= AUTOSCROLL_SPEED;
      refreshAtPointer();
    } else if (y > rectBottom - AUTOSCROLL_ZONE) {
      scrollEl.scrollTop += AUTOSCROLL_SPEED;
      refreshAtPointer();
    }
    drag.autoScrollRaf = requestAnimationFrame(autoScroll);
  };
  const onWheel = (ev) => {
    if (!drag.active) return;
    ev.preventDefault();
    drag.scrollEl.scrollTop += ev.deltaY;
    requestAnimationFrame(refreshAtPointer);
  };
  return { autoScroll, onWheel };
}

export const dndMethods = {
  _setupDnD(grid) {
    const DRAG_THRESHOLD_SQ = 64; // 8px² — squared avoids sqrt; distance must be crossed while held
    const MIN_DRAG_HOLD_MS  = 120; // ms button must be held before drag detection starts

    const drag = {
      active: false, starting: false,
      fromIdx: null, dragGroup: null,
      ghost: null, placeholder: null,
      scrollEl: null, phRef: null, phBefore: null,
      lastX: 0, lastY: 0, startX: 0, startY: 0,
      pointerId: null, autoScrollRaf: null,
      cachedMidX: null, lastTarget: null,
      startTime: 0,
    };

    const _removePlaceholder = () => {
      if (drag.placeholder?.parentElement) drag.placeholder.remove();
      drag.phRef = null;
      drag.phBefore = null;
    };

    const _getUnitsBefore = (ph) => {
      let count = 0;
      for (const child of ph.parentElement.children) {
        if (child === ph) break;
        if (
          child.classList.contains('pg-unit')
          && !child.classList.contains('pg-drag-placeholder')
        ) count++;
      }
      return count;
    };

    // Inserta un recuadro real en la grilla. Ocupa exactamente el lugar de una
    // hoja y fuerza el espacio de destino, pero no tiene transición ni animación.
    const _insertPlaceholderAt = (refUnit, before) => {
      if (!refUnit || !drag.placeholder) return;
      if (drag.phRef === refUnit && drag.phBefore === before) return;

      drag.phRef = refUnit;
      drag.phBefore = before;

      const ph = drag.placeholder;
      const unitRect = refUnit.getBoundingClientRect();
      const refCard = refUnit.querySelector('.pg-card');
      const cardRect = refCard?.getBoundingClientRect() || unitRect;

      ph.style.width = `${Math.round(unitRect.width)}px`;
      ph.style.height = `${Math.round(unitRect.height)}px`;
      ph.style.flexBasis = `${Math.round(unitRect.width)}px`;
      ph.style.setProperty('--pg-placeholder-card-width', `${Math.round(cardRect.width)}px`);
      ph.style.setProperty('--pg-placeholder-card-height', `${Math.round(cardRect.height)}px`);

      if (before) grid.insertBefore(ph, refUnit);
      else refUnit.after(ph);
    };

    // Returns true when dropping at insertIdx would not change page order.
    const _isNoopDrop = (insertIdx) => {
      if (!drag.dragGroup) {
        const f = drag.fromIdx;
        return insertIdx === f || insertIdx === f + 1;
      }
      const sorted = [...drag.dragGroup].sort((a, b) => a - b);
      const removedBefore = sorted.filter(i => i < insertIdx).length;
      return (insertIdx - removedBefore) === sorted[0];
    };

    // e.target ya viene resuelto por Chromium. El placeholder solo se mueve
    // cuando cambia realmente el destino, evitando trabajo innecesario.
    const _updateIndicatorFromTarget = (target, x) => {
      if (!drag.active || !target) return;
      if (
        target.closest('.pg-drag-ghost')
        || target.closest('.pg-drag-placeholder')
      ) return;

      const slot = target.closest('.pg-inter-slot');
      if (slot && grid.contains(slot)) {
        const afterIdx = parseInt(slot.dataset.afterIdx);
        const insertIdx = afterIdx + 1;
        if (_isNoopDrop(insertIdx)) {
          _removePlaceholder();
          return;
        }
        const parentUnit = slot.closest('.pg-unit');
        if (parentUnit) _insertPlaceholderAt(parentUnit, false);
        return;
      }

      const card = target.closest('.pg-card');
      if (
        card
        && grid.contains(card)
        && !card.classList.contains('deleted')
        && !card.classList.contains('dragging')
      ) {
        const cardIdx = parseInt(card.dataset.idx);
        let midX = drag.cachedMidX?.get(cardIdx);
        if (midX === undefined) {
          const r = card.getBoundingClientRect();
          midX = r.left + r.width / 2;
          drag.cachedMidX?.set(cardIdx, midX);
        }

        const insertBefore = x < midX;
        const insertIdx = insertBefore ? cardIdx : cardIdx + 1;
        if (_isNoopDrop(insertIdx)) {
          _removePlaceholder();
          return;
        }
        _insertPlaceholderAt(card.parentElement, insertBefore);
      }
    };

    const _updateGhost = (x, y) => _positionGhost(drag.ghost, x, y);

    // En la grilla de hojas el destino depende de la X (antes/después de la tarjeta).
    const { autoScroll: _autoScroll, onWheel: _onWheel } =
      _createDragScroller(drag, (el) => _updateIndicatorFromTarget(el, drag.lastX));

    const _cleanup = () => {
      if (drag.ghost) { drag.ghost.remove(); drag.ghost = null; }
      _removePlaceholder();
      drag.placeholder = null;
      drag.cachedMidX = null;
      drag.lastTarget = null;
      document.body.classList.remove('artemis-dragging');
      grid.querySelectorAll('.pg-card.dragging').forEach(c => c.classList.remove('dragging'));
      if (drag.autoScrollRaf) { cancelAnimationFrame(drag.autoScrollRaf); drag.autoScrollRaf = null; }
      window.removeEventListener('wheel', _onWheel);
      window.removeEventListener('pointermove', _onPointerMove);
      window.removeEventListener('pointerup', _onPointerUp);
      window.removeEventListener('pointercancel', _onPointerCancel);
      window.removeEventListener('keydown', _onKeyDown);
      window.removeEventListener('blur', _cleanup);
      drag.active = false;
      drag.starting = false;
      drag.fromIdx = null;
      drag.dragGroup = null;
      drag.scrollEl = null;
      drag.pointerId = null;
      drag.startTime = 0;
      _rafIndicatorPending = false; // defensive: prevent stale RAF from blocking next drag
    };

    const _activateDrag = (card) => {
      drag.active = true;
      const cardIdx = drag.fromIdx;
      const page = this.state.pages[cardIdx];
      let dragGroup = null;
      if (page?.selected && this.state.selectedCount > 1) {
        dragGroup = this.state.pages
          .map((p, i) => ({ p, i }))
          .filter(({ p }) => p.selected && !p.deleted)
          .map(({ i }) => i);
      }
      drag.dragGroup = dragGroup;
      drag.scrollEl = _findScrollParent(grid);
      document.body.classList.add('artemis-dragging');

      // ── Ghost first: text-only, all styles in CSS, zero inline style mutations ──
      const ghost = document.createElement('div');
      ghost.className = 'pg-drag-ghost';
      if (dragGroup && dragGroup.length > 1) {
        ghost.textContent = `${dragGroup.length} páginas`;
        dragGroup.forEach(gIdx => {
          const gCard = grid.querySelector(`[data-idx="${gIdx}"]`);
          if (gCard) gCard.classList.add('dragging');
        });
      } else {
        ghost.textContent = `Pág. ${(page?.pageNum ?? 0) + 1}`;
        card.classList.add('dragging');
      }
      document.body.appendChild(ghost);
      drag.ghost = ghost;
      _updateGhost(drag.lastX, drag.lastY);

      // Placeholder real pero liviano: crea el espacio de destino igual que
      // SmallPDF, sin animaciones, escalados ni transiciones.
      const phCount = dragGroup?.length || 1;
      const ph = document.createElement('div');
      ph.className = 'pg-unit pg-drag-placeholder';
      ph.innerHTML = `
        <div class="pg-placeholder-card">
          ${phCount > 1 ? `<span class="pg-placeholder-count">${phCount}</span>` : ''}
        </div>
      `;
      drag.placeholder = ph;

      // midX cache starts empty — populated lazily per card on first hover.
      drag.cachedMidX = new Map();

      drag.autoScrollRaf = requestAnimationFrame(_autoScroll);
      window.addEventListener('wheel', _onWheel, { passive: false });
    };

    let _rafIndicatorPending = false;
    const _onPointerMove = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      if (drag.active) {
        e.preventDefault();
        _updateGhost(e.clientX, e.clientY);
        drag.lastTarget = e.target;
        if (!_rafIndicatorPending) {
          _rafIndicatorPending = true;
          requestAnimationFrame(() => {
            _updateIndicatorFromTarget(drag.lastTarget, drag.lastX);
            _rafIndicatorPending = false;
          });
        }
      } else if (drag.starting) {
        if (e.buttons !== 1) { _cleanup(); return; } // button released — click, not drag
        if (performance.now() - drag.startTime < MIN_DRAG_HOLD_MS) return; // too fast — click, not drag
        const dx = e.clientX - drag.startX;
        const dy = e.clientY - drag.startY;
        if (dx * dx + dy * dy >= DRAG_THRESHOLD_SQ) {
          drag.starting = false;
          const card = grid.querySelector(`[data-idx="${drag.fromIdx}"]`);
          if (card && !card.classList.contains('deleted')) {
            _activateDrag(card);
          } else {
            _cleanup();
          }
        }
      }
    };

    const _onPointerUp = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      if (drag.active && drag.placeholder?.parentElement) {
        const insertIdx = _getUnitsBefore(drag.placeholder);
        const fromIdx = drag.fromIdx;
        const dragGroup = drag.dragGroup;
        const noop = _isNoopDrop(insertIdx);
        _cleanup();
        if (noop) return;

        if (dragGroup && dragGroup.length > 1) {
          this.state.pushHistory?.();
          this.state.movePagesTo(dragGroup, insertIdx);
          this.callbacks.onReorder?.();
          if (!this._patchDomAfterMultiDrop(dragGroup, insertIdx)) {
            this.render();
          }
        } else if (fromIdx != null) {
          let adj = insertIdx;
          if (adj > fromIdx) adj--;
          if (adj !== fromIdx) {
            this.state.pushHistory?.();
            this.state.movePageTo(fromIdx, adj);
            this.callbacks.onReorder?.();
            // DOM patch inmediato: sin reconstrucción ni transición visual.
            if (!this._patchDomAfterDrop(fromIdx, adj)) {
              this.render();
            }
          }
        }
      } else {
        _cleanup();
      }
    };

    const _onPointerCancel = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      _cleanup();
    };

    const _onKeyDown = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); _cleanup(); }
    };

    // Listener delegado en la grilla: cubre también las páginas restauradas después
    // del último render (antes solo se enganchaba a las tarjetas no eliminadas).
    grid.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      if (drag.active || drag.starting) return;
      if (e.target.closest('input, button')) return;
      const card = e.target.closest('.pg-card');
      if (!card || !grid.contains(card) || card.classList.contains('deleted')) return;
      drag.starting = true;
      drag.startTime = performance.now();
      drag.fromIdx = parseInt(card.dataset.idx);
      drag.startX = e.clientX;
      drag.startY = e.clientY;
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      drag.pointerId = e.pointerId;
      window.addEventListener('pointermove', _onPointerMove);
      window.addEventListener('pointerup', _onPointerUp);
      window.addEventListener('pointercancel', _onPointerCancel);
      window.addEventListener('keydown', _onKeyDown);
      window.addEventListener('blur', _cleanup, { once: true });
    });
  },

  // ─── DOM patch: move single page without full rebuild ────────────────────────

  _patchDomAfterDrop(fromIdx, toIdx) {
    try {
      const grid = this.container.querySelector('.pg-grid');
      if (!grid) return false;

      const units = [...grid.querySelectorAll(':scope > .pg-unit:not(.pg-drag-placeholder)')];
      const total = units.length;

      // Validate DOM/state consistency before touching anything
      if (total !== this.state.pages.length) return false;
      if (fromIdx < 0 || fromIdx >= total || toIdx < 0 || toIdx >= total) return false;

      const movingUnit = units[fromIdx];
      if (!movingUnit) return false;

      // Move unit in DOM (insertBefore/after handles the move atomically)
      if (fromIdx < toIdx) {
        units[toIdx].after(movingUnit);
      } else {
        units[toIdx].before(movingUnit);
      }

      this._syncUnitsAfterPatch(grid, total);

      return true;
    } catch (_) {
      return false;
    }
  },

  // ─── Shared reindex helper for DOM patches ───────────────────────────────────

  _syncUnitsAfterPatch(grid, total) {
    const units = [...grid.querySelectorAll(':scope > .pg-unit:not(.pg-drag-placeholder)')];
    units.forEach((unit, newIdx) => {
      const card = unit.querySelector('.pg-card');
      if (!card) return;

      card.dataset.idx = String(newIdx);

      const badge = card.querySelector('.pg-order-badge');
      if (badge) badge.textContent = String(newIdx + 1);

      const slot = unit.querySelector('.pg-inter-slot');
      if (newIdx === total - 1) {
        if (slot) slot.remove();
      } else {
        const page = this.state.pages[newIdx];
        const freshSlot = this._buildInterSlot(page, newIdx, this.state.splitMode);
        if (slot) slot.replaceWith(freshSlot);
        else unit.appendChild(freshSlot);
      }
    });

    if (this.state.splitMode) this.updateSplitMarkers();
  },

  // ─── DOM patch: move multi-select group without full rebuild ──────────────────

  _patchDomAfterMultiDrop(dragIndices, insertIdx) {
    try {
      const grid = this.container.querySelector('.pg-grid');
      if (!grid) return false;

      const units = [...grid.querySelectorAll(':scope > .pg-unit:not(.pg-drag-placeholder)')];
      const total = units.length;

      if (total !== this.state.pages.length) return false;

      const sorted = [...dragIndices].sort((a, b) => a - b);
      if (sorted.some(i => i < 0 || i >= total)) return false;

      // Mirror state.movePagesTo adjTo: remove pages, adjust insertion point
      const removedBefore = sorted.filter(i => i < insertIdx).length;
      const adjTo = Math.max(0, Math.min(insertIdx - removedBefore, total - sorted.length));

      const movingUnits = sorted.map(i => units[i]);
      if (movingUnits.some(u => !u)) return false;

      const sortedSet = new Set(sorted);
      const nonMovingUnits = units.filter((_, i) => !sortedSet.has(i));
      const refNode = nonMovingUnits[adjTo] ?? null;

      // Detach moving units into fragment (preserves their relative order)
      const fragment = document.createDocumentFragment();
      for (const unit of movingUnits) fragment.appendChild(unit);

      if (refNode) {
        grid.insertBefore(fragment, refNode);
      } else {
        grid.appendChild(fragment);
      }

      this._syncUnitsAfterPatch(grid, total);

      return true;
    } catch (_) {
      return false;
    }
  },

  // ─── DOM patch: move single file-block without full rebuild ─────────────────

  _patchFilesAfterDrop(fromBlockIdx, toIdx, before) {
    try {
      const grid = this.container.querySelector('.pg-files-grid');
      if (!grid) return false;

      const cards = [...grid.querySelectorAll('.pg-file-card')];
      const total = cards.length;

      if (total !== this.state.getDocumentBlocks().length) return false;
      if (fromBlockIdx < 0 || fromBlockIdx >= total) return false;
      if (toIdx < 0 || toIdx >= total) return false;

      const movingCard = cards[fromBlockIdx];
      if (!movingCard) return false;

      const targetCard = cards[toIdx];
      if (before) {
        targetCard.before(movingCard);
      } else {
        targetCard.after(movingCard);
      }

      // Update data-block-idx on all cards in new order
      const newCards = [...grid.querySelectorAll('.pg-file-card')];
      newCards.forEach((card, newIdx) => {
        card.dataset.blockIdx = String(newIdx);
      });

      return true;
    } catch (_) {
      return false;
    }
  },

  _setupFilesDnD(grid) {
    const DRAG_THRESHOLD = 4;

    const drag = {
      active: false, starting: false,
      fromBlockIdx: null, ghost: null,
      scrollEl: null, dropTarget: null,
      lastX: 0, lastY: 0, startX: 0, startY: 0,
      pointerId: null, autoScrollRaf: null,
      lastTarget: null,
    };

    let _placeholder = null;

    const _removePlaceholder = () => {
      if (_placeholder) { _placeholder.remove(); _placeholder = null; }
    };

    // Uses e.target (no layout flush) — same approach as page drag.
    // pg-file-drop-ph has pointer-events:none so target passes through it.
    const _updateIndicatorFromTarget = (target, y) => {
      if (!drag.active || !target) return;
      if (target.closest('.pg-drag-ghost') || target.closest('.pg-file-drop-ph')) return;
      const fc = target.closest('.pg-file-card');
      if (!fc || !grid.contains(fc) || fc.classList.contains('dragging')) return;
      const rect = fc.getBoundingClientRect();
      const before = y < rect.top + rect.height / 2;
      const blockIdx = parseInt(fc.dataset.blockIdx);
      if (drag.dropTarget?.blockIdx === blockIdx && drag.dropTarget?.before === before) return;
      drag.dropTarget = { blockIdx, before };
      _removePlaceholder();
      const ph = document.createElement('div');
      ph.className = 'pg-file-drop-ph';
      if (before) grid.insertBefore(ph, fc);
      else fc.after(ph);
      _placeholder = ph;
    };

    const _updateGhost = (x, y) => _positionGhost(drag.ghost, x, y);

    // En la lista de archivos el destino depende de la Y (arriba/abajo del bloque).
    const { autoScroll: _autoScroll, onWheel: _onWheel } =
      _createDragScroller(drag, (el) => _updateIndicatorFromTarget(el, drag.lastY));

    const _cleanup = () => {
      if (drag.ghost) { drag.ghost.remove(); drag.ghost = null; }
      _removePlaceholder();
      document.body.classList.remove('artemis-dragging');
      grid.querySelectorAll('.pg-file-card.dragging').forEach(c => c.classList.remove('dragging'));
      if (drag.autoScrollRaf) { cancelAnimationFrame(drag.autoScrollRaf); drag.autoScrollRaf = null; }
      window.removeEventListener('wheel', _onWheel);
      window.removeEventListener('pointermove', _onPointerMove);
      window.removeEventListener('pointerup', _onPointerUp);
      window.removeEventListener('pointercancel', _onPointerCancel);
      window.removeEventListener('keydown', _onKeyDown);
      drag.active = false;
      drag.starting = false;
      drag.fromBlockIdx = null;
      drag.scrollEl = null;
      drag.dropTarget = null;
      drag.pointerId = null;
      drag.lastTarget = null;
    };

    const _activateDrag = (card) => {
      drag.active = true;
      drag.scrollEl = _findScrollParent(grid);
      document.body.classList.add('artemis-dragging');
      card.classList.add('dragging');

      const fname = card.querySelector('.pg-file-name')?.textContent?.trim() || 'Archivo';
      const short = fname.length > 22 ? fname.slice(0, 22) + '..' : fname;
      const ghost = document.createElement('div');
      ghost.className = 'pg-drag-ghost';
      ghost.style.cssText = 'position:fixed;display:flex;align-items:center;gap:6px;background:#2A6FE0;color:#fff;border-radius:20px;padding:6px 14px;font-size:12px;font-weight:700;box-shadow:0 4px 20px rgba(42,111,224,0.45);pointer-events:none;z-index:9999;';
      // El nombre del archivo va por textContent (nunca innerHTML): no se interpreta.
      ghost.innerHTML = svgIcon('file-text', 14);
      const ghostLabel = document.createElement('span');
      ghostLabel.textContent = short;
      ghost.appendChild(ghostLabel);
      document.body.appendChild(ghost);
      drag.ghost = ghost;
      _updateGhost(drag.lastX, drag.lastY);

      drag.autoScrollRaf = requestAnimationFrame(_autoScroll);
      window.addEventListener('wheel', _onWheel, { passive: false });
    };

    let _rafFilesIndicatorPending = false;
    const _onPointerMove = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      if (drag.active) {
        e.preventDefault();
        _updateGhost(e.clientX, e.clientY);
        drag.lastTarget = e.target;
        // RAF-throttle indicator updates — e.target already hit-tested, no layout flush.
        if (!_rafFilesIndicatorPending) {
          _rafFilesIndicatorPending = true;
          requestAnimationFrame(() => {
            _updateIndicatorFromTarget(drag.lastTarget, drag.lastY);
            _rafFilesIndicatorPending = false;
          });
        }
      } else if (drag.starting) {
        if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) > DRAG_THRESHOLD) {
          drag.starting = false;
          const card = grid.querySelector(`[data-block-idx="${drag.fromBlockIdx}"]`);
          if (card) {
            _activateDrag(card);
          } else {
            _cleanup();
          }
        }
      }
    };

    const _onPointerUp = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      if (drag.active && drag.dropTarget !== null) {
        const { blockIdx: toIdx, before } = drag.dropTarget;
        const fromBlockIdx = drag.fromBlockIdx;
        _cleanup();
        if (fromBlockIdx !== null && fromBlockIdx !== toIdx) {
          const insertIdx = before ? toIdx : toIdx + 1;
          this.state.pushHistory?.();
          const orderBefore = this.state.pages.map(p => p.id).join('|');
          this.state.moveBlockTo(fromBlockIdx, insertIdx);
          // moveBlockTo puede no mover nada (soltar en el mismo lugar): no dejar un
          // paso de deshacer vacío.
          if (this.state.pages.map(p => p.id).join('|') === orderBefore) {
            this.state.dropLastHistory?.();
            return;
          }
          this.callbacks.onReorder?.();
          if (!this._patchFilesAfterDrop(fromBlockIdx, toIdx, before)) {
            this.render();
          }
        }
      } else {
        _cleanup();
      }
    };

    const _onPointerCancel = (e) => {
      if (e.pointerId !== drag.pointerId) return;
      _cleanup();
    };

    const _onKeyDown = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); _cleanup(); }
    };

    grid.querySelectorAll('.pg-file-card').forEach(card => {
      card.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        if (drag.active || drag.starting) return;
        if (e.target.closest('button')) return;
        drag.starting = true;
        drag.fromBlockIdx = parseInt(card.dataset.blockIdx);
        drag.startX = e.clientX;
        drag.startY = e.clientY;
        drag.lastX = e.clientX;
        drag.lastY = e.clientY;
        drag.pointerId = e.pointerId;
        window.addEventListener('pointermove', _onPointerMove);
        window.addEventListener('pointerup', _onPointerUp);
        window.addEventListener('pointercancel', _onPointerCancel);
        window.addEventListener('keydown', _onKeyDown);
      });
    });
  },
};
