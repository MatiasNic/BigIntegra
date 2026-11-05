import { escapeHtml } from '../js/utils.js';

// Modal propio de confirmación, reemplaza window.confirm() (el diálogo nativo del
// navegador embebido muestra un título genérico "JavaScript Confirm" poco claro).
// Reutilizable para cualquier confirmación futura de la app.
//
// Accesibilidad: role="dialog" + aria-modal, foco inicial en "Cancelar" (la opción
// segura), Tab queda atrapado entre los botones, Escape cancela y Enter activa el
// botón que tenga el foco (no confirma "a ciegas").
let _activeCleanup = null;

export function showConfirmDialog({ title = 'Confirmar', message = '', confirmText = 'Aceptar', cancelText = 'Cancelar' } = {}) {
  return new Promise((resolve) => {
    // Si había otro diálogo abierto, se resuelve como cancelado antes de abrir éste.
    if (_activeCleanup) _activeCleanup(false);

    const previousFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'bigpdf-confirm-overlay';
    overlay.innerHTML = `
      <div class="bigpdf-confirm-modal" role="dialog" aria-modal="true"
           aria-labelledby="bigpdf-confirm-title" aria-describedby="bigpdf-confirm-message">
        <div class="bigpdf-confirm-title" id="bigpdf-confirm-title">${escapeHtml(title)}</div>
        <div class="bigpdf-confirm-message" id="bigpdf-confirm-message">${escapeHtml(message)}</div>
        <div class="bigpdf-confirm-actions">
          <button type="button" class="btn btn-ghost ws-btn" id="bigpdf-confirm-cancel">${escapeHtml(cancelText)}</button>
          <button type="button" class="btn btn-primary ws-btn" id="bigpdf-confirm-ok">${escapeHtml(confirmText)}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const cancelBtn = overlay.querySelector('#bigpdf-confirm-cancel');
    const okBtn = overlay.querySelector('#bigpdf-confirm-ok');
    const buttons = [cancelBtn, okBtn];

    let done = false;
    const cleanup = (result) => {
      if (done) return;
      done = true;
      overlay.remove();
      if (_activeCleanup === cleanup) _activeCleanup = null;
      document.removeEventListener('keydown', onKey, true);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        try { previousFocus.focus(); } catch (_) {}
      }
      resolve(result);
    };
    _activeCleanup = cleanup;

    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        cleanup(false);
      } else if (e.key === 'Enter') {
        // Enter solo activa el botón enfocado (comportamiento nativo del <button>).
        // Si el foco quedó fuera de los botones, no se confirma nada.
        if (!buttons.includes(document.activeElement)) {
          e.preventDefault();
          e.stopPropagation();
        }
      } else if (e.key === 'Tab') {
        // Foco atrapado dentro del diálogo.
        e.preventDefault();
        const i = buttons.indexOf(document.activeElement);
        const next = e.shiftKey
          ? buttons[(i <= 0 ? buttons.length : i) - 1]
          : buttons[(i + 1) % buttons.length];
        next.focus();
      }
    };
    // Captura: el diálogo atiende sus teclas antes que los atajos del workspace.
    document.addEventListener('keydown', onKey, true);

    cancelBtn.addEventListener('click', () => cleanup(false));
    okBtn.addEventListener('click', () => cleanup(true));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(false); });

    cancelBtn.focus();
  });
}
