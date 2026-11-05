import { svgIcon } from '../js/utils.js';

const DEBUG = false;
function debugLog(...args) { if (DEBUG) console.log(...args); }
import { API } from '../js/api.js';
import { toast } from '../components/toast.js';
import { WorkspaceState } from '../components/workspace-state.js';
import { PageGrid } from '../components/page-grid.js';
import { openPageViewer } from '../components/page-viewer.js';
import { showConfirmDialog } from '../components/confirm-dialog.js';
import {
  runExclusiveExport, syncExportButtons, exportSingle, exportUnifiedZip,
} from './organize-export.js';
import { renderResultPanel } from './organize-parts-panel.js';
import { installWorkspaceShortcuts, removeWorkspaceShortcuts } from './organize-shortcuts.js';
import {
  setupIntegra, toggleIntegraPanel, handleLabelClick, resetIntegra,
} from './integra-panel.js';

const ACCEPTED = '.pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx,.ppt,.pptx';

// Persist state across hash-navigation (e.g., old tool → back to workspace)
let _ws = null;
let _grid = null;
let _currentMode = 'organize';
let _compressLevel = 'none';
let _gridSize = (() => {
  const s = localStorage.getItem('artemis-grid-size') || 'auto';
  return ['auto', '2', '4', '6'].includes(s) ? s : 'auto';
})();
// Nombre y compresión por parte, indexados por la CLAVE de la parte (id de la
// primera página del tramo, ver WorkspaceState.getOutputGroups) y no por posición:
// así agregar o quitar un corte antes no "corre" el nombre a otra parte.
let _groupNames = {}; // { groupKey: customName } — survives _renderResultPanel re-calls
let _groupCompression = {}; // { groupKey: 'none'|'high' } — persists per-group compression
let _singleOutputName = ''; // name for single-PDF export (no splits)

// Contexto vivo para los módulos extraídos (organize-export.js,
// organize-parts-panel.js, organize-shortcuts.js): los getters leen las variables
// de este módulo en el momento de usarse, igual que cuando el código vivía acá.
const _ctx = {
  get ws() { return _ws; },
  get grid() { return _grid; },
  get currentMode() { return _currentMode; },
  get compressLevel() { return _compressLevel; },
  get singleOutputName() { return _singleOutputName; },
  get groupNames() { return _groupNames; },
  get groupCompression() { return _groupCompression; },
  applyHistory: (app, direction) => _applyHistory(app, direction),
};

// ─── Deshacer / rehacer ──────────────────────────────────────────────────────

function _updateHistoryButtons(app) {
  const undoBtn = app.querySelector('#undo-btn');
  const redoBtn = app.querySelector('#redo-btn');
  if (undoBtn) undoBtn.disabled = !_ws?.canUndo;
  if (redoBtn) redoBtn.disabled = !_ws?.canRedo;
}

function _applyHistory(app, direction) {
  if (!_ws || !_grid) return;
  const changed = direction === 'redo' ? _ws.redo() : _ws.undo();
  if (!changed) return;
  _grid.render();
  _updateCounter(app);
  if (_ws.splitMode) _renderResultPanel(app);
}

function _removeWorkspaceShortcuts() {
  removeWorkspaceShortcuts();
}

function _installWorkspaceShortcuts(app) {
  installWorkspaceShortcuts(app, _ctx);
}

async function _rerenderPagesLimited(pageIds, limit = 6) {
  const ids = [...new Set(pageIds || [])];
  if (!ids.length || !_grid) return;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, ids.length) }, async () => {
    while (cursor < ids.length) {
      const index = cursor++;
      await _grid.rerenderCard(ids[index]);
    }
  });
  await Promise.allSettled(workers);
}

function _applyThemeBtn(btn) {
  const isDark = document.documentElement.classList.contains('theme-dark');
  btn.innerHTML = isDark ? svgIcon('sun', 15) : svgIcon('moon', 15);
  btn.title = isDark ? 'Activar modo claro' : 'Activar modo oscuro';
  btn.setAttribute('aria-label', btn.title);
}

