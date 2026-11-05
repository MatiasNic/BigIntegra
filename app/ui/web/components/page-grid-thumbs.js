// Miniaturas de PageGrid: cola de render con concurrencia limitada, caché de
// imágenes WebP (LRU por bytes), IntersectionObserver de cercanía y precarga en
// momentos ociosos. Se mezcla en PageGrid.prototype (ver page-grid.js): los
// métodos usan `this` igual que si estuvieran declarados en la clase.

const THUMB_WIDTH = 180;       // ancho CSS de respaldo si la tarjeta todavía no tiene layout
const THUMB_WIDTH_STEP = 60;   // el ancho real se redondea hacia arriba a múltiplos de esto
const THUMB_MAX_CSS_W = 720;   // tope de ancho CSS por miniatura (vista "2 por fila" en pantallas grandes)
const FILE_THUMB_W = 44;
const FILE_THUMB_GRID_W = 100;
const THUMB_CONCURRENCY = 7;   // max parallel PDF.js renders
// Las miniaturas renderizadas se guardan comprimidas (WebP, ~20-40 KB cada una) y
// se muestran en un <img>. Una vez cargadas quedan fijas: al scrollear arriba y
// abajo NO se vuelven grises ni se re-renderizan. El navegador decodifica la imagen
// al instante si la había descartado de memoria. Tope de seguridad por si hubiera
// miles de páginas: ~400 MB solo se alcanzaría con más de 10.000 miniaturas.
const THUMB_CACHE_MAX_BYTES = 400 * 1024 * 1024;
const THUMB_MIME = 'image/webp';
const THUMB_QUALITY = 0.92;
const NEAR_MARGIN = '600px 0px';

// DPR real de la pantalla (antes se forzaba ≥ 2 incluso en monitores 1x, ~4x más RAM).
function _thumbDpr() {
  return Math.min(window.devicePixelRatio || 1, 2);
}

// Rotación efectiva para pdf.js: page.rotation en el estado de la UI es un DELTA del
// usuario relativo a la orientación propia de la página (/Rotate). pdf.js reemplaza
// /Rotate por el valor que se le pasa, así que hay que sumarlos.
export function effectiveRotation(pdfPage, userRotation) {
  return (((pdfPage?.rotate || 0) + (Number(userRotation) || 0)) % 360 + 360) % 360;
}

