// Panel Integra: el usuario elige prestaciones y condiciones, aprieta "Analizar" y ve
// qué documento es cada hoja (etiqueta en la miniatura), el checklist por prestación
// con ✔ / ⚠ / ❌, y puede aplicar el orden sugerido. Todo el análisis lo hace Python
// (app/engine/integra); acá solo se arma el pedido y se muestra el resultado.
//
// Las etiquetas se pintan sobre las tarjetas de la grilla sin tocar page-grid.js: un
// MutationObserver las vuelve a poner cuando la grilla se re-renderiza.

import { API } from '../js/api.js';
import { toast } from '../components/toast.js';
import { escapeHtml, svgIcon } from '../js/utils.js';

const _state = {
  catalog: null,
  result: null,
  overrides: {}, // page_id → type_id elegido a mano
  prestaciones: new Set(),
  condiciones: {},
  modo: 'integracion',
  open: false,
  busy: false,
};
let _observer = null;
let _paintQueued = false;

const ICON = { ok: '✔', revisar: '⚠', falta: '❌' };
const CONF_LABEL = {
  alta: '', media: '', baja: 'Revisar', desconocido: 'Revisar', continuacion: 'Continuación', manual: 'Manual',
};

export function resetIntegra() {
  _state.result = null;
  _state.overrides = {};
}

export function isIntegraOpen() { return _state.open; }

export async function toggleIntegraPanel(app, ctx, force = null) {
  _state.open = force === null ? !_state.open : !!force;
  const panel = app.querySelector('#integra-panel');
  app.querySelector('#integra-btn')?.classList.toggle('active', _state.open);
  if (!panel) return;
  panel.classList.toggle('hidden', !_state.open);
  if (_state.open) {
    if (!_state.catalog) {
      try {
        _state.catalog = await API.integraCatalog();
        for (const c of _state.catalog.condiciones) _state.condiciones[c.id] ??= c.defecto;
      } catch (err) {
        toast(err?.message || 'No se pudo cargar el catálogo de Integra.', 'error');
        return;
      }
    }
    renderIntegraPanel(app, ctx);
  }
}

// Llamar cada vez que se arma el workspace activo (la grilla es nueva).
export function setupIntegra(app, ctx) {
  const container = app.querySelector('#grid-container');
  if (_observer) _observer.disconnect();
  if (container) {
    _observer = new MutationObserver(() => _queuePaint(app));
    _observer.observe(container, { childList: true, subtree: true });
  }
  if (_state.open) toggleIntegraPanel(app, ctx, true);
  _queuePaint(app);
}

function _queuePaint(app) {
  if (_paintQueued) return;
  _paintQueued = true;
  requestAnimationFrame(() => {
    _paintQueued = false;
    paintIntegraLabels(app);
  });
}

function _hojaInfo(pageId) {
  const hojas = _state.result?.hojas || [];
  return hojas.find(h => h.page_id === pageId) || null;
}

function _typeName(typeId) {
  return _state.catalog?.tipos.find(t => t.id === typeId)?.nombre || 'Sin identificar';
}

export function paintIntegraLabels(app) {
  const container = app.querySelector('#grid-container');
  if (!container) return;
  container.querySelectorAll('.pg-card[data-page-id]').forEach(card => {
    const info = _state.result ? _hojaInfo(card.dataset.pageId) : null;
    let label = card.querySelector('.pg-doc-label');
    if (!info) { label?.remove(); return; }
    if (!label) {
      label = document.createElement('button');
      label.type = 'button';
      label.className = 'pg-doc-label';
      (card.querySelector('.pg-card-top') || card).appendChild(label);
    }
    const conf = info.confianza;
    const text = info.nombre + (CONF_LABEL[conf] ? ` · ${CONF_LABEL[conf]}` : '');
    if (label.dataset.text !== text) {
      label.dataset.text = text;
      label.textContent = text;
      label.title = `${text}\nClic para corregir el tipo de documento.`;
    }
    label.className = `pg-doc-label conf-${conf}${info.type_id === 'instructivo' ? ' is-discard' : ''}`;
  });
}

// ─── Panel ───────────────────────────────────────────────────────────────────

