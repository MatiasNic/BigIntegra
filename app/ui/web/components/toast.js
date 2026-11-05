import { svgIcon, escapeHtml } from '../js/utils.js';

const container = document.getElementById('toast-container');

export function toast(message, type = 'info', duration = 4000) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;

  const iconMap = { success: 'check-circle', error: 'x-circle', warning: 'alert', info: 'check-circle' };
  el.innerHTML = `${svgIcon(iconMap[type] || 'check-circle', 18)}<span>${escapeHtml(message)}</span>`;

  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));

  setTimeout(() => {
    el.classList.remove('show');
    el.classList.add('hide');
    setTimeout(() => el.remove(), 350);
  }, duration);
}