export const thumbMethods = {
  // Marca la miniatura como desactualizada sin vaciarla (evita parpadeo). Si la
  // tarjeta está cerca de la vista se vuelve a encolar.
  _invalidateCard(card) {
    delete card.dataset.rendered;
    if (this._nearCards.has(card)) this._queueCardThumb(card);
  },

  // Recalcula el tamaño de render de las miniaturas (cambio de tamaño de grilla o
  // de ventana). Solo invalida las tarjetas cuyo ancho objetivo cambió.
  _refreshThumbSizes() {
    if (this._destroyed) return;
    this.container.querySelectorAll('.pg-card[data-rendered]').forEach(card => {
      const wrap = card.querySelector('.pg-canvas-wrap');
      if (!wrap) return;
      if (String(this._thumbPixelWidth(wrap)) !== card.dataset.thumbPx) this._invalidateCard(card);
    });
    this.container.querySelectorAll('.pg-file-card').forEach(card => {
      const thumb = card.querySelector('.pg-file-thumb');
      if (!thumb?.dataset.rendered) return;
      if (String(this._fileThumbPixelWidth(thumb)) !== thumb.dataset.thumbPx) {
        delete thumb.dataset.rendered;
        if (this._nearCards.has(card)) this._queueFileThumb(card);
      }
    });
  },

  // Ancho en píxeles de dispositivo al que se renderiza una miniatura de página:
  // ancho real de la tarjeta, redondeado hacia arriba para no invalidar por 1px.
  _thumbPixelWidth(wrap) {
    const cssW = wrap?.clientWidth || THUMB_WIDTH;
    const stepped = Math.min(THUMB_MAX_CSS_W, Math.ceil(cssW / THUMB_WIDTH_STEP) * THUMB_WIDTH_STEP);
    return Math.round(stepped * _thumbDpr());
  },

  _fileThumbPixelWidth(thumb) {
    const fallback = this.state.viewMode === 'grid' ? FILE_THUMB_GRID_W : FILE_THUMB_W;
    const cssW = thumb?.clientWidth || fallback;
    const stepped = Math.min(THUMB_MAX_CSS_W, Math.ceil(cssW / 20) * 20);
    return Math.round(stepped * _thumbDpr());
  },

  _getCachedThumb(key) {
    const entry = this._thumbCache.get(key);
    if (!entry) return null;
    // Cache hit: refresca la posición LRU.
    this._thumbCache.delete(key);
    this._thumbCache.set(key, entry);
    return entry;
  },

  // Convierte el render (canvas fuera de pantalla) en una imagen comprimida.
  // Mucho más liviano que dejar un canvas por tarjeta (~1 MB sin comprimir).
  async _encodeThumb(offscreen) {
    const blob = await new Promise((resolve) => {
      try { offscreen.toBlob(resolve, THUMB_MIME, THUMB_QUALITY); } catch (_) { resolve(null); }
    });
    if (!blob) return null;
    const url = URL.createObjectURL(blob);
    this._thumbUrls.add(url);
    return { url, w: offscreen.width, h: offscreen.height, bytes: blob.size };
  },

  _setCachedThumb(key, entry) {
    const previous = this._thumbCache.get(key);
    if (previous) this._thumbCacheBytes -= previous.bytes;
    this._thumbCache.delete(key);
    this._thumbCache.set(key, entry);
    this._thumbCacheBytes += entry.bytes;

    // Solo si se supera el tope (miles de páginas) se descartan las más viejas. La
    // URL no se revoca acá: alguna tarjeta podría seguir mostrándola.
    while (this._thumbCacheBytes > THUMB_CACHE_MAX_BYTES && this._thumbCache.size > 1) {
      const oldestKey = this._thumbCache.keys().next().value;
      this._thumbCacheBytes -= this._thumbCache.get(oldestKey).bytes;
      this._thumbCache.delete(oldestKey);
    }
  },

  _deleteThumbCacheByPrefix(prefix) {
    for (const key of [...this._thumbCache.keys()]) {
      if (!key.startsWith(prefix)) continue;
      this._thumbCacheBytes -= this._thumbCache.get(key).bytes;
      this._thumbCache.delete(key);
    }
  },

  // Muestra una miniatura en el <img> del contenedor. Si la URL anterior ya no
  // está en la caché (p. ej. tras rotar), se revoca para no acumular memoria.
  _showThumb(container, entry) {
    const img = container.querySelector('img.pg-thumb-img');
    if (!img) return;
    const previous = img.dataset.url;
    if (previous === entry.url) return;
    img.width = entry.w;
    img.height = entry.h;
    img.src = entry.url;
    img.dataset.url = entry.url;
    if (previous && ![...this._thumbCache.values()].some(e => e.url === previous)) {
      // Se revoca después de que el <img> cargó la nueva, para no dejarlo en blanco.
      img.decode?.().catch(() => {}).finally(() => {
        try { URL.revokeObjectURL(previous); } catch (_) {}
        this._thumbUrls.delete(previous);
      });
    }
  },

  // ─── Concurrency queue ────────────────────────────────────────────────────────

  // key: clave única por tarjeta ('page:<id>' / 'file:<docId>') — evita duplicados.
  // resolve(): busca la tarjeta ACTUAL al momento de ejecutar (no la del momento de
  // encolar), así un render() intermedio no deja tareas apuntando a tarjetas viejas.
  // run(card): hace el trabajo sobre esa tarjeta.
  _queueThumb(key, resolve, run) {
    if (this._thumbInFlight.has(key)) return;
    this._thumbInFlight.add(key);
    this._qPending.push({ key, resolve, run, gen: this._renderGen });
    this._drainQueue();
  },

  _drainQueue() {
    while (this._qPending.length && this._qActive < THUMB_CONCURRENCY) {
      // LIFO: lo último que entró en la vista es lo que el usuario está mirando.
      const task = this._qPending.pop();
      if (task.gen !== this._renderGen) continue;
      const card = task.resolve();
      // Tarjeta descartada o que ya salió de la zona cercana: se saltea. Cuando
      // vuelva a entrar, el observer la encola de nuevo.
      if (!card || !card.isConnected || !this._nearCards.has(card)) {
        this._thumbInFlight.delete(task.key);
        continue;
      }
      this._qActive++;
      Promise.resolve()
        .then(() => task.run(card))
        .catch(() => {})
        .finally(() => {
          if (task.gen === this._renderGen) this._thumbInFlight.delete(task.key);
          this._qActive--;
          this._drainQueue();
        });
    }
    if (!this._qPending.length && this._qActive === 0) this._schedulePrefetch();
  },

  // ─── Precarga en segundo plano ────────────────────────────────────────────────
  // Cuando no queda nada visible por dibujar, se van renderizando de a una (en
  // momentos ociosos) las miniaturas que todavía no se vieron. Así, al scrollear
  // a cualquier parte del documento las miniaturas ya están listas.
  _schedulePrefetch() {
    if (this._destroyed || this._prefetchScheduled) return;
    this._prefetchScheduled = true;
    const run = () => { this._prefetchScheduled = false; this._prefetchNext(); };
    if (window.requestIdleCallback) window.requestIdleCallback(run, { timeout: 1500 });
    else setTimeout(run, 120);
  },

  async _prefetchNext() {
    if (this._destroyed || this._qActive > 0 || this._qPending.length) return;
    // No competir con un arrastre en curso.
    if (document.body.classList.contains('artemis-dragging')) { setTimeout(() => this._schedulePrefetch(), 500); return; }
    const gen = this._renderGen;
    const next = this.state.viewType === 'files'
      ? [...this.container.querySelectorAll('.pg-file-card')].find(c => !c.querySelector('.pg-file-thumb')?.dataset?.rendered)
      : [...this.container.querySelectorAll('.pg-card:not([data-rendered])')].find(c => !c.querySelector('.thumb-error'));
    if (!next) return;
    try {
      if (next.classList.contains('pg-file-card')) await this._renderFileThumb(next);
      else await this._renderThumbnail(next);
    } catch (_) {}
    // Si falló sin marcarse como renderizada, no reintentar en bucle.
    if (next.classList.contains('pg-card') && !next.dataset.rendered) {
      next.querySelector('.pg-canvas-wrap')?.classList.add('thumb-error');
    } else if (next.classList.contains('pg-file-card')) {
      const thumb = next.querySelector('.pg-file-thumb');
      if (thumb && !thumb.dataset.rendered) thumb.dataset.rendered = 'error';
    }
    if (gen === this._renderGen) this._schedulePrefetch();
  },

  _queueCardThumb(card) {
    const pageId = card.dataset.pageId;
    this._queueThumb(
      `page:${pageId}`,
      () => this._findPageCard(pageId),
      (c) => (c.dataset.rendered ? null : this._renderThumbnail(c)),
    );
  },

  _queueFileThumb(card) {
    const docId = card.querySelector('.pg-file-thumb')?.dataset?.docId;
    const blockIdx = card.dataset.blockIdx;
    this._queueThumb(
      `file:${docId}:${blockIdx}`,
      () => (card.isConnected ? card : null),
      (c) => this._renderFileThumb(c),
    );
  },

  async _renderFileThumb(card) {
    const thumb = card.querySelector('.pg-file-thumb');
    if (!thumb) return;
    const docId = thumb.dataset.docId;
    if (thumb.dataset.rendered) return;
    const pxW = this._fileThumbPixelWidth(thumb);
    const cacheKey = `file:${docId}:${pxW}`;

    // Check cache — instant restore
    const cached = this._getCachedThumb(cacheKey);
    if (cached) {
      this._showThumb(thumb, cached);
      thumb.classList.remove('loading');
      thumb.dataset.rendered = '1';
      thumb.dataset.thumbPx = String(pxW);
      return;
    }

    let offscreen = null;
    try {
      const pdfDoc = await this.getOrLoad(docId);
      const pdfPage = await pdfDoc.getPage(1);
      const baseVp = pdfPage.getViewport({ scale: 1 });
      const scale = pxW / baseVp.width;
      const vp = pdfPage.getViewport({ scale });
      offscreen = document.createElement('canvas');
      offscreen.width = Math.round(vp.width);
      offscreen.height = Math.round(vp.height);
      await pdfPage.render({ canvasContext: offscreen.getContext('2d'), viewport: vp }).promise;
      try { pdfPage.cleanup(); } catch (_) {}
      const entry = await this._encodeThumb(offscreen);
      if (!entry) throw new Error('No se pudo codificar la miniatura.');
      this._setCachedThumb(cacheKey, entry);
      if (!card.isConnected) return;
      this._showThumb(thumb, entry);
      thumb.classList.remove('loading');
      thumb.dataset.rendered = '1';
      thumb.dataset.thumbPx = String(pxW);
    } catch {
      thumb.classList.remove('loading');
    } finally {
      if (offscreen) { offscreen.width = 0; offscreen.height = 0; }
    }
  },

  // ─── Thumbnails & observer ────────────────────────────────────────────────────

  _setupObserver() {
    // Observer cercano: lleva el registro de qué tarjetas están en (o cerca de) la
    // vista. Se usa para encolar renders y para priorizar la cola.
    this._observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const el = entry.target;
        if (!entry.isIntersecting) {
          this._nearCards.delete(el);
          continue;
        }
        this._nearCards.add(el);
        if (el.classList.contains('pg-card')) {
          if (!el.dataset.rendered && !this._drawCardFromCache(el)) this._queueCardThumb(el);
        } else if (el.classList.contains('pg-file-card')) {
          if (!el.querySelector('.pg-file-thumb')?.dataset?.rendered) this._queueFileThumb(el);
        }
      }
    }, { rootMargin: NEAR_MARGIN });
  },

  _thumbCacheKey(card, pxW) {
    const rotation = parseInt(card.dataset.rotation) || 0;
    return `${card.dataset.pageId}:${rotation}:${pxW}`;
  },

  // Dibuja la miniatura desde la caché de bitmaps si está. Síncrono: sin parpadeo.
  _drawCardFromCache(card, knownPxW = null) {
    const wrap = card.querySelector('.pg-canvas-wrap');
    if (!wrap) return false;
    const pxW = knownPxW ?? this._thumbPixelWidth(wrap);
    const cached = this._getCachedThumb(this._thumbCacheKey(card, pxW));
    if (!cached) return false;
    // Un render en curso sobre esta tarjeta ya no hace falta.
    card._thumbToken = (card._thumbToken || 0) + 1;
    try { card._thumbRenderTask?.cancel(); } catch (_) {}
    card._thumbRenderTask = null;
    this._showThumb(wrap, cached);
    wrap.classList.remove('loading', 'thumb-error');
    card.dataset.rendered = '1';
    card.dataset.thumbPx = String(pxW);
    return true;
  },

  async _renderThumbnail(card) {
    // Cache hit → draw synchronously, no flash, no async wait
    if (this._drawCardFromCache(card)) return;

    const docId = card.dataset.docId;
    const pageNum = parseInt(card.dataset.pageNum);
    const rotation = parseInt(card.dataset.rotation) || 0;
    const wrap = card.querySelector('.pg-canvas-wrap');
    const pxW = this._thumbPixelWidth(wrap);
    const cacheKey = this._thumbCacheKey(card, pxW);

    // Token por tarjeta + cancelación del render anterior: rotar rápido dos veces
    // ya no lanza dos renders sobre el mismo canvas ni deja la rotación vieja.
    const token = (card._thumbToken || 0) + 1;
    card._thumbToken = token;
    try { card._thumbRenderTask?.cancel(); } catch (_) {}
    card._thumbRenderTask = null;

    let offscreen = null;
    try {
      const pdfDoc = await this.getOrLoad(docId);
      if (card._thumbToken !== token) return;
      const pdfPage = await pdfDoc.getPage(pageNum + 1);
      if (card._thumbToken !== token) return;
      const rot = effectiveRotation(pdfPage, rotation);
      const baseVp = pdfPage.getViewport({ scale: 1, rotation: rot });
      const scale = pxW / baseVp.width;
      const vp = pdfPage.getViewport({ scale, rotation: rot });
      // Se renderiza en un canvas fuera de pantalla y después se comprime a imagen.
      offscreen = document.createElement('canvas');
      offscreen.width  = Math.round(vp.width);
      offscreen.height = Math.round(vp.height);
      const task = pdfPage.render({ canvasContext: offscreen.getContext('2d'), viewport: vp });
      card._thumbRenderTask = task;
      try {
        await task.promise;
      } finally {
        if (card._thumbRenderTask === task) card._thumbRenderTask = null;
        // Libera recursos de la página (operator list, imágenes decodificadas).
        try { pdfPage.cleanup(); } catch (_) {}
      }
      // Se guarda comprimida: queda disponible para siempre (scroll, re-render, deshacer).
      const entry = await this._encodeThumb(offscreen);
      if (!entry) throw new Error('No se pudo codificar la miniatura.');
      this._setCachedThumb(cacheKey, entry);
      if (card._thumbToken !== token || !card.isConnected) return;
      this._showThumb(wrap, entry);
      wrap.classList.remove('loading', 'thumb-error');
      card.dataset.rendered = '1';
      card.dataset.thumbPx = String(pxW);
    } catch (err) {
      if (card._thumbToken !== token) return;
      if (err?.name === 'RenderingCancelledException') return;
      wrap.classList.remove('loading');
      wrap.classList.add('thumb-error');
    } finally {
      if (offscreen) { offscreen.width = 0; offscreen.height = 0; }
    }
  },
};
