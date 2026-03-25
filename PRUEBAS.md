# Cómo testear el módulo `ghr_compliance` (KYC + scoring + adjuntos + OCR)

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

Alternativa por consola (Docker):

```bash
docker exec odoo-docker-17-web-1 odoo -d prueba3 -u ghr_compliance --stop-after-init
```

---

## 2. Prerrequisitos a validar (antes de probar KYC)

### 2.1 Módulos Odoo
Confirmar que están instalados:

- `survey`
- `survey_upload_file` (para preguntas de adjuntos en el KYC)

### 2.2 OCR de PDF (opcional, recomendado)
Si vas a subir **PDF** para OCR, el contenedor debe tener:

- `poppler-utils`
- Python lib `pdf2image`

Si no están, el OCR de PDF mostrará error indicando que falta convertir a imágenes.

---

## 3. Configurar API Keys (solo admin)

1. Inicia sesión como **administrador**.
2. Menú **Ajustes** (engranaje).
3. En la columna izquierda, baja hasta la sección **"Compliance (PLAFT)"**.
4. Configura al menos una:
   - **OpenAI (GPT)** (preferida) o
   - **Gemini** (fallback)
5. Clic en **Guardar**.

Si no configuras ninguna clave, el módulo mostrará notificación y usará **fallback por reglas internas** donde aplique.

---

## 4. Probar el KYC (Survey) + obligatoriedad

1. Menú **Compliance** → **Evaluaciones** → **Crear**.
2. Selecciona **Cliente** y guarda.
3. Abre/lanza la encuesta KYC Persona Física (**KYC Persona Física - Debida Diligencia**).
4. Prueba avance con **Siguiente**:
   - si dejas una pregunta requerida vacía, **debe bloquear** y mostrar error.

### Excepciones (NO requeridas)
Validar que NO obligue estas (según negocio):

- `Teléfono Empresa`
- `Región Empresa`
- Sección `2.2 Referencias Comerciales y Personales` (todas)
- Sección `2.3 Clientes Principales (Independientes)` (todas)
- Sección `2.4 Proveedores Principales (Independientes)` (todas)
- `Nombre del Fideicomiso`
- `No. Pasaporte Adicional 2`

---

## 5. UI/Frontend: fondo, dropdown y ocultar “R#”

### 5.1 Fondo (patrón Debida Diligencia)
- El survey debe usar el fondo `patron_debida_diloigencia.jpg` (carpeta `Img`) y verse como background.

### 5.2 Nacionalidad como dropdown
- La pregunta **Nacionalidad** debe mostrarse como **desplegable** (no lista de radios).
- Debe contener países en orden alfabético.

### 5.3 Ocultar “R#” al usuario final
- En el frontend NO debe verse `(...)` tipo `No (R0)`, `Sí (R4)` ni países con `(R#)`.
- El scoring interno debe mantenerse (se usa `answer_score` internamente).

---

## 6. Probar OCR (adjuntos)

### 6.1 OCR desde evaluación
1. En una evaluación, sube adjuntos al **chatter**:
   - Imagen (`jpg/png/webp`) y/o
   - PDF (`application/pdf`)
2. Ejecuta la acción de OCR.
3. Verifica que se creen registros en **Análisis documental** y que el texto salga en párrafos (sin separación por página).

---

## 7. Probar dictamen (con o sin IA)

1. En una evaluación, pulsa **"Generar dictamen IA"**.
2. Abre la pestaña **"🤖 IA info"**:
   - **Con API key**: respuesta IA.
   - **Sin API key o error**: fallback por reglas internas.

---

## 8. Casos de prueba rápidos

| Qué probar              | Cómo |
|--------------------------|------|
| Survey requerido (bloqueo) | Dejar una requerida vacía y pulsar **Siguiente** → debe bloquear y mostrar error. |
| Excepciones no requeridas | Validar que las preguntas/sections listadas arriba dejan avanzar sin llenar. |
| Fondo Debida Diligencia | Abrir survey y verificar que se ve el background del patrón. |
| Ocultar `(R#)` | Revisar dropdowns y radios → NO debe verse `(R#)` en ninguna opción. |
| OCR imagen | Adjuntar JPG/PNG y ejecutar OCR → debe crear análisis con texto breve. |
| OCR PDF | Adjuntar PDF y ejecutar OCR → si Poppler/pdf2image existen, debe procesar páginas. |
| Dictamen sin API key | Quitar claves en Ajustes → generar dictamen → fallback por reglas. |
| Dictamen con API key | Configurar OpenAI o Gemini → generar dictamen → respuesta IA. |
| Solo admin ve claves | Usuario no admin no debe ver bloque de claves en Ajustes. |

---

## 9. Si algo falla

- **El módulo no aparece en Aplicaciones:** comprueba que la ruta de addons incluya `your-addons` y que la carpeta se llama `ghr_compliance`. Actualiza la lista de aplicaciones.
- **Error al actualizar el módulo:** revisa los logs con `docker logs -f odoo-docker-17-web-1` (traza de Python o XML).
- **El Survey no obliga requeridos:** confirmar `users_can_go_back=False` en el survey y que `constr_mandatory=True` en preguntas.
- **No se ve el fondo:** limpiar caché (Ctrl+F5) y confirmar `background_image` del survey.
- **OCR PDF falla:** instalar `poppler-utils` y `pdf2image`.
- **"Generar dictamen IA" da error:** revisar log; si API key inválida o sin red, debe caer a fallback.
