// Límite de pasos de deshacer. Cada snapshot son copias planas de las páginas
// (objetos chicos), así que 50 pasos con cientos de páginas siguen siendo baratos.
const MAX_HISTORY = 50;

export class WorkspaceState {
  constructor() {
    this.documents = new Map();
    this.pages = [];
    this.viewMode = 'grid';
    this.viewType = 'pages'; // 'pages' | 'files'
    this.splitMode = false;
    // Posiciones de corte, no IDs de hoja.
    // Cada número indica cuántas páginas quedan antes del corte: 1 = corte después de la primera página.
    this.splitCuts = new Set();
    // Identidad estable de cada corte (posición → id). La parte que empieza en un
    // corte se identifica por ese id (la primera parte, por 'start'): así el nombre
    // y la compresión de cada parte no se corren al mover, eliminar o insertar hojas.
    this._cutIds = new Map();
    this._cutSeq = 0;
    this.selectedIds = new Set();

    // Deshacer/rehacer: pilas de snapshots {pages, splitCuts, documents}.
    this._undoStack = [];
    this._redoStack = [];
    // Callback opcional (organize.js) para refrescar los botones de deshacer/rehacer.
    this.onHistoryChange = null;
  }

  // ─── Deshacer / rehacer ──────────────────────────────────────────────────────

  _snapshot() {
    return {
      pages: this.pages.map(p => ({ ...p })),
      splitCuts: [...this.splitCuts],
      cutIds: [...this._cutIds],
      documents: new Map(this.documents),
    };
  }

  _restoreSnapshot(snap) {
    this.pages = snap.pages.map(p => ({ ...p }));
    this.splitCuts = new Set(snap.splitCuts);
    this._cutIds = new Map(snap.cutIds || []);
    this.documents = new Map(snap.documents);
    this.selectedIds = new Set(this.pages.filter(p => p.selected && !p.deleted).map(p => p.id));
    this._normalizeCutPositions();
  }

  // Registrar ANTES de cada cambio deshacible (eliminar, rotar, reordenar, cortar,
  // restaurar, agregar archivos). Un cambio nuevo invalida la pila de rehacer.
  pushHistory() {
    this._undoStack.push(this._snapshot());
    if (this._undoStack.length > MAX_HISTORY) this._undoStack.shift();
    this._redoStack = [];
    this.onHistoryChange?.();
  }

  // Descarta el último snapshot registrado (p. ej. la acción terminó sin cambios).
  dropLastHistory() {
    this._undoStack.pop();
    this.onHistoryChange?.();
  }

  get canUndo() { return this._undoStack.length > 0; }
  get canRedo() { return this._redoStack.length > 0; }

  undo() {
    const snap = this._undoStack.pop();
    if (!snap) return false;
    this._redoStack.push(this._snapshot());
    this._restoreSnapshot(snap);
    this.onHistoryChange?.();
    return true;
  }

  redo() {
    const snap = this._redoStack.pop();
    if (!snap) return false;
    this._undoStack.push(this._snapshot());
    this._restoreSnapshot(snap);
    this.onHistoryChange?.();
    return true;
  }

  clearHistory() {
    this._undoStack = [];
    this._redoStack = [];
    this.onHistoryChange?.();
  }

  get activePages() {
    return this.pages.filter(p => !p.deleted);
  }

  get selectedCount() {
    return this.selectedIds.size;
  }

  addDocument(docData) {
    const doc = {
      id: docData.document_id || docData.converted_pdf_id,
      filename: docData.filename,
      originalType: docData.original_type || 'pdf',
      convertedPdfId: docData.converted_pdf_id || docData.document_id,
      pages: docData.pages,
      sizeBytes: docData.size_bytes || 0,
      status: docData.status || 'ready',
      colorIndex: this.documents.size % 8,
    };
    this.documents.set(doc.id, doc);
    return doc;
  }

  appendPages(doc) {
    const newPages = this._makePages(doc);
    this.pages.push(...newPages);
  }

