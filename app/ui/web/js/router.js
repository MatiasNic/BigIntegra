const routes = {};

export function registerRoute(hash, handler) {
  routes[hash] = handler;
}

export function initRouter(app) {
  async function resolve() {
    // BUG-7: cancelar jobs pendientes antes de desmontar la ruta actual.
    window._bigpdfClearPendingJobs?.();
    const hash = window.location.hash.replace('#', '') || '/';
    const handler = routes[hash] || routes['/'];
    if (handler) {
      app.innerHTML = '';
      await handler(app);
    }
  }

  window.addEventListener('hashchange', resolve);
  resolve();
}
