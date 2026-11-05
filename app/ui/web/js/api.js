// Fachada fina sobre window.bigpdfApi (desktop-bridge.js). No hay HTTP: todo va
// por QWebChannel. Solo se exponen los métodos que usa la interfaz actual.
async function _desktop() {
  await window.bigpdfReady;
  return window.bigpdfApi;
}

export const API = {
  async getFileUrl(id) {
    return (await _desktop()).getFileUrl(id);
  },

  downloadUrl(exportId) {
    // Ya no hay /api/download. Este ID lo consume saveExport().
    return exportId;
  },

  async importFiles(files) {
    return (await _desktop()).importFiles(files);
  },

  // overlayMsg y batchMode se pasan al bridge para elegir chip vs overlay.
  async exportWorkspace(req, overlayMsg = null, batchMode = false) {
    return (await _desktop()).exportWorkspace(req, overlayMsg, batchMode);
  },

  // chipMsg se pasa al bridge para actualizar el chip durante lotes.
  async saveExport(exportId, filename, chipMsg = null) {
    return (await _desktop()).saveExport(exportId, filename, chipMsg);
  },

  async getDownloadLocationPref() {
    return (await _desktop()).getDownloadLocationPref();
  },

  async setDownloadLocationPref(value) {
    return (await _desktop()).setDownloadLocationPref(!!value);
  },

  async integraCatalog() {
    return (await _desktop()).integraCatalog();
  },

  async integraAnalyze(req) {
    return (await _desktop()).integraAnalyze(req);
  },

  isCancellation(error) {
    return !!error?.cancelled || error?.name === 'AbortError';
  },

  // ─── Status chip API (síncrono — DOM inmediato, no necesita await) ─────────
  // Delega directamente a window.bigpdfApi para evitar latencia de _desktop().
  // Solo disponible después de que bigpdfReady resuelve (cualquier click post-carga).

  showProgress(msg) { window.bigpdfApi?.showProgress(msg); },

  updateProgress(msg) { window.bigpdfApi?.updateProgress(msg); },

  showSuccess(msg, ms = 2000) { window.bigpdfApi?.showSuccess(msg, ms); },

  showStatusError(msg, ms = 3500) { window.bigpdfApi?.showStatusError(msg, ms); },

  hideProgress() { window.bigpdfApi?.hideProgress(); },
};