export function renderIntegraPanel(app, ctx) {
  const panel = app.querySelector('#integra-panel');
  if (!panel || !_state.catalog) return;
  const cat = _state.catalog;
  const prestHtml = cat.prestaciones.map(p => `
    <label class="ig-check">
      <input type="checkbox" data-prest="${escapeHtml(p.id)}" ${_state.prestaciones.has(p.id) ? 'checked' : ''}>
      <span>${escapeHtml(p.nombre)}${p.provisoria ? ' <em title="Falta el instructivo: requisitos tomados del flujo anterior">(provisorio)</em>' : ''}</span>
    </label>`).join('');
  const condHtml = cat.condiciones.map(c => `
    <label class="ig-cond"><span>${escapeHtml(c.nombre)}</span>
      <select data-cond="${escapeHtml(c.id)}">
        ${Object.entries(c.opciones).map(([v, l]) => `<option value="${escapeHtml(v)}" ${_state.condiciones[c.id] === v ? 'selected' : ''}>${escapeHtml(l)}</option>`).join('')}
      </select>
    </label>`).join('');

  panel.innerHTML = `
    <div class="ig-header">
      <span class="ig-title">${svgIcon('file-text', 14)} Integra</span>
      <button class="btn btn-ghost ws-btn ig-close" title="Cerrar panel" aria-label="Cerrar panel">✕</button>
    </div>
    <div class="ig-body">
      <div class="ig-section">
        <div class="ig-section-title">¿Qué prestaciones se cargan?</div>
        ${prestHtml}
      </div>
      <div class="ig-section">
        <div class="ig-section-title">Condiciones</div>
        ${condHtml}
      </div>
      <div class="ig-section">
        <div class="ig-section-title">Uso</div>
        <label class="ig-check"><input type="radio" name="ig-modo" value="entrada" ${_state.modo === 'entrada' ? 'checked' : ''}><span>Entrada: un PDF por documento</span></label>
        <label class="ig-check"><input type="radio" name="ig-modo" value="integracion" ${_state.modo === 'integracion' ? 'checked' : ''}><span>Integración: legajo completo ordenado</span></label>
      </div>
      <button class="btn btn-primary ws-btn ig-analyze" ${_state.busy ? 'disabled' : ''}>${svgIcon('check', 14)} Analizar documentación</button>
      <div class="ig-results">${_renderResults()}</div>
    </div>`;

  panel.querySelector('.ig-close').addEventListener('click', () => toggleIntegraPanel(app, ctx, false));
  panel.querySelectorAll('[data-prest]').forEach(chk => chk.addEventListener('change', () => {
    if (chk.checked) _state.prestaciones.add(chk.dataset.prest);
    else _state.prestaciones.delete(chk.dataset.prest);
  }));
  panel.querySelectorAll('[data-cond]').forEach(sel => sel.addEventListener('change', () => {
    _state.condiciones[sel.dataset.cond] = sel.value;
  }));
  panel.querySelectorAll('input[name="ig-modo"]').forEach(r => r.addEventListener('change', () => {
    if (r.checked) _state.modo = r.value;
  }));
  panel.querySelector('.ig-analyze').addEventListener('click', () => runAnalysis(app, ctx));
  panel.querySelectorAll('[data-goto]').forEach(el => el.addEventListener('click', () => {
    _gotoPage(app, el.dataset.goto);
  }));
  panel.querySelector('.ig-apply-order')?.addEventListener('click', () => _applyOrder(app, ctx));
  panel.querySelector('.ig-copy-socio')?.addEventListener('click', () => {
    _copyText(_state.result?.respuesta_socio || '');
  });
  panel.querySelector('.ig-copy-report')?.addEventListener('click', () => {
    _copyText(_reportAsText());
  });
}

function _renderItems(items) {
  return items.map(it => {
    const goto = it.paginas?.length ? `data-goto="${it.paginas[0]}"` : '';
    return `<li class="ig-item ig-${it.estado}" ${goto} title="${escapeHtml(it.detalle)}">
      <span class="ig-icon">${ICON[it.estado] || ''}</span>
      <span class="ig-item-text"><b>${escapeHtml(it.nombre)}</b><br><small>${escapeHtml(it.detalle)}</small></span>
    </li>`;
  }).join('');
}