  insertPagesAfterIndex(insertAfterIdx, doc) {
    const newPages = this._makePages(doc);
    const insertPos = Math.max(0, Math.min(insertAfterIdx + 1, this.pages.length));
    this._shiftCutsForInsert(insertPos, newPages.length);
    this.pages.splice(insertPos, 0, ...newPages);
    this._normalizeCutPositions();
  }

  _makePages(doc) {
    return Array.from({ length: doc.pages }, (_, i) => ({
      id: `${doc.convertedPdfId}_${i}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
      sourceDocumentId: doc.convertedPdfId,
      sourceFilename: doc.filename,
      pageNum: i,
      rotation: 0,
      deleted: false,
      selected: false,
    }));
  }

  movePageTo(fromIdx, toIdx) {
    if (fromIdx === toIdx) return;
    if (fromIdx < 0 || fromIdx >= this.pages.length) return;
    if (toIdx < 0 || toIdx >= this.pages.length) return;

    // movePageTo recibe el índice final ya ajustado por el DnD.
    // Para conservar los cortes como posiciones fijas, reconstruimos el punto
    // de inserción original antes de quitar la página.
    const originalInsertIdx = toIdx > fromIdx ? toIdx + 1 : toIdx;
    this._movePagesToPreservingCuts([fromIdx], originalInsertIdx);
  }

  movePagesTo(fromIndices, toIdx) {
    this._movePagesToPreservingCuts(fromIndices, toIdx);
  }

  _movePagesToPreservingCuts(fromIndices, toIdx) {
    const n = this.pages.length;
    if (!n) return;

    const sorted = [...new Set(fromIndices)]
      .filter(i => Number.isInteger(i) && i >= 0 && i < n)
      .sort((a, b) => a - b);

    if (!sorted.length) return;

    const movingSet = new Set(sorted);
    const cutsBefore = new Set(this.splitCuts);

    // Representamos los cortes como marcadores independientes entre páginas.
    // Al mover páginas, se mueven solo las páginas; los marcadores quedan en la
    // posición visual donde fueron puestos.
    const sequence = [];
    for (let i = 0; i < n; i++) {
      sequence.push({ type: 'page', page: this.pages[i], originalIndex: i });
      if (cutsBefore.has(i + 1)) {
        sequence.push({ type: 'cut', id: this._cutIdAt(i + 1) });
      }
    }

    const movingTokens = [];
    const remaining = [];
    for (const item of sequence) {
      if (item.type === 'page' && movingSet.has(item.originalIndex)) {
        movingTokens.push(item);
      } else {
        remaining.push(item);
      }
    }

    const removedBefore = sorted.filter(i => i < toIdx).length;
    const remainingPageCount = n - sorted.length;
    const adjTo = Math.max(0, Math.min(toIdx - removedBefore, remainingPageCount));

    let insertAt = 0;
    if (adjTo > 0) {
      let pagesSeen = 0;
      insertAt = remaining.length;
      for (let i = 0; i < remaining.length; i++) {
        if (remaining[i].type === 'page') {
          pagesSeen++;
          if (pagesSeen === adjTo) {
            insertAt = i + 1;
            // Si justo hay un corte en ese límite, insertamos después del corte
            // para no empujarlo ni hacerlo viajar con la página movida.
            while (insertAt < remaining.length && remaining[insertAt].type === 'cut') {
              insertAt++;
            }
            break;
          }
        }
      }
    }

    remaining.splice(insertAt, 0, ...movingTokens);

    const newPages = [];
    const newCuts = new Set();
    const newIds = new Map();
    let pagesBeforeCut = 0;

    for (const item of remaining) {
      if (item.type === 'page') {
        newPages.push(item.page);
        pagesBeforeCut++;
      } else if (item.type === 'cut') {
        if (pagesBeforeCut > 0 && pagesBeforeCut < n && !newCuts.has(pagesBeforeCut)) {
          newCuts.add(pagesBeforeCut);
          newIds.set(pagesBeforeCut, item.id);
        }
      }
    }

    this.pages = newPages;
    this.splitCuts = newCuts;
    this._cutIds = newIds;
    this._normalizeCutPositions();
  }

  _shiftCutsForInsert(insertPos, count) {
    if (!count || this.splitCuts.size === 0) return;
    const shifted = new Set();
    const shiftedIds = new Map();
    const finalLen = this.pages.length + count;
    for (const pos of this.splitCuts) {
      const next = pos >= insertPos ? pos + count : pos;
      if (next > 0 && next < finalLen) {
        shifted.add(next);
        shiftedIds.set(next, this._cutIdAt(pos));
      }
    }
    this.splitCuts = shifted;
    this._cutIds = shiftedIds;
  }

  _normalizeCutPositions() {
    if (this.splitCuts.size === 0) return;
    const max = Math.max(0, this.pages.length - 1);
    const normalized = new Set();
    const ids = new Map();
    for (const pos of this.splitCuts) {
      const n = Number(pos);
      if (Number.isInteger(n) && n > 0 && n <= max) {
        normalized.add(n);
        ids.set(n, this._cutIds.get(pos) ?? this._cutIds.get(n) ?? this._newCutId());
      }
    }
    this.splitCuts = normalized;
    this._cutIds = ids;
  }

  _newCutId() {
    this._cutSeq += 1;
    return `cut-${this._cutSeq}-${Math.random().toString(36).slice(2, 6)}`;
  }

  // Id del corte en esa posición (lo crea si un caller agregó el corte a mano).
  _cutIdAt(pos) {
    let id = this._cutIds.get(pos);
    if (!id) {
      id = this._newCutId();
      this._cutIds.set(pos, id);
    }
    return id;
  }

  toggleSelect(pageId) {
    const page = this.pages.find(p => p.id === pageId);
    if (!page) return;
    if (this.selectedIds.has(pageId)) {
      this.selectedIds.delete(pageId);
      page.selected = false;
    } else {
      this.selectedIds.add(pageId);
      page.selected = true;
    }
  }

  selectOnly(pageId) {
    this.clearSelection();
    const page = this.pages.find(p => p.id === pageId);
    if (page && !page.deleted) {
      page.selected = true;
      this.selectedIds.add(pageId);
    }
  }

  // Shift+clic: agrega a la selección todas las páginas activas entre ambas (inclusive).
  selectRange(fromId, toId) {
    const a = this.pages.findIndex(p => p.id === fromId);
    const b = this.pages.findIndex(p => p.id === toId);
    if (a < 0 || b < 0) return;
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    for (let i = lo; i <= hi; i++) {
      const p = this.pages[i];
      if (!p.deleted) { p.selected = true; this.selectedIds.add(p.id); }
    }
  }

  selectAll() {
    this.pages.forEach(p => { if (!p.deleted) { p.selected = true; this.selectedIds.add(p.id); } });
  }

  clearSelection() {
    this.pages.forEach(p => { p.selected = false; });
    this.selectedIds.clear();
  }

  rotateSelected(deg) {
    for (const id of this.selectedIds) {
      const p = this.pages.find(p => p.id === id);
      if (p && !p.deleted) p.rotation = (p.rotation + deg + 360) % 360;
    }
  }

  rotatePage(pageId, deg) {
    const p = this.pages.find(p => p.id === pageId);
    if (p) p.rotation = (p.rotation + deg + 360) % 360;
  }

  deleteSelected() {
    for (const id of this.selectedIds) {
      const p = this.pages.find(p => p.id === id);
      if (p) { p.deleted = true; p.selected = false; }
    }
    this.selectedIds.clear();
  }

  deletePage(pageId) {
    const p = this.pages.find(p => p.id === pageId);
    if (p) {
      p.deleted = true;
      p.selected = false;
      this.selectedIds.delete(pageId);
    }
  }

  restorePage(pageId) {
    const p = this.pages.find(p => p.id === pageId);
    if (p) p.deleted = false;
  }

  restoreAll() {
    this.pages.forEach(p => p.deleted = false);
    this._normalizeCutPositions();
  }

  hasCutAt(position) {
    return this.splitCuts.has(Number(position));
  }

  toggleCutAt(position) {
    const pos = Number(position);
    if (!Number.isInteger(pos) || pos <= 0 || pos >= this.pages.length) return false;
    if (this.splitCuts.has(pos)) {
      this.splitCuts.delete(pos);
      this._cutIds.delete(pos);
      return false;
    }
    this.splitCuts.add(pos);
    this._cutIds.set(pos, this._newCutId());
    return true;
  }

  // Compatibilidad defensiva: si algún caller viejo pasa un pageId,
  // lo convertimos a posición después de esa página.
  toggleCut(positionOrPageId) {
    if (Number.isInteger(Number(positionOrPageId))) {
      return this.toggleCutAt(Number(positionOrPageId));
    }
    const idx = this.pages.findIndex(p => p.id === positionOrPageId);
    return idx >= 0 ? this.toggleCutAt(idx + 1) : false;
  }

  getOutputGroups() {
    const active = this.pages.filter(p => !p.deleted);
    this._normalizeCutPositions();

    if (!this.splitMode || this.splitCuts.size === 0) {
      return [{ id: 'group-0', name: 'Documento final', pages: active, compression: 'none' }];
    }

    // key: id del corte donde empieza la parte ('start' para la primera). Sirve
    // para asociar nombre/compresión a la parte de forma estable: no se corre al
    // agregar o quitar cortes antes, ni al mover o eliminar hojas (antes la clave
    // era la primera hoja del tramo y arrastrar esa hoja se llevaba el nombre).
    const groups = [];
    let current = [];
    let currentKey = 'start';

    for (let i = 0; i < this.pages.length; i++) {
      const page = this.pages[i];
      if (!page.deleted) current.push(page);

      // El corte pertenece al espacio visual después de la posición i,
      // no a la hoja. Si el tramo quedó sin hojas activas (todas eliminadas),
      // no genera parte, pero la siguiente parte igual empieza en este corte.
      if (this.splitCuts.has(i + 1)) {
        if (current.length > 0) groups.push({ key: currentKey, pages: [...current] });
        current = [];
        currentKey = this._cutIdAt(i + 1);
      }
    }

    if (current.length > 0) groups.push({ key: currentKey, pages: current });

    if (groups.length === 0) {
      return [{ id: 'group-0', name: 'Documento final', pages: active, compression: 'none' }];
    }

    return groups.map((g, i) => ({
      id: `group-${i}`,
      key: g.key,
      name: `Parte ${i + 1}`,
      pages: g.pages,
      compression: 'none',
    }));
  }

  // Etiquetas de la grilla para cada corte: { left, right } con los números de
  // parte que separa, iguales a los del panel (getOutputGroups). null si el corte
  // no separa nada (no hay hojas activas de alguno de los dos lados).
  getCutPartLabels() {
    const labels = new Map();
    if (!this.splitMode) return labels;
    this._normalizeCutPositions();
    const remainingActive = [];
    let count = 0;
    for (let i = this.pages.length - 1; i >= 0; i--) {
      if (!this.pages[i].deleted) count++;
      remainingActive[i] = count;
    }
    let groupsDone = 0;
    let currentActive = 0;
    for (let i = 0; i < this.pages.length; i++) {
      if (!this.pages[i].deleted) currentActive++;
      const pos = i + 1;
      if (!this.splitCuts.has(pos)) continue;
      const after = i + 1 < this.pages.length ? remainingActive[i + 1] : 0;
      if (currentActive > 0) {
        groupsDone++;
        labels.set(pos, after > 0 ? { left: groupsDone, right: groupsDone + 1 } : null);
      } else {
        labels.set(pos, null);
      }
      currentActive = 0;
    }
    return labels;
  }

  // Returns consecutive blocks of same-source pages in current order
  getDocumentBlocks() {
    const active = this.pages.filter(p => !p.deleted);
    if (!active.length) return [];
    const blocks = [];
    let cur = null;
    for (const page of active) {
      if (cur && cur.sourceDocumentId === page.sourceDocumentId) {
        cur.pages.push(page);
      } else {
        if (cur) blocks.push(cur);
        cur = { sourceDocumentId: page.sourceDocumentId, filename: page.sourceFilename, pages: [page] };
      }
    }
    if (cur) blocks.push(cur);
    return blocks.map((b, i) => {
      const doc = this.documents.get(b.sourceDocumentId);
      const sizeBytes = doc ? Math.round((doc.sizeBytes / (doc.pages || 1)) * b.pages.length) : 0;
      return { ...b, idx: i, id: `block-${i}-${b.sourceDocumentId}`, sizeBytes, originalType: doc?.originalType || 'pdf' };
    });
  }

  // Move block at fromBlockIdx to insert before insertBeforeBlockIdx
  moveBlockTo(fromBlockIdx, insertBeforeBlockIdx) {
    const blocks = this.getDocumentBlocks();
    if (fromBlockIdx === insertBeforeBlockIdx || fromBlockIdx + 1 === insertBeforeBlockIdx) return;
    if (fromBlockIdx < 0 || fromBlockIdx >= blocks.length) return;
    const src = blocks[fromBlockIdx];
    const srcIds = new Set(src.pages.map(p => p.id));
    const srcIndices = this.pages.map((p, i) => srcIds.has(p.id) ? i : -1).filter(i => i >= 0);
    let insertBeforePageIdx;
    if (insertBeforeBlockIdx >= blocks.length) {
      insertBeforePageIdx = this.pages.length;
    } else {
      insertBeforePageIdx = this.pages.findIndex(p => p.id === blocks[insertBeforeBlockIdx].pages[0].id);
    }
    this.movePagesTo(srcIndices, insertBeforePageIdx);
  }

  // Integra: aplica el orden propuesto por el análisis y crea las partes.
  // pageIds: hojas activas en el orden nuevo (las que no figuran, p. ej. eliminadas,
  // quedan al final). cutPositions: cantidad de hojas antes de cada corte.
  // Registra historial: Ctrl+Z vuelve al orden anterior. Devuelve las claves de las
  // partes en orden ('start' y los ids de corte) para asignarles nombre.
  applyProposedOrder(pageIds, cutPositions = []) {
    const byId = new Map(this.pages.map(p => [p.id, p]));
    const ordered = pageIds.map(id => byId.get(id)).filter(Boolean);
    if (!ordered.length) return [];
    const used = new Set(ordered.map(p => p.id));
    this.pushHistory();
    this.pages = [...ordered, ...this.pages.filter(p => !used.has(p.id))];
    this.splitCuts = new Set();
    this._cutIds = new Map();
    const keys = ['start'];
    [...new Set(cutPositions.map(Number))]
      .filter(n => Number.isInteger(n) && n > 0 && n < this.pages.length)
      .sort((a, b) => a - b)
      .forEach(pos => {
        const id = this._newCutId();
        this.splitCuts.add(pos);
        this._cutIds.set(pos, id);
        keys.push(id);
      });
    return keys;
  }

  getExportRequest(outputGroups) {
    const pages = this.pages.map((p, i) => ({
      page_id: p.id,
      source_document_id: p.sourceDocumentId,
      page_num: p.pageNum,
      rotation: p.rotation,
      deleted: p.deleted,
      order: i,
    }));

    const outputs = outputGroups.map((g, i) => ({
      name: (g.name || g.defaultName || `Parte ${i + 1}`).trim() || `Parte ${i + 1}`,
      page_ids: g.pages.map(p => p.id),
      compression: g.compression || 'none',
    }));

    return { pages, outputs };
  }
}