export async function renderWorkspace(app) {
  _removeWorkspaceShortcuts();
  if (!_ws) {
    _ws = new WorkspaceState();
    _ws.viewMode = 'grid'; // files view is always grid; list mode removed
    _ws.viewType = localStorage.getItem('artemis-view-type') || 'pages';
  }
  if (_grid) { _grid.destroy(); _grid = null; }

  _renderShell(app);
  _updateHeaderState(app);

  // Expuesto para que Python invoque vía bridge.filesDropped signal.
  // _last_selected_paths ya está seteado; importFiles([]) los toma sin base64.
  window._bigpdfHandleNativeDrop = () => {
    if (_ws && _ws.pages.length > 0) {
      _addMoreFiles(app, [], null);
    } else {
      _importFiles(app, []);
    }
  };

  if (_ws.pages.length > 0) {
    _renderWorkspaceActive(app);
  } else {
    _renderEmptyState(app);
  }
}

// ─── SHELL (sidebar + header + ws-root placeholder) ───────────────────────────

function _renderShell(app) {
  app.innerHTML = `
    <div class="ws-app-shell">
      <div class="ws-main-col">
        <header class="ws-app-header" id="ws-app-header">
          <div class="ws-app-title"><img src="./assets/brand/bigpdf-icon-v3-192x192.png" alt="Big Integra" style="width:38px;height:38px;object-fit:contain;border-radius:8px;flex-shrink:0"> <span style="font-size:22px;font-weight:800;letter-spacing:-0.03em">Big Integra</span></div>
          <nav class="ws-topbar-nav">
            <button class="ws-topbar-nav-item active" data-mode="organize">${svgIcon('layers', 15)} Organizar</button>
          </nav>
          <div id="ws-doc-state" class="ws-doc-state hidden"></div>
          <div style="flex:1"></div>
          <button class="theme-toggle-btn" id="theme-toggle-btn" title="Activar modo oscuro" style="background:transparent;border:1px solid rgba(255,255,255,0.22);color:rgba(255,255,255,0.75);border-radius:7px;width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;transition:background 0.15s,border-color 0.15s;flex-shrink:0" onmouseover="this.style.background='rgba(255,255,255,0.12)'" onmouseout="this.style.background='transparent'">${svgIcon('moon', 15)}</button>
          <button class="btn btn-ghost ws-btn hidden" id="clear-ws-btn">${svgIcon('x-circle', 14)} Limpiar</button>
        </header>
        <div id="ws-root" style="flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0"></div>
      </div>
    </div>
  `;

  // Theme toggle
  const _themeBtn = app.querySelector('#theme-toggle-btn');
  if (_themeBtn) {
    _applyThemeBtn(_themeBtn);
    _themeBtn.addEventListener('click', () => {
      const isDark = document.documentElement.classList.toggle('theme-dark');
      localStorage.setItem('bigpdf_theme', isDark ? 'dark' : 'light');
      debugLog('[Bigpdf] THEME TOGGLE', { theme: isDark ? 'dark' : 'light' });
      _applyThemeBtn(_themeBtn);
    });
  }

  app.querySelector('#clear-ws-btn').addEventListener('click', () => _clearWorkspace(app));
}

// ─── EMPTY STATE (no docs loaded) ─────────────────────────────────────────────

