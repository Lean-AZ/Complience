# Cómo testear el módulo ghr_compliance (Gemini + dictamen)

## 1. Arrancar Odoo y actualizar el módulo

```bash
cd odoo-docker-17
docker compose up -d
docker logs -f odoo-docker-17-web-1
```

Cuando Odoo esté listo (verás algo como "HTTP service (HTTP) listening on 0.0.0.0:8069"):

1. Entra en **http://localhost:8069**
2. Crea una base de datos nueva o usa una existente.
3. **Activar modo desarrollador:** Ajustes → Activar el modo de desarrollador.
4. **Actualizar el módulo:** Aplicaciones → buscar "Compliance" o "Matriz de Riesgo" → ⋮ (tres puntos) → **Actualizar** (o desde Aplicaciones → Actualizar lista de aplicaciones, luego buscar el módulo e instalar/actualizar).

---

## 2. Configurar la API Key de Gemini (solo admin)

1. Inicia sesión como **administrador**.
2. Menú **Ajustes** (engranaje).
3. En la columna izquierda, baja hasta la sección **"Compliance (PLAFT)"**.
4. En **"Dictamen con IA (Gemini)"** pega tu API Key de [Google AI Studio](https://aistudio.google.com/apikey).
5. Clic en **Guardar**.

Si no configuras la clave, el botón "Generar dictamen IA" seguirá funcionando y usará el **dictamen por reglas internas** (sin llamar a Gemini).

---

## 3. Probar el dictamen (con o sin IA)

1. Menú **Compliance** → **Evaluaciones**.
2. **Crear** una nueva evaluación:
   - **Cliente:** elige un contacto (o crea uno con nombre y país).
   - Opcional: asigna un **Perfil de umbrales** (o déjalo vacío para usar el por defecto).
   - Rellena o deja los puntajes (Origen de fondos, Actividad económica, PEP, Nacionalidad, Transacción bulto). Si tienes encuesta vinculada, pueden llenarse al marcar la encuesta como hecha.
   - **Volumen mensual (USD)** y **Cantidad de transferencias** (opcionales pero útiles para el dictamen).
3. Guarda el registro.
4. Pulsa el botón **"Generar dictamen IA"** en la barra superior.
5. Abre la pestaña **"🤖 IA info"**: ahí debe aparecer el contenido en **Resumen del Chivatazo IA**:
   - **Con API key configurada:** texto generado por Gemini (formato RIESGO DETECTADO, ANÁLISIS INTEGRAL, BÚSQUEDAS SUGERIDAS, DICTAMEN).
   - **Sin API key o si falla la llamada:** dictamen por reglas internas (o mensaje de error + dictamen por reglas).

---

## 4. Casos de prueba rápidos

| Qué probar              | Cómo |
|--------------------------|------|
| Dictamen sin API key     | Borra la clave en Ajustes → Compliance, guarda. Genera dictamen → debe salir solo el dictamen por reglas. |
| Dictamen con API key     | Pega una API key válida en Ajustes → Compliance. Genera dictamen → debe salir texto de Gemini. |
| Error de API / sin red    | Pon una clave inválida o desconecta red. Genera dictamen → debe salir aviso + dictamen por reglas. |
| Solo administrador ve clave | Entra con un usuario no admin. En Ajustes no debe verse el bloque "Compliance (PLAFT)". |

---

## 5. Si algo falla

- **El módulo no aparece en Aplicaciones:** comprueba que la ruta de addons incluya `your-addons` y que la carpeta se llama `ghr_compliance`. Actualiza la lista de aplicaciones.
- **Error al actualizar el módulo:** revisa los logs con `docker logs -f odoo-docker-17-web-1` (traza de Python o XML).
- **"Generar dictamen IA" no hace nada o da error:** mira el log de Odoo; si la API key está mal o Gemini devuelve error, verás el fallback (mensaje + dictamen por reglas).
- **Gemini no responde / timeout:** comprueba que el servidor tenga salida a internet y que la API key sea válida y con cuota en Google AI Studio.
