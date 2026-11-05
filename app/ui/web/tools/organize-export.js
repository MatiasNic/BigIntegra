import { API } from '../js/api.js';
import { toast } from '../components/toast.js';
import { svgIcon } from '../js/utils.js';

// Exportación y descargas del workspace (PDF unificado, ZIP, partes del modo
// Separar) y el candado de "una exportación a la vez". Extraído de organize.js.
// `ctx` es el contexto vivo de organize.js (getters sobre sus variables de módulo:
// ws, grid, currentMode, compressLevel, singleOutputName, groupNames,
// groupCompression): cada acceso lee el valor actual, igual que antes.

const DEBUG = false;
function debugLog(...args) { if (DEBUG) console.log(...args); }

function _isCancelled(error) {
  return !!error?.cancelled || error?.name === 'AbortError' || API.isCancellation?.(error);
}

function _showExportError(error, fallback) {
  if (_isCancelled(error)) {
    toast('Procesamiento cancelado.', 'warning');
    return;
  }
  API.showStatusError?.(error?.message || fallback);
}

// Returns true if filename is an auto-generated internal name (should never be shown to user).
// Catches both pre-rename (artemis_) and post-rename (bigpdf_) prefixes so the
// defensive fallback works regardless of whether the server was restarted after a rename.
// 'artemis_' es el prefijo legacy (nombre anterior del producto); 'bigpdf_' es el actual.
// Ambos se detectan para cubrir archivos generados antes y después del rename.
function _isInternalFilename(fn) {
  const l = (fn || '').toLowerCase();
  return l.startsWith('artemis_') || l.startsWith('bigpdf_');
}

// ─── Nombres de archivo de salida ─────────────────────────────────────────────

// Agrega ".pdf" si el nombre no lo trae (sin distinguir mayúsculas).
function _withPdfExt(name) {
  return name.toLowerCase().endsWith('.pdf') ? name : `${name}.pdf`;
}

// Quita ".pdf" final si lo trae (para armar el nombre del .zip).
function _stripPdfExt(name) {
  return name.toLowerCase().endsWith('.pdf') ? name.slice(0, -4) : name;
}

// Nombre elegido por el usuario (recortado y con ".pdf"), o null si quedó vacío.
function _userPdfFilename(name) {
  const trimmed = name?.trim();
  return trimmed ? _withPdfExt(trimmed) : null;
}

// Nombre con que se guarda una parte: el del servidor salvo que sea un nombre
// interno autogenerado; si no hay ninguno, el del usuario o `fallback`.
function _pickDownloadFilename(serverFilename, userFilename, fallback) {
  return !_isInternalFilename(serverFilename)
    ? (serverFilename || userFilename || fallback)
    : (userFilename || fallback);
}

const SPINNER_14 = '<span class="loading-spinner" style="width:14px;height:14px;border-width:2px"></span>';

// Exportación en curso (cualquiera: PDF, ZIP, Todas, por parte). Mientras está
// activa se deshabilitan TODOS los botones de exportación: lanzar varias en
// paralelo dejaba las primeras sin forma de cancelarse (el chip sigue la última).
let _exportInFlight = false;
const EXPORT_BTN_SELECTOR = '#export-single-btn, #export-zip-btn, #dl-all-sep, #dl-zip-all, .group-dl-btn';

export function syncExportButtons(app) {
  app.querySelectorAll(EXPORT_BTN_SELECTOR).forEach(btn => { btn.disabled = _exportInFlight; });
}

export async function runExclusiveExport(app, fn) {
  if (_exportInFlight) {
    toast('Ya hay una exportación en curso.', 'warning');
    return;
  }
  _exportInFlight = true;
  syncExportButtons(app);
  try {
    return await fn();
  } finally {
    _exportInFlight = false;
    syncExportButtons(app);
  }
}

// ─── EXPORT ────────────────────────────────────────────────────────────────────