function _renderEmptyState(app) {
  const root = app.querySelector('#ws-root');
  root.innerHTML = `
    <div class="ws-empty-drop" id="ws-empty-area">
      <div class="ws-empty-dropzone" id="dropzone">
        <div class="ws-empty-icon">${svgIcon('layers', 40)}</div>
        <h2>Arrastrá tus documentos acá</h2>
        <button class="btn btn-primary" id="select-btn">Seleccionar archivos</button>
        <p class="upload-hint">Múltiples archivos · máx. 2 GB por archivo</p>
        <div class="progress-bar hidden" id="prog"><div class="progress-fill indeterminate"></div></div>
        <div id="import-status" style="font-size:12px;color:var(--text-secondary);margin-top:8px;min-height:18px"></div>
      </div>
      <input type="file" id="file-input" accept="${ACCEPTED}" multiple style="display:none">
    </div>
  `;

  const dz = root.querySelector('#dropzone');
  const input = root.querySelector('#file-input');

  root.querySelector('#select-btn').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const files = [...input.files];
    // Resetear para que volver a elegir el mismo archivo (p. ej. tras un error)
    // dispare 'change' de nuevo.
    input.value = '';
    if (files.length) _importFiles(app, files);
  });

  // Full ws-root accepts drops
  root.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
  root.addEventListener('dragleave', e => { if (!root.contains(e.relatedTarget)) dz.classList.remove('dragover'); });
  root.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('dragover');
    if (e.dataTransfer.files.length) _importFiles(app, [...e.dataTransfer.files]);
  });
}

// ─── IMPORT ────────────────────────────────────────────────────────────────────

async function _importFiles(app, files) {
  const prog = app.querySelector('#prog');
  const statusEl = app.querySelector('#import-status');
  if (prog) prog.classList.remove('hidden');
  if (statusEl) statusEl.textContent = files.length > 0
    ? `Procesando ${files.length} archivo(s)...`
    : 'Cargando archivos…';

  try {
    const result = await API.importFiles(files);
    let ok = 0;
    for (const doc of result.documents) {
      if (doc.status === 'error') {
        toast(`${doc.filename}: ${doc.error}`, 'error');
      } else {
        if (doc.warning) toast(`${doc.filename}: ${doc.warning}`, 'warning');
        const docObj = _ws.addDocument(doc);
        _ws.appendPages(docObj);
        ok++;
      }
    }
    if (result.notice) toast(result.notice, 'warning');
    if (ok === 0) {
      if (prog) prog.classList.add('hidden');
      toast('No se pudo procesar ningún archivo.', 'error');
      return;
    }
    toast(`${ok} archivo(s) cargados.`, 'success');
    // La primera carga no es deshacible (dejaría el workspace vacío).
    _ws.clearHistory();
    _renderWorkspaceActive(app);
    _updateHeaderState(app);
  } catch (e) {
    toast(e.message || 'Error al importar archivos.', 'error');
    if (prog) prog.classList.add('hidden');
  }
}

// ─── ACTIVE WORKSPACE (docs loaded) ───────────────────────────────────────────

