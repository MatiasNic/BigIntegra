# Lo que hacía el agente anterior (tabla "Agente Integración", SharePoint)

Análisis **agregado** de la exportación de la lista `EquipoDiscapacidad` (612 legajos,
447 con análisis del agente). No contiene nombres, DNI, CUIL ni números de trámite: los
datos originales no se suben al repositorio.

## Volumen

- Estado del flujo: Analizado 420 · sin estado 133 · Reprocesar 44 · Procesando 14 · Error 1.
- Reprocesos: 0 → 455 · 1 → 91 · 2 → 15 · 3 → 11 · 4 → 3.
- Prestaciones por legajo: 1 → 176 · 2 → 139 · 3 → 75 · 4 o más → 52.

## Prestaciones (legajos que la incluyen)

| Prestación | Legajos | Estado en Big Integra |
|---|---|---|
| Rehabilitación | 364 | Instructivo 2024 |
| Módulo de apoyo a la integración escolar | 112 | **Provisorio (falta instructivo)** |
| Transporte | 62 | Instructivo 2024 |
| Maestra de apoyo | 56 | **Provisorio (falta instructivo)** |
| Escolaridad especial | 30 | **Provisorio (falta instructivo)** |
| Terapéutico-educativa (CET / Centro de Día) | 22 | Instructivo 2024 |
| Acompañante terapéutico y otras | ~17 | No contemplada |
| Estimulación temprana | 2 | Instructivo 2024 |

Combinaciones más comunes: solo Rehabilitación (195), MAIE + Rehabilitación (64), solo
MAIE (35), Maestra de apoyo + Rehabilitación (33).

## Faltantes que marcaba (ítems ❌)

CUD 193 · Constancia de alumno regular / CUE 186 · Orden médica 102 · Título habilitante 96 ·
ARCA 76 · Resumen de HC 76 · Conformidad prestacional 76 · Presupuesto 67 · Adaptación
curricular 62 · Plan de abordaje 60 · CBU / formulario bancario 53 · RNP 51 ·
Documentación del transportista 45 · Acta de acuerdo 43.

## Observaciones (ítems ⚠)

- Por documento: ARCA 542 · Presupuesto 451 · Orden médica 304 · Conformidad 212 ·
  CBU / bancaria 210 · Resumen de HC 125 · Transportista 120 · RNP 104.
- Por tema: fecha / período 860 · vencimiento / vigencia 629 · OCR / legibilidad 600 ·
  no coincide 277 · firma / sello 224 · sesiones / frecuencia 171 · frente / dorso 38.
- ARCA: de 766 menciones, 276 la marcan "vencida / no vigente" y 165 "con más de 2 años".
  Las instrucciones del unificador decían alertar solo con 2 años o más: el agente no lo
  respetaba de forma consistente.

## Hallazgos que se incorporaron a Big Integra

- **El número de CUD codifica las fechas**: `ARG-xx-<11 dígitos>-AAAAMMDD(emisión)-AAAAMMDD(vencimiento)-PROV-nnn`.
  El vencimiento informado coincidía con ese bloque en 274 de 281 casos. Big Integra lee
  el número (317 de 327 números de la tabla) y toma el vencimiento de ahí. Los 11 dígitos
  no son un CUIL (ninguno valida).
- **CUE**: 9 dígitos (7 + 2 de anexo), a veces escrito `9999999-99`.
- **Prestaciones elegidas por el usuario**: el agente a veces "detectaba" una prestación por
  una mención aislada (una escuela nombrada en el presupuesto de transporte) y pedía toda su
  documentación como faltante. En Big Integra las prestaciones las marca el usuario.

## Pendiente

- Instructivos de MAIE, Maestra de apoyo y Escolaridad especial (juntas: 198 legajos).
- Definir el criterio de ARCA vencida (¿cualquier vencimiento o solo ≥ 2 años?).
- Regla de recorrido: el agente comparaba km del presupuesto con Google Maps (28 observaciones).
