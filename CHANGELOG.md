# Registro de cambios

Qué cambió en cada versión de Big Integra, en lenguaje de usuario. Lo más nuevo arriba.

**Regla de versiones:** cada tanda de cambios que se entrega lleva un número nuevo,
subiendo el último dígito: 0.1.0 → 0.1.1 → 0.1.2… El número vive solo en
`app/__init__.py`; el `.exe` y la ventana de inicio lo toman de ahí.

## [Sin publicar]

(Todavía nada.)

## [0.1.0] - 2026-11-05

Primera versión. Big Integra parte de Big PDF 1.4.8 (importar, organizar, separar,
comprimir y exportar siguen igual) y agrega el panel **Integra** para armar y revisar
legajos de Discapacidad, sin IA y sin enviar documentos fuera de la PC.

### Nuevo
- **Panel Integra** (botón "Integra" en la barra): se marcan las prestaciones que se
  cargan (Rehabilitación, Estimulación Temprana, Terapéutico-Educativa, Transporte,
  Escolaridad Especial, Módulo de Integración, Maestra de Apoyo) y las condiciones
  (nueva/continuidad, profesional/institución, dependencia, edad escolar, cambio de
  prestador), y se aprieta **Analizar**.
- **Identificación de cada hoja**: cada miniatura muestra qué documento es (CUD,
  Prescripción médica, Presupuesto, FIM, Conformidad, ARCA, CBU…). Las dudosas se ven
  en naranja; un clic en la etiqueta permite corregirla y el checklist se recalcula.
- **Lectura de escaneos y fotos** con OCR incorporado (no hay que instalar nada).
- **Checklist por prestación** según los instructivos 2024: ✔ correcto, ⚠ a revisar,
  ❌ faltante, con la hoja donde se encontró cada documento (clic para ir a la hoja).
- **Controles automáticos**: vigencia del CUD (con la prórroga de la Res. 1907/2023),
  fecha de emisión anterior al período, período y sesiones de la orden médica vs. el
  presupuesto, CUIT y CBU con dígito verificador inválido, ARCA o documentación
  bancaria con más de 2 años, zona desfavorable, DNI distintos en el legajo.
- **Aplicar orden sugerido**: reordena las hojas como indica el instructivo y arma
  las partes con el nombre de cada documento (modo Entrada) o un único legajo (modo
  Integración). Las hojas de instructivo quedan aparte como "NO PRESENTAR". Se puede
  deshacer con Ctrl+Z.
- **Respuesta sugerida al socio** e informe completo, listos para copiar.
- Herramienta de calibración para medir el acierto con legajos reales en la PC local
  (`tools/dev/integra_eval.py`).

### Pendiente
- Requisitos de Escolaridad Especial, Módulo de Integración y Maestra de Apoyo son
  provisorios (tomados del flujo anterior) hasta tener sus instructivos.
- Los instructivos cargados son los de 2024: actualizar el catálogo con los vigentes.