function _renderWorkspaceActive(app) {
  const root = app.querySelector('#ws-root');
  root.innerHTML = `
    <div class="ws-toolbar" id="ws-toolbar">
      <button class="btn btn-ghost ws-btn" id="add-files-btn">${svgIcon('upload', 14)} Agregar</button>
      <button class="btn btn-ghost ws-btn" id="integra-btn" title="Identificar documentos, armar el checklist y ordenar el legajo">${svgIcon('file-text', 14)} Integra</button>
      <input type="file" id="add-input" accept="${ACCEPTED}" multiple style="display:none">
      <div class="toolbar-sep"></div>
      <button class="btn btn-ghost ws-btn" id="undo-btn" title="Deshacer (Ctrl+Z)" aria-label="Deshacer" disabled>${svgIcon('undo', 15)}</button>
      <button class="btn btn-ghost ws-btn" id="redo-btn" title="Rehacer (Ctrl+Y)" aria-label="Rehacer" disabled><span style="display:inline-flex;transform:scaleX(-1)">${svgIcon('undo', 15)}</span></button>
      <div class="toolbar-sep"></div>
      <button class="btn btn-ghost ws-btn" id="sel-all-btn" title="Seleccionar todas las páginas (Ctrl+A)">${svgIcon('check', 14)} Todo</button>
      <button class="btn btn-ghost ws-btn" id="sel-none-btn" title="Quitar selección (Esc)">Ninguno</button>
      <div class="toolbar-sep"></div>
      <button class="btn btn-ghost ws-btn" id="rot-l-btn" title="Rotar selección a la izquierda" aria-label="Rotar selección a la izquierda">${svgIcon('rotate-ccw', 15)}</button>
      <button class="btn btn-ghost ws-btn" id="rot-r-btn" title="Rotar selección a la derecha" aria-label="Rotar selección a la derecha">${svgIcon('rotate-cw', 15)}</button>
      <button class="btn btn-ghost ws-btn" id="del-btn" title="Eliminar selección (Supr)" aria-label="Eliminar selección">${svgIcon('trash', 14)}</button>
      <button class="btn btn-ghost ws-btn" id="restore-btn" title="Restaurar todas las páginas" aria-label="Restaurar todas las páginas">${svgIcon('refresh-ccw', 14)}</button>
      <button class="btn btn-ghost ws-btn" id="split-mode-btn" title="Modo separar" aria-label="Modo separar">${svgIcon('scissors', 14)}</button>
      <div class="toolbar-sep"></div>
      <button class="btn btn-ghost ws-btn ws-vtype-btn${_ws.viewType === 'pages' ? ' active' : ''}" data-vtype="pages" title="Ver páginas" style="padding:4px 8px;font-size:11px">${svgIcon('file-text', 13)} Hojas</button>
      <button class="btn btn-ghost ws-btn ws-vtype-btn${_ws.viewType === 'files' ? ' active' : ''}" data-vtype="files" title="Ver archivos" style="padding:4px 8px;font-size:11px">${svgIcon('folder', 13)} Archivos</button>
      <div class="toolbar-sep"></div>
      <button class="btn btn-ghost ws-btn ws-size-btn${_gridSize === 'auto' ? ' active' : ''}" data-size="auto" title="Vista automática" style="min-width:32px;padding:4px 7px;font-size:11px">A</button>
      <button class="btn btn-ghost ws-btn ws-size-btn${_gridSize === '2' ? ' active' : ''}" data-size="2" title="2 por fila" style="min-width:28px;padding:4px 7px;font-size:11px">2</button>
      <button class="btn btn-ghost ws-btn ws-size-btn${_gridSize === '4' ? ' active' : ''}" data-size="4" title="4 por fila" style="min-width:28px;padding:4px 7px;font-size:11px">4</button>
      <button class="btn btn-ghost ws-btn ws-size-btn${_gridSize === '6' ? ' active' : ''}" data-size="6" title="6 por fila" style="min-width:28px;padding:4px 7px;font-size:11px">6</button>
    </div>

    <div class="ws-work-row">
      <div id="grid-container" style="flex:1;overflow-y:auto;background:var(--bg-grid);min-height:0"></div>
      <div id="result-panel" class="result-panel-side hidden"></div>
      <div id="integra-panel" class="integra-panel hidden"></div>
    </div>

    <div class="ws-footer" id="ws-footer">
      <span id="footer-count" class="ws-counter"></span>
      <div style="flex:1"></div>
      <select id="compress-sel" class="compress-sel" style="font-size:12px;padding:4px 8px">
        <option value="none">Sin compresión</option>
        <option value="high">Comprimir PDF</option>
      </select>
      <input id="single-name-input" type="text" class="single-name-input" placeholder="Documento organizado" title="Nombre del PDF de salida" style="max-width:180px;font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-card);color:var(--text-primary);outline:none">
      <label for="choose-location-chk" style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-secondary);cursor:pointer;user-select:none;white-space:nowrap" title="Si está activado, se abre el explorador para elegir carpeta y nombre al guardar">
        <input type="checkbox" id="choose-location-chk" style="margin:0;cursor:pointer;accent-color:var(--accent,#2563eb)">
        Elegir ubicación al guardar
      </label>
      <button class="btn btn-primary ws-btn" id="export-single-btn">${svgIcon('download', 14)} Descargar PDF</button>
      <button class="btn btn-ghost ws-btn" id="export-zip-btn">${svgIcon('download', 14)} ZIP</button>
    </div>
  `;

  _updateCounter(app);
  _applyModeUI(app);

  // Integra: panel lateral, etiquetas en las miniaturas y orden sugerido.
  root.querySelector('#integra-btn').addEventListener('click', () => toggleIntegraPanel(app, _integraCtx(app)));
  const gridEl = root.querySelector('#grid-container');
  // Captura: la etiqueta no debe seleccionar la hoja ni iniciar un arrastre.
  ['pointerdown', 'mousedown'].forEach(evt => gridEl.addEventListener(evt, (e) => {
    if (e.target.closest?.('.pg-doc-label, .ig-type-picker')) e.stopPropagation();
  }, true));
  gridEl.addEventListener('click', (e) => {
    if (e.target.closest?.('.ig-type-picker')) { e.stopPropagation(); return; }
    handleLabelClick(app, _integraCtx(app), e);
  }, true);

  // Deshacer / rehacer
  _ws.onHistoryChange = () => _updateHistoryButtons(app);
  _updateHistoryButtons(app);
  root.querySelector('#undo-btn').addEventListener('click', () => _applyHistory(app, 'undo'));
  root.querySelector('#redo-btn').addEventListener('click', () => _applyHistory(app, 'redo'));

  // Add files
  const addInput = root.querySelector('#add-input');
  root.querySelector('#add-files-btn').addEventListener('click', () => addInput.click());
  addInput.addEventListener('change', async () => {
    const files = [...addInput.files];
    addInput.value = ''; // permite reintentar con el mismo archivo
    if (!files.length) return;
    await _addMoreFiles(app, files, null);
  });

  // Selection
  root.querySelector('#sel-all-btn').addEventListener('click', () => {
    _ws.selectAll(); _grid.updateAllStates(); _updateCounter(app);
  });
  root.querySelector('#sel-none-btn').addEventListener('click', () => {
    _ws.clearSelection(); _grid.updateAllStates(); _updateCounter(app);
  });

  // Rotate
  root.querySelector('#rot-l-btn').addEventListener('click', async () => {
    if (!_ws.selectedCount) { toast('Seleccioná páginas para rotar.', 'warning'); return; }
    const ids = [..._ws.selectedIds];
    _ws.pushHistory();
    _ws.rotateSelected(-90);
    await _rerenderPagesLimited(ids);
  });
  root.querySelector('#rot-r-btn').addEventListener('click', async () => {
    if (!_ws.selectedCount) { toast('Seleccioná páginas para rotar.', 'warning'); return; }
    const ids = [..._ws.selectedIds];
    _ws.pushHistory();
    _ws.rotateSelected(90);
    await _rerenderPagesLimited(ids);
  });

  // Delete / Restore
  root.querySelector('#del-btn').addEventListener('click', () => {
    if (!_ws.selectedCount) { toast('Seleccioná páginas para eliminar.', 'warning'); return; }
    const n = _ws.selectedCount;
    _ws.pushHistory();
    _ws.deleteSelected();
    _grid.updateAllStates();
    _updateCounter(app);
    toast(`${n} página(s) marcadas para eliminar.`, 'info');
    if (_ws.splitMode) _renderResultPanel(app);
  });
  root.querySelector('#restore-btn').addEventListener('click', () => {
    if (_ws.pages.some(p => p.deleted)) _ws.pushHistory();
    _ws.restoreAll();
    _grid.updateAllStates();
    _updateCounter(app);
    toast('Páginas restauradas.', 'success');
    if (_ws.splitMode) _renderResultPanel(app);
  });

  // Split mode toggle
  root.querySelector('#split-mode-btn').addEventListener('click', () => {
    _switchMode(app, _currentMode === 'split' ? 'organize' : 'split');
  });

  // View type toggle (pages / files)
  root.querySelectorAll('.ws-vtype-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const vtype = btn.dataset.vtype;
      if (vtype === 'files' && _currentMode === 'split') {
        toast('Cortes de páginas no disponible en vista Archivos.', 'warning');
        return;
      }
      _ws.viewType = vtype;
      localStorage.setItem('artemis-view-type', vtype);
      root.querySelectorAll('.ws-vtype-btn').forEach(b => b.classList.toggle('active', b === btn));
      if (_grid) _grid.render();
    });
  });

  // Grid size
  root.querySelectorAll('.ws-size-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _gridSize = btn.dataset.size;
      localStorage.setItem('artemis-grid-size', _gridSize);
      root.querySelectorAll('.ws-size-btn').forEach(b => b.classList.toggle('active', b === btn));
      if (_grid) _grid.setGridSize(_gridSize);
    });
  });

  // Compress level
  root.querySelector('#compress-sel').value = _compressLevel;
  root.querySelector('#compress-sel').addEventListener('change', (e) => {
    _compressLevel = e.target.value;
    const label = _compressLevel === 'none' ? 'Sin compresión' : 'Comprimir PDF';
    debugLog('[Bigpdf] COMPRESSION SELECTED', { compression: _compressLevel, label });
  });

  // Single output name input
  const singleNameInput = root.querySelector('#single-name-input');
  if (singleNameInput) {
    if (_singleOutputName) singleNameInput.value = _singleOutputName;
    singleNameInput.addEventListener('input', (e) => { _singleOutputName = e.target.value; });
  }

  // Export
  root.querySelector('#export-single-btn').addEventListener('click', () => runExclusiveExport(app, () => exportSingle(app, _ctx)));
  // ZIP del pie: siempre descarga el documento completo dentro de un ZIP,
  // independientemente de que el Modo Separar esté activo o no.
  root.querySelector('#export-zip-btn').addEventListener('click', () => runExclusiveExport(app, () => exportUnifiedZip(app, _ctx)));
  syncExportButtons(app);

  // Preferencia "Elegir ubicación al guardar": vive en Python (prefs.json en
  // %LOCALAPPDATA%\BigPDF), que la consulta antes de cada descarga para decidir
  // auto-guardado en Descargas vs. diálogo manual. No se usa localStorage: el perfil
  // web es off-the-record y se vacía en cada arranque (pisaba la preferencia).
  const chooseLocationChk = root.querySelector('#choose-location-chk');
  if (chooseLocationChk) {
    API.getDownloadLocationPref()
      .then((pref) => { chooseLocationChk.checked = !!pref?.choose_location; })
      .catch(() => {});
    chooseLocationChk.addEventListener('change', () => {
      API.setDownloadLocationPref(chooseLocationChk.checked).catch(() => {});
    });
  }

  // Init grid
  const gridContainer = root.querySelector('#grid-container');
  _grid = new PageGrid(gridContainer, _ws, {
    onSelectionChange: () => _updateCounter(app),
    onReorder: () => { _updateCounter(app); if (_ws.splitMode) _renderResultPanel(app); },
    onInsert: (afterIdx) => _openInsertAt(app, afterIdx),
    onSplitChange: () => _renderResultPanel(app),
    onPageDelete: () => { _updateCounter(app); if (_ws.splitMode) _renderResultPanel(app); },
    onPageRestore: () => { _updateCounter(app); if (_ws.splitMode) _renderResultPanel(app); },
    onPageRotate: () => {},
    onPageView: (pageId) => {
      openPageViewer(_ws.pages, pageId, (docId) => _grid.getOrLoad(docId), async (rotatedPageId) => {
        // Rotation from viewer modal → invalidate cache + re-render thumbnail in grid
        await _grid.rerenderCard(rotatedPageId);
      }, () => _ws.pushHistory());
    },
    onDeleteBlock: (pageIds) => {
      _ws.pushHistory();
      for (const id of pageIds) _ws.deletePage(id);
      _grid.render();
      _updateCounter(app);
      if (_ws.splitMode) _renderResultPanel(app);
      toast(`${pageIds.length} páginas eliminadas.`, 'info');
    },
    onExpandBlock: () => {
      _ws.viewType = 'pages';
      root.querySelectorAll('.ws-vtype-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.vtype === 'pages')
      );
      if (_grid) _grid.render();
    },
  });
  _grid.init();
  _installWorkspaceShortcuts(app);
  setupIntegra(app, _integraCtx(app));
}

