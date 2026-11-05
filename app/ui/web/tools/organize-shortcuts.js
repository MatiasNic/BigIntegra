// Atajos de teclado del workspace (Ctrl+Z/Ctrl+Y, Ctrl+A, Supr, Esc). Extraído de
// organize.js. `ctx` es el contexto vivo de organize.js (ws, grid, applyHistory).

let _workspaceKeyHandler = null;

export function removeWorkspaceShortcuts() {
  if (_workspaceKeyHandler) {
    document.removeEventListener('keydown', _workspaceKeyHandler);
    _workspaceKeyHandler = null;
  }
}

export function installWorkspaceShortcuts(app, ctx) {
  removeWorkspaceShortcuts();
  _workspaceKeyHandler = (event) => {
    const target = event.target;
    const isEditable = target instanceof HTMLElement && (
      target.matches('input, textarea, select') || target.isContentEditable
    );
    if (isEditable) return;
    if (document.querySelector('.pg-viewer-overlay, .bigpdf-confirm-overlay, #bp-job-overlay.visible')) return;
    if (document.body.classList.contains('artemis-dragging')) return;
    if (!ctx.ws || !ctx.grid) return;

    const key = event.key.toLowerCase();
    // Ctrl+Z deshacer · Ctrl+Y / Ctrl+Shift+Z rehacer (también en vista Archivos).
    if ((event.ctrlKey || event.metaKey) && !event.altKey) {
      if (key === 'z' && !event.shiftKey) {
        event.preventDefault();
        ctx.applyHistory(app, 'undo');
        return;
      }
      if (key === 'y' || (key === 'z' && event.shiftKey)) {
        event.preventDefault();
        ctx.applyHistory(app, 'redo');
        return;
      }
    }

    if (!ctx.ws.pages.length || ctx.ws.viewType !== 'pages') return;
    if ((event.ctrlKey || event.metaKey) && key === 'a') {
      event.preventDefault();
      app.querySelector('#sel-all-btn')?.click();
    } else if (event.key === 'Delete') {
      event.preventDefault();
      if (ctx.ws.selectedCount) app.querySelector('#del-btn')?.click();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      app.querySelector('#sel-none-btn')?.click();
    }
  };
  document.addEventListener('keydown', _workspaceKeyHandler);
}
