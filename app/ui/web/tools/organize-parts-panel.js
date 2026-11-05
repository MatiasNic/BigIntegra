import { svgIcon } from '../js/utils.js';
import {
  runExclusiveExport, syncExportButtons, exportGroup, exportAll, downloadAllSeparately,
} from './organize-export.js';

// Panel lateral de partes del modo Separar: nombre y compresión por parte, y
// botones de descarga (parte, "Todas", ZIP). Extraído de organize.js.
// `ctx` es el contexto vivo de organize.js (getters sobre sus variables de módulo:
// ws, grid, currentMode, compressLevel, singleOutputName, groupNames,
// groupCompression): cada acceso lee el valor actual, igual que antes.

const DEBUG = false;
function debugLog(...args) { if (DEBUG) console.log(...args); }

function _groupKey(group, idx) {
  return group?.key || `idx-${idx}`;
}

// ─── RESULT PANEL (split mode) ─────────────────────────────────────────────────

export function renderResultPanel(app, ctx) {
  const panel = app.querySelector('#result-panel');
  if (!panel) return;
  const groups = ctx.ws.getOutputGroups();

  if (!ctx.ws.splitMode || (groups.length <= 1 && !ctx.ws.splitCuts.size)) {
    panel.classList.add('hidden');
    return;
  }

  panel.classList.remove('hidden');
  panel.innerHTML = `
    <div class="rp-header">
      <span class="rp-title">${svgIcon('scissors', 14)} ${groups.length} ${groups.length === 1 ? 'parte' : 'partes'}</span>
      <button class="btn btn-ghost ws-btn" id="dl-all-sep" style="padding:4px 8px;font-size:11px">${svgIcon('copy', 12)} Todas</button>
      <button class="btn btn-accent ws-btn" id="dl-zip-all" style="padding:4px 8px;font-size:11px">${svgIcon('archive', 12)} ZIP</button>
    </div>
    <div class="rp-groups">
      ${groups.map((g, i) => {
        const bytes = _estimateGroupBytes(ctx, g);
        const sizeStr = bytes > 0 ? ` · ~${_formatBytes(bytes)}` : '';
        // savedName: custom value or empty (default shown as placeholder)
        const key = _groupKey(g, i);
        const savedName = (ctx.groupNames[key] !== undefined && ctx.groupNames[key] !== '')
          ? ctx.groupNames[key].replace(/&/g, '&amp;').replace(/"/g, '&quot;')
          : '';
        const placeholderName = g.name.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        return `
        <div class="rp-group" data-group="${i}">
          <div class="rp-group-header">
            <span class="rp-part-badge">Parte ${i + 1}</span>
            <span class="rp-pages">${g.pages.length} pág.${sizeStr}</span>
          </div>
          <input class="group-name-input" type="text" value="${savedName}" placeholder="${placeholderName}" data-group="${i}" data-default="${placeholderName}">
          <div class="rp-actions">
            <select class="group-compress-sel" data-group="${i}">
              <option value="none">Sin compresión</option>
              <option value="high">Comprimir PDF</option>
            </select>
            <button class="btn btn-ghost ws-btn group-dl-btn" data-group="${i}" title="Descargar esta parte" aria-label="Descargar esta parte">${svgIcon('download', 13)}</button>
          </div>
        </div>
        `;
      }).join('')}
    </div>
  `;

  // groupState: each entry has defaultName + customName (persisted in ctx.groupNames)
  const groupState = groups.map((g, i) => {
    const key = _groupKey(g, i);
    const custom = (ctx.groupNames[key] !== undefined && ctx.groupNames[key] !== '') ? ctx.groupNames[key] : '';
    return {
      ...g,
      key,
      defaultName: g.name,
      customName: custom,
      name: custom || g.name,
    };
  });

  panel.querySelectorAll('.group-name-input').forEach(inp => {
    inp.addEventListener('input', () => {
      const idx = +inp.dataset.group;
      const raw = inp.value.trim();
      const name = raw || groupState[idx].defaultName;
      debugLog('[Bigpdf rename] input →', { idx, raw, resolved: name });
      ctx.groupNames[groupState[idx].key] = raw; // store raw (empty = use default at download time)
      groupState[idx].customName = raw;
      groupState[idx].name = name;
    });
  });
  // Initialize per-group compression selects: prefer persisted → footer ctx.compressLevel
  panel.querySelectorAll('.group-compress-sel').forEach(sel => {
    const g = +sel.dataset.group;
    const key = groupState[g].key;
    let rawLevel = (ctx.groupCompression[key] !== undefined) ? ctx.groupCompression[key] : ctx.compressLevel;
    // Normalize legacy values (low/medium → high) from stale persisted state
    const initLevel = (rawLevel && rawLevel !== 'none') ? 'high' : 'none';
    sel.value = initLevel;
    groupState[g].compression = initLevel;
  });
  panel.querySelectorAll('.group-compress-sel').forEach(sel => {
    sel.addEventListener('change', () => {
      const g = +sel.dataset.group;
      groupState[g].compression = sel.value;
      ctx.groupCompression[groupState[g].key] = sel.value;
    });
  });
  // Name resolution priority: ctx.groupNames (persisted) → DOM value → data-default → fallback
  const _resolveName = (idx) => {
    const persisted = ctx.groupNames[groupState[idx]?.key];
    if (persisted !== undefined && persisted !== '') return persisted;
    const inp = panel.querySelector(`.group-name-input[data-group="${idx}"]`);
    const dom = (inp?.value || '').trim();
    if (dom) return dom;
    const dflt = inp?.dataset?.default || groupState[idx]?.defaultName || `Parte ${idx + 1}`;
    return dflt;
  };
  panel.querySelectorAll('.group-dl-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = +btn.dataset.group;
      const inp = panel.querySelector(`.group-name-input[data-group="${idx}"]`);
      const name = _resolveName(idx);
      const finalFilename = `${name}.pdf`;
      debugLog('DESCARGAR PARTE DEBUG', {
        groupId: groupState[idx]?.id,
        defaultName: groupState[idx]?.defaultName,
        customName: groupState[idx]?.customName,
        inputValue: inp?.value,
        _groupNames_val: ctx.groupNames[groupState[idx]?.key],
        finalName: name,
        finalFilename,
        pagesCount: groupState[idx]?.pages?.length,
        pages: groupState[idx]?.pages?.map(p => p.id),
      });
      runExclusiveExport(app, () => exportGroup(ctx, { ...groupState[idx], name }, btn));
    });
  });
  panel.querySelector('#dl-zip-all').addEventListener('click', () => {
    const freshGroups = groupState.map((g, i) => {
      const name = _resolveName(i);
      return { ...g, name };
    });
    debugLog('[Bigpdf ZIP BUTTON CLICK] panel', freshGroups.map((g, i) => ({ idx: i, name: g.name, pagesCount: g.pages?.length })));
    runExclusiveExport(app, () => exportAll(app, ctx, true, freshGroups));
  });
  panel.querySelector('#dl-all-sep').addEventListener('click', () => {
    const freshGroups = groupState.map((g, i) => {
      const name = _resolveName(i);
      return { ...g, name };
    });
    debugLog('[Bigpdf] DOWNLOAD ALL PARTS START', freshGroups.map(g => g.name));
    runExclusiveExport(app, () => downloadAllSeparately(app, ctx, freshGroups));
  });

  // Si el panel se reconstruye durante una exportación, los botones nuevos
  // también nacen deshabilitados.
  syncExportButtons(app);
}

function _estimateGroupBytes(ctx, group) {
  let bytes = 0;
  for (const page of group.pages) {
    const doc = ctx.ws.documents.get(page.sourceDocumentId);
    if (doc && doc.sizeBytes && doc.pages) bytes += doc.sizeBytes / doc.pages;
  }
  return Math.round(bytes);
}

function _formatBytes(b) {
  if (!b) return '';
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  return `${(b / 1024 ** 2).toFixed(1)} MB`;
}