// ─── MODE SYSTEM ───────────────────────────────────────────────────────────────

function _switchMode(app, mode) {
  _currentMode = mode;

  // Handle split mode
  const splitActive = mode === 'split';
  if (splitActive && _ws.viewType === 'files') {
    _ws.viewType = 'pages';
    app.querySelectorAll('.ws-vtype-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.vtype === 'pages')
    );
    toast('Vista cambiada a Hojas para marcar cortes.', 'info');
  }
  if (_ws.splitMode !== splitActive) {
    // Al salir del modo Separar se conservan los cortes y también los nombres y la
    // compresión por parte (indexados por clave de parte): al volver, todo sigue igual.
    // updateSplitMode only rebuilds inter-slots — cards and thumbnails stay intact
    if (_grid) _grid.updateSplitMode(splitActive);
    else _ws.splitMode = splitActive;
  }

  // Update UI if workspace is active
  if (_ws.pages.length > 0) {
    _applyModeUI(app);
    if (splitActive) _renderResultPanel(app);
    else {
      const rp = app.querySelector('#result-panel');
      if (rp) rp.classList.add('hidden');
    }
  }
}

function _applyModeUI(app) {
  const exportBtn = app.querySelector('#export-single-btn');
  const zipBtn = app.querySelector('#export-zip-btn');

  // La descarga unificada siempre está disponible, incluso con cortes activos.
  // Los cortes solo afectan las descargas por partes (individuales, "Todas" o ZIP).
  if (exportBtn) {
    exportBtn.classList.remove('hidden');
    exportBtn.innerHTML = _currentMode === 'split'
      ? `${svgIcon('download', 14)} Descargar PDF`
      : `${svgIcon('download', 14)} Descargar PDF`;
  }

  // El ZIP del pie siempre está disponible y descarga el PDF completo
  // dentro de un ZIP. Los cortes no afectan esta salida.
  if (zipBtn) zipBtn.classList.remove('hidden');

  // El nombre del PDF unificado puede editarse también con cortes activos.
  const sni = app.querySelector('#single-name-input');
  if (sni) sni.classList.remove('hidden');

  // Split toolbar button highlight
  app.querySelector('#split-mode-btn')?.classList.toggle('active', _currentMode === 'split');
}