function _renderBlock(title, block, extra = '') {
  const obs = block.observaciones || [];
  return `
    <div class="ig-block">
      <div class="ig-block-title">${escapeHtml(title)}${extra}
        <span class="ig-estado ig-estado-${(block.estadoGeneral || '').toLowerCase()}">${escapeHtml(block.estadoGeneral || '')}</span>
      </div>
      <ul class="ig-items">${_renderItems(block.items || [])}</ul>
      ${obs.length ? `<div class="ig-obs-title">⚠ Observaciones</div><ul class="ig-obs">${obs.map(o => `<li>${escapeHtml(o)}</li>`).join('')}</ul>` : ''}
    </div>`;
}

function _renderResults() {
  const r = _state.result;
  if (!r) {
    return '<p class="ig-hint">Marcá las prestaciones y apretá <b>Analizar</b>. La primera vez puede tardar unos segundos por hoja escaneada.</p>';
  }
  const benef = r.beneficiario?.nombre
    ? `<p class="ig-benef"><b>Beneficiario/a:</b> ${escapeHtml(r.beneficiario.nombre)} — DNI ${escapeHtml(r.beneficiario.dni || 'no identificado')}</p>` : '';
  const ocr = (r.hojas || []).filter(h => h.fuente_texto === 'ocr').length;
  const sinId = (r.hojas || []).filter(h => ['desconocido', 'baja'].includes(h.confianza)).length;
  const resumen = `<p class="ig-hint">${(r.hojas || []).length} hojas analizadas${ocr ? ` (${ocr} por OCR)` : ''}.
    ${sinId ? `<b>${sinId} para revisar</b> (etiqueta amarilla): clic en la etiqueta para corregir.` : ''}</p>`;
  const blocks = [_renderBlock(r.generales.nombre, r.generales)]
    .concat(r.prestaciones.map(p => _renderBlock(p.prestacionAnalizada, p, p.provisoria ? ' <em>(provisorio)</em>' : '')))
    .join('');
  const noReq = r.no_requeridos?.length ? `
    <div class="ig-block"><div class="ig-block-title">Hojas no requeridas / sin identificar</div>
      <ul class="ig-obs">${r.no_requeridos.map(d => `<li data-goto="${d.paginas[0]}" class="ig-link">${escapeHtml(d.nombre)} (${d.paginas.length === 1 ? 'hoja' : 'hojas'} ${d.paginas.join(', ')})</li>`).join('')}</ul>
    </div>` : '';
  const partes = r.orden_propuesto?.partes || [];
  return `
    ${benef}${resumen}
    <div class="ig-actions">
      <button class="btn btn-ghost ws-btn ig-apply-order" title="Reordenar las hojas según el instructivo y crear las partes (se puede deshacer con Ctrl+Z)">${svgIcon('scissors', 13)} Aplicar orden sugerido (${partes.length} ${partes.length === 1 ? 'parte' : 'partes'})</button>
      <button class="btn btn-ghost ws-btn ig-copy-socio" title="Copiar el texto sugerido para responder al socio">Copiar respuesta</button>
      <button class="btn btn-ghost ws-btn ig-copy-report" title="Copiar el informe completo como texto">Copiar informe</button>
    </div>
    ${blocks}${noReq}
    <div class="ig-block"><div class="ig-block-title">Respuesta sugerida al socio</div>
      <pre class="ig-socio">${escapeHtml(r.respuesta_socio || '')}</pre></div>
    <p class="ig-hint">Firmas, sellos, enmiendas y datos manuscritos deben verificarse en el documento original.</p>`;
}

// ─── Acciones ────────────────────────────────────────────────────────────────

function _payload(ctx) {
  const pages = ctx.ws.pages.filter(p => !p.deleted).map(p => ({
    page_id: p.id,
    source_document_id: p.sourceDocumentId,
    page_num: p.pageNum,
    filename: p.sourceFilename,
    override_type: _state.overrides[p.id] || null,
  }));
  return {
    pages,
    prestaciones: [..._state.prestaciones],
    condiciones: { ..._state.condiciones },
    modo: _state.modo,
  };
}

