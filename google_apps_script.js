/**
 * Base de datos del bot en Google Sheets.
 *
 * Cómo instalarlo (una sola vez):
 * 1. Creá una hoja nueva en Google Sheets (ej: "Bot WhatsApp - Base de datos").
 * 2. Menú Extensiones → Apps Script. Borrá lo que haya y pegá TODO este archivo.
 * 3. Cambiá CLAVE (abajo) por una palabra secreta tuya. La misma va en Render como REGISTRO_CLAVE.
 * 4. (Opcional, para agendar en Google Contactos) A la izquierda, en "Servicios", tocá "+",
 *    elegí "People API" y Agregar.
 * 5. Arriba a la derecha: Implementar → Nueva implementación → tipo "Aplicación web".
 *    Ejecutar como: Yo. Quién tiene acceso: Cualquier usuario. → Implementar → Autorizar.
 * 6. Copiá la URL de la aplicación web (termina en /exec) y cargala en Render como REGISTRO_URL.
 *
 * Si después cambiás este código: Implementar → Administrar implementaciones → editar (lápiz)
 * → Versión: "Nueva versión" → Implementar. (La URL sigue siendo la misma.)
 */

const CLAVE = 'CAMBIAME';          // misma palabra que REGISTRO_CLAVE en Render
const AGENDAR_EN_GOOGLE = true;    // crea el contacto en tus Contactos de Google (necesita People API)
const ETIQUETA_CONTACTO = 'Cliente WhatsApp bot';

const HOJA_CONSULTAS = 'Consultas';
const HOJA_CONTACTOS = 'Contactos';
const HOJA_RESUMEN = 'Resumen';

const COLS_CONSULTAS = ['Fecha', 'Número', 'Nombre', 'Tipo cliente', 'Negocio', 'Pieza', 'Código pedido',
  'Marca', 'Modelo', 'Motor', 'Año', '¿Teníamos?', 'Código ofrecido', 'Precio ofrecido', 'Notas'];
const COLS_CONTACTOS = ['Número', 'Nombre WhatsApp', 'Tipo cliente', 'Negocio / localidad', 'País',
  'Primera vez', 'Última vez', 'Mensajes', 'Consultas', 'Agendado en Google'];


function doPost(e) {
  const datos = JSON.parse(e.postData.contents);
  if (datos.clave !== CLAVE) return respuesta({ ok: false, error: 'clave incorrecta' });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    preparar();
    if (datos.accion === 'consulta') registrarConsulta(datos);
    if (datos.accion === 'contacto') registrarContacto(datos);
    return respuesta({ ok: true });
  } finally {
    lock.releaseLock();
  }
}


// Descarga de contactos para importar en el celular: <URL>?clave=TU_CLAVE&formato=vcf
function doGet(e) {
  if (e.parameter.clave !== CLAVE) return ContentService.createTextOutput('clave incorrecta');
  preparar();
  const filas = hoja(HOJA_CONTACTOS).getDataRange().getValues().slice(1);
  const vcf = filas.filter(f => f[0]).map(f => [
    'BEGIN:VCARD', 'VERSION:3.0',
    'FN:' + nombreContacto(f[1], f[3], f[0]),
    'TEL;TYPE=CELL:+' + f[0],
    'NOTE:' + ETIQUETA_CONTACTO + (f[2] ? ' - ' + f[2] : ''),
    'END:VCARD'].join('\n')).join('\n');
  return ContentService.createTextOutput(vcf).setMimeType(ContentService.MimeType.VCARD)
    .downloadAsFile('contactos_bot.vcf');
}


function registrarConsulta(d) {
  hoja(HOJA_CONSULTAS).appendRow([
    new Date(), "'" + d.numero, d.nombre || '', d.tipo || '', d.negocio || '', d.pieza || '',
    d.codigo_pedido || '', d.marca || '', d.modelo || '', d.motor || '', d.anio || '',
    d.resultado || '', d.codigo_ofrecido || '', d.precio_ofrecido || '', d.notas || '']);
  // suma 1 a las consultas del contacto
  const fila = buscarContacto(d.numero);
  if (fila) {
    const celda = hoja(HOJA_CONTACTOS).getRange(fila, 9);
    celda.setValue((Number(celda.getValue()) || 0) + 1);
  }
}