export async function exportGroup(ctx, group, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = SPINNER_14;

  debugLog('[Bigpdf] _exportGroup START', {
    name: group.name,
    defaultName: group.defaultName,
    customName: group.customName,
    pagesCount: group.pages?.length,
  });

  try {
    const req = ctx.ws.getExportRequest([group]);
    req.make_zip = false;

    const sentName = req.outputs?.[0]?.name;
    debugLog('[Bigpdf] _exportGroup REQUEST', {
      sentName,
      pageIdCount: req.outputs?.[0]?.page_ids?.length,
      compression: req.outputs?.[0]?.compression,
    });

    if (!sentName || sentName.trim() === '') {
      console.error('[Bigpdf] CRITICAL: sentName is empty! group.name was:', group.name);
    }

    const result = await API.exportWorkspace(
      req,
      group.compression && group.compression !== 'none'
        ? 'Comprimiendo y preparando la parte…'
        : 'Generando la parte…',
      true,
    );

    if (result.outputs?.[0]) {
      const out = result.outputs[0];
      const serverFilename = out.filename;
      const userFilename = _userPdfFilename(group.name);
      // Defensive fallback: if backend returns any auto-generated name, prefer user's input
      const downloadFilename = _pickDownloadFilename(serverFilename, userFilename, 'parte.pdf');
      debugLog('[Bigpdf] _exportGroup RESPONSE', {
        serverFilename,
        userFilename,
        downloadFilename,
        serverName: out.name,
        exportId: out.export_id,
        sizeBytes: out.size_bytes,
      });
      const savingsMsg = (out.saved_pct > 2 && out.size_before > 0)
        ? ` (−${Math.round(out.saved_pct)}%)` : '';
      await _triggerDownload(
        API.downloadUrl(out.export_id), downloadFilename,
        null, false,
        (realName) => `"${realName}" guardado${savingsMsg}`,
      );
    } else {
      console.error('[Bigpdf] _exportGroup: result.outputs is empty!', result);
      toast('Error: no se generó ningún archivo.', 'error');
    }
  } catch (e) {
    if (_isCancelled(e)) toast('Procesamiento cancelado.', 'warning');
    else toast(e.message, 'error');
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

export async function exportSingle(app, ctx) {
  const active = ctx.ws.activePages;
  if (!active.length) { toast('No hay páginas para descargar.', 'error'); return; }

  // Los cortes no afectan esta salida: siempre genera un único PDF con todas
  // las páginas activas, respetando el orden actual del workspace.
  const compression = ctx.compressLevel;
  const rawSingleName = ctx.singleOutputName.trim();
  const outputName = rawSingleName || 'Documento organizado';
  const outputFilename = _withPdfExt(outputName);
  debugLog('[Bigpdf] UNIFIED DOWNLOAD INPUT', {
    rawSingleName,
    outputName,
    outputFilename,
    splitMode: ctx.currentMode === 'split',
    pages: active.length,
  });
  const groups = [{ name: outputName, pages: active, compression }];

  const btn = app.querySelector('#export-single-btn');
  const originalLabel = _getExportBtnLabel();
  const setLoading = (v) => {
    if (!btn) return;
    btn.disabled = v;
    btn.innerHTML = v
      ? SPINNER_14
      : originalLabel;
  };

  setLoading(true);
  try {
    const req = ctx.ws.getExportRequest(groups);
    req.make_zip = false;
    const progressMsg = compression !== 'none'
      ? 'Comprimiendo y preparando el PDF…'
      : 'Generando el PDF unificado…';

    debugLog('[Bigpdf] UNIFIED DOWNLOAD REQUEST', {
      outputName: req.outputs?.[0]?.name,
      pages: req.outputs?.[0]?.page_ids?.length,
      compression: req.outputs?.[0]?.compression,
    });

    // Indicador inferior no bloqueante: mantiene la grilla usable y evita el
    // overlay cuya barra podía verse a saltos durante compresiones intensivas.
    const result = await API.exportWorkspace(req, progressMsg, true);
    if (!result.outputs?.[0]) {
      API.showStatusError('No se pudo generar el PDF.');
      return;
    }

    const out = result.outputs[0];
    const savingsMsg = (out.saved_pct > 2 && out.size_before > 0)
      ? ` (−${Math.round(out.saved_pct)}%)`
      : '';
    debugLog('[Bigpdf] UNIFIED DOWNLOAD RESPONSE', {
      serverFilename: out.filename,
      outputFilename,
      savedPct: out.saved_pct,
    });

    // Generar y guardar forman ahora una única acción para el usuario.
    await _triggerDownload(
      API.downloadUrl(out.export_id),
      outputFilename,
      'Guardando PDF…',
      false,
      (realName) => `"${realName}" guardado${savingsMsg}`,
    );
  } catch (e) {
    _showExportError(e, 'Error al generar el PDF.');
  } finally {
    setLoading(false);
  }
}


export async function exportUnifiedZip(app, ctx) {
  const active = ctx.ws.activePages;
  if (!active.length) {
    toast('No hay páginas para descargar.', 'error');
    return;
  }

  const compression = ctx.compressLevel;
  const rawSingleName = ctx.singleOutputName.trim();
  const outputName = rawSingleName || 'Documento organizado';
  const pdfFilename = _withPdfExt(outputName);
  const zipFilename = `${_stripPdfExt(outputName)}.zip`;

  // Un único grupo con todas las páginas activas. Los cortes se ignoran
  // exclusivamente para el ZIP del pie.
  const groups = [{
    name: outputName,
    pages: active,
    compression,
  }];

  const btn = app.querySelector('#export-zip-btn');
  const originalLabel = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = SPINNER_14;
  }

  try {
    const req = ctx.ws.getExportRequest(groups);
    req.make_zip = true;

    debugLog('[Bigpdf] UNIFIED ZIP REQUEST', {
      outputName: req.outputs?.[0]?.name,
      pdfFilename,
      zipFilename,
      splitMode: ctx.currentMode === 'split',
      pages: active.length,
      compression: req.outputs?.[0]?.compression,
    });

    const progressMsg = compression !== 'none'
      ? 'Comprimiendo y preparando el ZIP…'
      : 'Generando el ZIP…';

    const result = await API.exportWorkspace(req, progressMsg, true);

    if (!result.zip_export_id) {
      API.showStatusError('No se pudo generar el ZIP.');
      return;
    }

    const saved = await API.saveExport(
      result.zip_export_id,
      zipFilename,
      'Guardando ZIP…',
    );

    if (saved.cancelled) {
      toast('Guardado cancelado.', 'warning');
      return;
    }

    API.showSuccess(saved.message || `"${zipFilename}" guardado`);
  } catch (e) {
    _showExportError(e, 'Error al generar el ZIP.');
  } finally {
    if (btn) {
      btn.disabled = false;
      if (originalLabel) btn.innerHTML = originalLabel;
    }
  }
}

export async function exportAll(app, ctx, makeZip, groupState) {
  const groups = groupState || ctx.ws.getOutputGroups();
  if (!groups.length) { toast('No hay grupos para exportar.', 'warning'); return; }

  const btn = app.querySelector('#dl-zip-all') || app.querySelector('#export-zip-btn');
  const origBtnHtml = btn?.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = SPINNER_14; }

  // Compute final download filenames from group.name (same defensive logic as "Todas")
  const resolvedNames = groups.map((g, i) => _userPdfFilename(g?.name) || `parte-${i + 1}.pdf`);
  debugLog('[Bigpdf] ZIP PAYLOAD FINAL', groups.map((g, i) => ({ idx: i, name: g.name, resolvedFilename: resolvedNames[i], pageIdCount: g.pages?.length })));

  try {
    const req = ctx.ws.getExportRequest(groups);
    req.make_zip = !!makeZip;
    const result = await API.exportWorkspace(
      req,
      makeZip ? 'Generando las partes para el ZIP…' : 'Generando archivos…',
      true,
    );

    if (!result.outputs?.length) {
      toast('No se generaron archivos.', 'error');
      return;
    }

    if (makeZip && result.zip_export_id) {
      const saved = await API.saveExport(
        result.zip_export_id,
        result.zip_filename || 'bigpdf_partes.zip',
        'Guardando ZIP…',
      );
      if (saved.cancelled) {
        toast('Guardado cancelado.', 'warning');
      } else {
        API.showSuccess(saved.message || 'ZIP guardado');
      }
    } else {
      const n = result.outputs.length;
      for (let i = 0; i < n; i++) {
        const out = result.outputs[i];
        const serverFilename = out.filename;
        const downloadFilename = resolvedNames[i]
          || (!_isInternalFilename(serverFilename) ? serverFilename : `parte-${i + 1}.pdf`);
        const successMsg = n > 1
          ? `Parte ${i + 1} de ${n} guardada`
          : (realName) => `"${realName}" guardado`;
        await _triggerDownload(API.downloadUrl(out.export_id), downloadFilename, null, false, successMsg);
      }
    }
  } catch (e) {
    if (_isCancelled(e)) toast('Procesamiento cancelado.', 'warning');
    else toast(e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; if (origBtnHtml) btn.innerHTML = origBtnHtml; }
  }
}