export async function runAnalysis(app, ctx) {
  if (_state.busy) return;
  if (!ctx.ws?.pages?.some(p => !p.deleted)) {
    toast('No hay hojas para analizar.', 'warning');
    return;
  }
  if (!_state.prestaciones.size) {
    toast('Marcá al menos una prestación para armar el checklist. Igual se identifican las hojas.', 'info');
  }
  _state.busy = true;
  renderIntegraPanel(app, ctx);
  try {
    _state.result = await API.integraAnalyze(_payload(ctx));
    paintIntegraLabels(app);
  } catch (err) {
    if (!API.isCancellation(err)) toast(err?.message || 'No se pudo analizar la documentación.', 'error');
  } finally {
    _state.busy = false;
    renderIntegraPanel(app, ctx);
  }
}

function _pageIdAt(number) {
  return _state.result?.hojas?.[Number(number) - 1]?.page_id || null;
}

function _gotoPage(app, number) {
  const pageId = _pageIdAt(number);
  const card = pageId && app.querySelector(`#grid-container .pg-card[data-page-id="${CSS.escape(pageId)}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.add('ig-flash');
  setTimeout(() => card.classList.remove('ig-flash'), 1600);
}

function _applyOrder(app, ctx) {
  const orden = _state.result?.orden_propuesto;
  if (!orden?.page_ids?.length) return;
  ctx.applyOrder(orden.page_ids, orden.partes || []);
  // Las referencias "hoja N" del informe quedan viejas: se vuelve a analizar (rápido,
  // el texto ya está en memoria) para que coincidan con el orden nuevo.
  runAnalysis(app, ctx);
  toast('Orden aplicado. Revisá la grilla; Ctrl+Z vuelve al orden anterior.', 'success');
}

// Corrección manual de etiqueta: clic en la etiqueta → lista de tipos.
export function handleLabelClick(app, ctx, event) {
  const label = event.target.closest?.('.pg-doc-label');
  if (!label) return false;
  event.stopPropagation();
  event.preventDefault();
  const card = label.closest('.pg-card');
  const pageId = card?.dataset.pageId;
  if (!pageId || !_state.catalog) return true;
  document.querySelector('.ig-type-picker')?.remove();
  const current = _state.overrides[pageId] || _hojaInfo(pageId)?.type_id || '';
  const picker = document.createElement('select');
  picker.className = 'ig-type-picker';
  picker.innerHTML = `<option value="">— Automático —</option>` + _state.catalog.tipos
    .map(t => `<option value="${escapeHtml(t.id)}" ${t.id === current ? 'selected' : ''}>${escapeHtml(t.nombre)}</option>`)
    .join('');
  label.after(picker);
  picker.focus();
  const close = () => picker.remove();
  picker.addEventListener('blur', close);
  picker.addEventListener('click', e => e.stopPropagation());
  picker.addEventListener('change', () => {
    if (picker.value) _state.overrides[pageId] = picker.value;
    else delete _state.overrides[pageId];
    close();
    toast(`Hoja marcada como: ${_typeName(picker.value) || 'automático'}`, 'info', 2000);
    runAnalysis(app, ctx);
  });
  return true;
}

function _reportAsText() {
  const r = _state.result;
  if (!r) return '';
  const lines = [];
  if (r.beneficiario?.nombre) lines.push(`Beneficiario/a: ${r.beneficiario.nombre} — DNI ${r.beneficiario.dni || 'no identificado'}`);
  lines.push(`Prestaciones: ${r.prestaciones.map(p => p.prestacionAnalizada).join(', ') || '—'}`, '');
  for (const b of [r.generales, ...r.prestaciones]) {
    lines.push(`${b.nombre || b.prestacionAnalizada} — ${b.estadoGeneral}`);
    for (const [title, key] of [['✔ Correcto', 'correcto'], ['⚠ A revisar', 'aModificar'], ['⚠ Observaciones', 'observaciones'], ['❌ Faltante', 'faltante']]) {
      if (b[key]?.length) lines.push(title, ...b[key].map(x => `  - ${x}`));
    }
    lines.push('');
  }
  lines.push('Respuesta sugerida:', r.respuesta_socio || '');
  return lines.join('\n');
}

function _copyText(text) {
  if (!text) return;
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  if (!ok && navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => toast('Copiado al portapapeles.', 'success', 1800))
      .catch(() => toast('No se pudo copiar.', 'error'));
    return;
  }
  toast(ok ? 'Copiado al portapapeles.' : 'No se pudo copiar.', ok ? 'success' : 'error', 1800);
}
