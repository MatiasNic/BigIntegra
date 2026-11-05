import { initRouter, registerRoute } from './router.js';
import { renderWorkspace } from '../tools/organize.js?v=5';
import { toast } from '../components/toast.js';

const app = document.getElementById('app');

window.bigpdfToast = (message, type) => toast(message, type || 'success');

// Única pantalla de la app. Cualquier hash desconocido cae en '/' (ver router.js).
registerRoute('/', renderWorkspace);

await window.bigpdfReady;
initRouter(app);

// IMPORTANTE DESKTOP/ONEFILE:
// No limpiar documentos en pagehide. En QWebEngine/PyInstaller este evento puede
// dispararse aunque la ventana siga abierta, dejando la grilla con páginas en memoria
// pero borrando los PDFs fuente de uploads. La limpieza real se hace desde Python
// en MainWindow.closeEvent -> cleanup_session_temp_dir().