export async function downloadAllSeparately(app, ctx, groups) {
  if (!groups.length) return;
  const total = groups.length;

  // Snapshot inmutable: copia profunda del array pages de cada grupo.
  // Evita que drag de páginas durante el lote mute la membresía de grupos futuros.
  const snapGroups = groups.map(g => ({ ...g, pages: [...(g.pages || [])] }));
  // Los requests (con rotación y estado de cada hoja) se arman TODOS al hacer clic:
  // si el usuario sigue editando mientras se guardan, las partes que faltan salen
  // como estaban al pedir "Todas", no con los cambios posteriores.
  const snapRequests = snapGroups.map(g => {
    const req = ctx.ws.getExportRequest([g]);
    req.make_zip = false;
    return req;
  });

  // Deshabilitar botones de exportación durante el lote.
  const batchBtns = [
    app.querySelector('#dl-all-sep'),
    app.querySelector('#dl-zip-all'),
  ].filter(Boolean);
  const batchOrigHtml = batchBtns.map(b => b.innerHTML);
  const spinner12 = '<span class="loading-spinner" style="width:12px;height:12px;border-width:2px"></span>';
  batchBtns.forEach((b, i) => { b.disabled = true; if (i === 0) b.innerHTML = spinner12; });

  // Chip pinneado — no parpadea entre partes.
  API.showProgress(total > 1 ? `Preparando ${total} partes…` : 'Preparando…');
  debugLog('[Todas] INICIO', { total, parts: snapGroups.map(g => ({ name: g.name, pages: g.pages?.length })) });

  let failed = 0;
  let skipped = 0; // partes cuyo guardado canceló el usuario (diálogo "Guardar como")
  let cancelled = false;
  try {
    for (let i = 0; i < total; i++) {
      const group = snapGroups[i];
      debugLog(`[Todas] parte ${i + 1}/${total} INICIO`, { name: group.name, pages: group.pages?.length });
      try {
        // Generar PDF — no bloqueante (chip, no overlay).
        const genMsg = total > 1 ? `Generando parte ${i + 1} de ${total}…` : 'Generando PDF…';
        API.updateProgress(genMsg);
        const req = snapRequests[i];
        debugLog(`[Todas] parte ${i + 1} → exportWorkspace`, { outputs: req.outputs?.length, pages: req.pages?.length });
        const result = await API.exportWorkspace(req, genMsg, true); // batchMode=true
        debugLog(`[Todas] parte ${i + 1} ← exportWorkspace OK`, { export_id: result?.outputs?.[0]?.export_id });

        if (result.outputs?.[0]) {
          const out = result.outputs[0];
          const serverFilename = out.filename;
          const userFilename = _userPdfFilename(group.name);
          const downloadFilename = _pickDownloadFilename(serverFilename, userFilename, `parte-${i + 1}.pdf`);

          // Guardar — chip ya pinneado, no muestra/oculta por cuenta propia.
          const saveMsg = total > 1 ? `Guardando parte ${i + 1} de ${total}…` : 'Guardando archivo…';
          API.updateProgress(saveMsg);
          debugLog(`[Todas] parte ${i + 1} → saveExport`, { export_id: out.export_id, downloadFilename });
          // silent=true: solo el chip cubre la confirmación individual en lote.
          const status = await _triggerDownload(API.downloadUrl(out.export_id), downloadFilename, saveMsg, total > 1);
          if (status !== 'saved') skipped++;
          debugLog(`[Todas] parte ${i + 1} ← saveExport`, status);
          if (i < total - 1) await new Promise(r => setTimeout(r, 250));
        } else {
          debugLog(`[Todas] parte ${i + 1} — exportWorkspace sin outputs`, result);
          failed++;
          toast(`No se generó el archivo de "${group.name}".`, 'error');
        }
      } catch (e) {
        if (_isCancelled(e)) {
          cancelled = true;
          debugLog(`[Todas] parte ${i + 1} CANCELADA`);
          break;
        }
        failed++;
        debugLog(`[Todas] parte ${i + 1} CATCH`, e?.message);
        toast(`Error en "${group.name}": ${e.message}`, 'error');
      }
    }
  } finally {
    // Siempre restaurar botones y limpiar chip, incluso si hubo excepción inesperada.
    batchBtns.forEach((b, i) => { b.disabled = false; b.innerHTML = batchOrigHtml[i]; });
    const savedCount = total - failed - skipped;
    debugLog(`[Todas] FINALLY: failed=${failed} skipped=${skipped} saved=${savedCount}`);
    if (cancelled) {
      API.hideProgress();
      toast('Procesamiento cancelado.', 'warning');
    } else if (failed === 0 && skipped === 0) {
      // Solo chip — no toast duplicado para flujo "Todas".
      API.showSuccess(total === 1 ? '1 archivo guardado' : `${total} partes guardadas`);
    } else {
      API.hideProgress();
      toast(`${savedCount} de ${total} partes guardadas.`, 'warning');
    }
  }
}

function _getExportBtnLabel() {
  return `${svgIcon('download', 14)} Descargar PDF`;
}

// chipMsg: texto del chip durante el guardado (null = genérico).
// silent: si true, suprime confirmación individual (útil en lotes gestionados desde afuera).
// successMsg: texto del chip de éxito (null = genérico), o función que recibe el
// nombre REAL con que se guardó (puede ser "Informe (2).pdf" si ya existía, o el
// nombre corregido si traía caracteres no válidos). Si silent=true, se ignora.
// Devuelve 'saved' o 'cancelled'.
async function _triggerDownload(exportId, filename, chipMsg = null, silent = false, successMsg = null) {
  debugLog('[Bigpdf] _triggerDownload desktop', { exportId, filename });
  const saved = await API.saveExport(exportId, filename, chipMsg);
  if (saved.cancelled) {
    toast('Guardado cancelado.', 'warning');
    return 'cancelled';
  }
  const realName = saved.filename || filename;
  const msg = typeof successMsg === 'function' ? successMsg(realName) : successMsg;
  if (!silent) API.showSuccess(msg || saved.message || 'Archivo guardado');
  return 'saved';
}