function registrarContacto(d) {
  const h = hoja(HOJA_CONTACTOS);
  const ahora = new Date();
  let fila = buscarContacto(d.numero);
  if (!fila) {
    h.appendRow(["'" + d.numero, d.nombre || '', d.tipo || '', d.negocio || '', d.pais || '',
      ahora, ahora, 1, 0, '']);
    fila = h.getLastRow();
  } else {
    const valores = h.getRange(fila, 1, 1, COLS_CONTACTOS.length).getValues()[0];
    if (d.nombre) h.getRange(fila, 2).setValue(d.nombre);
    if (d.tipo) h.getRange(fila, 3).setValue(d.tipo);
    if (d.negocio) h.getRange(fila, 4).setValue(d.negocio);
    if (d.pais) h.getRange(fila, 5).setValue(d.pais);
    h.getRange(fila, 7).setValue(ahora);
    if (d.nuevo_mensaje) h.getRange(fila, 8).setValue((Number(valores[7]) || 0) + 1);
  }
  if (AGENDAR_EN_GOOGLE) agendarEnGoogle(fila);
}


// Crea el contacto en Google Contactos (se sincroniza con el celular si usás esa cuenta de Google).
function agendarEnGoogle(fila) {
  const h = hoja(HOJA_CONTACTOS);
  const v = h.getRange(fila, 1, 1, COLS_CONTACTOS.length).getValues()[0];
  if (v[9]) return; // ya agendado
  try {
    People.People.createContact({
      names: [{ givenName: nombreContacto(v[1], v[3], v[0]) }],
      phoneNumbers: [{ value: '+' + v[0], type: 'mobile' }],
      biographies: [{ value: ETIQUETA_CONTACTO + (v[2] ? ' - ' + v[2] : '') }],
    });
    h.getRange(fila, 10).setValue('Sí');
  } catch (err) {
    h.getRange(fila, 10).setValue('No (' + String(err).slice(0, 60) + ')');
  }
}


function nombreContacto(nombre, negocio, numero) {
  const base = negocio || nombre || ('Cliente ' + String(numero).slice(-4));
  return base + ' (WA bot)';
}


function buscarContacto(numero) {
  const h = hoja(HOJA_CONTACTOS);
  const n = h.getLastRow();
  if (n < 2) return null;
  const nums = h.getRange(2, 1, n - 1, 1).getValues();
  for (let i = 0; i < nums.length; i++) {
    if (String(nums[i][0]).replace("'", '') === String(numero)) return i + 2;
  }
  return null;
}


function hoja(nombre) {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(nombre);
}


// Crea las hojas y el resumen la primera vez
function preparar() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  crearHoja(ss, HOJA_CONSULTAS, COLS_CONSULTAS);
  crearHoja(ss, HOJA_CONTACTOS, COLS_CONTACTOS);
  if (!ss.getSheetByName(HOJA_RESUMEN)) {
    const r = ss.insertSheet(HOJA_RESUMEN, 0);
    const C = HOJA_CONSULTAS;
    const bloques = [
      ['A1', 'Piezas más pedidas',
        `=QUERY(${C}!A:O,"select F, count(A) where F is not null group by F order by count(A) desc label count(A) 'Veces'",1)`],
      ['D1', 'Vehículos más consultados',
        `=QUERY(${C}!A:O,"select H, I, count(A) where H is not null group by H, I order by count(A) desc label count(A) 'Veces'",1)`],
      ['H1', '¿Lo teníamos?',
        `=QUERY(${C}!A:O,"select L, count(A) where L is not null group by L order by count(A) desc label count(A) 'Veces'",1)`],
      ['K1', 'Lo que piden y NO tenemos (para reponer)',
        `=QUERY(${C}!A:O,"select F, G, H, I, J, count(A) where L = 'no lo tenemos' group by F, G, H, I, J order by count(A) desc label count(A) 'Veces'",1)`],
      ['R1', 'Códigos más pedidos',
        `=QUERY(${C}!A:O,"select G, count(A) where G is not null group by G order by count(A) desc label count(A) 'Veces'",1)`],
    ];
    bloques.forEach(([celda, titulo, formula]) => {
      const rango = r.getRange(celda);
      rango.setValue(titulo).setFontWeight('bold').setFontSize(12);
      rango.offset(1, 0).setFormula(formula);
    });
  }
}


function crearHoja(ss, nombre, columnas) {
  if (ss.getSheetByName(nombre)) return;
  const h = ss.insertSheet(nombre);
  h.getRange(1, 1, 1, columnas.length).setValues([columnas]).setFontWeight('bold').setBackground('#e8eef7');
  h.setFrozenRows(1);
}


function respuesta(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}


// Para probar desde el editor: seleccioná "probar" arriba y tocá Ejecutar.
function probar() {
  preparar();
  registrarContacto({ numero: '5493510000000', nombre: 'Prueba', tipo: 'particular', pais: 'Argentina', nuevo_mensaje: true });
  registrarConsulta({ numero: '5493510000000', nombre: 'Prueba', tipo: 'particular', pieza: 'inyector',
    codigo_pedido: '0445110183', marca: 'Fiat', modelo: 'Strada', motor: '1.3 Multijet', anio: '2015',
    resultado: 'lo tenemos', codigo_ofrecido: '0445110183', precio_ofrecido: '$302.567' });
}