// ─── INSERT BETWEEN PAGES ──────────────────────────────────────────────────────

function _openInsertAt(app, afterIdx) {
  const accepted = ACCEPTED;
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = accepted;
  input.multiple = true;
  input.style.display = 'none';
  document.body.appendChild(input);
  input.addEventListener('change', async () => {
    const files = [...input.files];
    input.remove();
    if (files.length) await _addMoreFiles(app, files, afterIdx);
  });
  input.addEventListener('cancel', () => input.remove());
  input.click();
}

async function _addMoreFiles(app, files, afterIdx) {
  const btn = app.querySelector('#add-files-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="loading-spinner" style="width:16px;height:16px;border-width:2px"></span>'; }

  try {
    const result = await API.importFiles(files);
    // Snapshot para deshacer antes de tocar el estado; se descarta si no entró nada.
    _ws.pushHistory();
    const pagesBefore = _ws.pages.length;
    let ok = 0;
    let insertOffset = 0;
    for (const doc of result.documents) {
      if (doc.status === 'error') {
        toast(`${doc.filename}: ${doc.error}`, 'error');
      } else {
        if (doc.warning) toast(`${doc.filename}: ${doc.warning}`, 'warning');
        const docObj = _ws.addDocument(doc);
        if (afterIdx !== null && afterIdx >= -1) {
          _ws.insertPagesAfterIndex(afterIdx + insertOffset, docObj);
          insertOffset += docObj.pages;
        } else {
          _ws.appendPages(docObj);
        }
        ok++;
      }
    }
    if (result.notice) toast(result.notice, 'warning');
    if (ok === 0) _ws.dropLastHistory();
    if (ok > 0) {
      const pos = afterIdx === -1 ? 'al inicio' : afterIdx === null ? 'al final' : `en posición ${afterIdx + 2}`;
      toast(`${ok} archivo(s) insertado(s) ${pos}.`, 'success');
      // Al final: se agregan solo las tarjetas nuevas. Insertar en medio reconstruye.
      if (afterIdx === null) _grid.appendNewPages(_ws.pages.length - pagesBefore);
      else _grid.render();
      _updateCounter(app);
      _updateHeaderState(app);
      if (_ws.splitMode) _renderResultPanel(app);
    }
  } catch (e) {
    toast(e.message || 'Error al importar.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = `${svgIcon('upload', 14)} Agregar`; }
  }
}

// ─── RESULT PANEL (split mode) y EXPORT ────────────────────────────────────────
// El panel de partes está en organize-parts-panel.js y la exportación/descargas
// en organize-export.js.

function _renderResultPanel(app) {
  renderResultPanel(app, _ctx);
}

// ─── INTEGRA ───────────────────────────────────────────────────────────────────

function _integraCtx(app) {
  return {
    get ws() { return _ws; },
    get grid() { return _grid; },
    applyOrder: (pageIds, partes) => _applyIntegraOrder(app, pageIds, partes),
  };
}

// Aplica el orden sugerido por el análisis: reordena las hojas y, si hay más de una
// parte, activa "Separar" con los cortes y los nombres de cada documento.
function _applyIntegraOrder(app, pageIds, partes) {
  const cuts = partes.slice(1).map(p => p.inicio);
  const keys = _ws.applyProposedOrder(pageIds, cuts);
  if (!keys.length) return;
  keys.forEach((key, i) => {
    if (partes[i]?.nombre) _groupNames[key] = partes[i].nombre;
  });
  if (partes.length === 1 && partes[0]?.nombre) {
    _singleOutputName = partes[0].nombre;
    const sni = app.querySelector('#single-name-input');
    if (sni) sni.value = _singleOutputName;
  }
  if (partes.length > 1 && _currentMode !== 'split') _switchMode(app, 'split');
  if (_grid) _grid.render();
  _updateCounter(app);
  if (_ws.splitMode) _renderResultPanel(app);
}

// ─── HELPERS ───────────────────────────────────────────────────────────────────

async function _clearWorkspace(app) {
  const ok = await showConfirmDialog({
    title: 'Limpiar workspace',
    message: '¿Limpiar el workspace? Se perderán todos los documentos cargados.',
    confirmText: 'Limpiar',
    cancelText: 'Cancelar',
  });
  if (!ok) return;
  _removeWorkspaceShortcuts();
  _ws = new WorkspaceState(); // historial de deshacer incluido: arranca vacío
  if (_grid) { _grid.destroy(); _grid = null; }
  _currentMode = 'organize';
  _singleOutputName = '';
  _groupNames = {};
  _groupCompression = {};
  resetIntegra();
  _updateHeaderState(app);
  _renderEmptyState(app);
}

function _updateHeaderState(app) {
  const numDocs = _ws.documents.size;
  const numPages = _ws.activePages.length;
  const stateEl = app.querySelector('#ws-doc-state');
  const clearBtn = app.querySelector('#clear-ws-btn');

  const hasContent = numDocs > 0;
  stateEl?.classList.toggle('hidden', !hasContent);
  clearBtn?.classList.toggle('hidden', !hasContent);
  if (stateEl && hasContent) {
    stateEl.textContent = `${numDocs} doc${numDocs !== 1 ? 's' : ''} · ${numPages} pág${numPages !== 1 ? 's' : ''}`;
  }
}

function _updateCounter(app) {
  const total = _ws.pages.length;
  const active = _ws.activePages.length;
  const sel = _ws.selectedCount;
  const deleted = total - active;
  const selPart = sel ? ` · ${sel} sel.` : '';
  const delPart = deleted ? ` (${deleted} elim.)` : '';
  const txt = `${active} págs.${selPart}${delPart}`;
  const counter = app.querySelector('#ws-counter');
  const footer = app.querySelector('#footer-count');
  if (counter) counter.textContent = txt;
  if (footer) footer.textContent = txt;
  _updateHeaderState(app);
}

