# GHR Compliance: Matriz de Riesgo Inteligente (Odoo 17)

Este módulo implementa un flujo de **Debida Diligencia / KYC** basado en **Survey** (encuestas) y una **evaluación de cumplimiento** (`compliance.assessment`) con:

- **Encuesta KYC** pública (Persona Física) con preguntas y scoring interno.
- **Matriz / semáforo de riesgo** (score total 0–5) alimentado por las respuestas.
- **Adjuntos** (documentos KYC) desde el Survey y/o desde el chatter de la evaluación.
- **OCR / análisis documental** desde adjuntos (imágenes y PDF) usando **OpenAI (GPT)** o **Google Gemini**.
- **Gestión de documentos pendientes** (subir después + recordatorios).

> Nota: El usuario final **no ve** el resultado de aprobado/reprobado ni el scoring; el scoring sigue funcionando internamente.

## Prerrequisitos (antes de instalar `ghr_compliance`)

### Módulos Odoo requeridos
En `__manifest__.py` este módulo depende de:

- `survey`
- `survey_upload_file` (**clave** para preguntas tipo “subir archivo” en la encuesta)
- además de: `base`, `base_setup`, `mail`, `contacts`

Si `survey_upload_file` no está instalado, el Survey no podrá manejar correctamente preguntas de adjuntos y el flujo de documentos KYC se rompe.

### Requisitos para OCR (en Docker / servidor)
El OCR del módulo funciona así:

- **Imágenes** (`jpg/png/webp/gif`): se envían directamente a la API (OpenAI o Gemini).
- **PDF**: se convierten a imágenes por página usando `pdf2image`, que requiere **Poppler** en el sistema.

Por tanto, para OCR con PDF necesitas:

1) Paquetes del sistema (dentro del contenedor `web`):

- `poppler-utils` (provee `pdftoppm`, requerido por `pdf2image`)

2) Librerías Python:

- `pdf2image`

> El módulo intenta usar `POPPLER_PATH` (por defecto `/usr/bin`) si tu entorno requiere un path explícito.

## Configuración (API Keys y costos OCR)

El OCR usa **OpenAI** si hay API Key configurada; si no, usa **Gemini**.

Configura en Odoo:

- **Ajustes → Compliance**:
  - `ghr_compliance.openai_api_key` (preferida)
  - `ghr_compliance.gemini_api_key` (fallback)
  - `ghr_compliance.ocr_price_per_1k_tokens_usd` (opcional, para costeo de llamadas GPT)

## Uso rápido

### 1) Actualizar/instalar el módulo
Ejemplo (Docker compose):

```bash
docker exec odoo-docker-17-web-1 odoo -d prueba3 -u ghr_compliance --stop-after-init
```

### 2) Adjuntos en Survey (KYC)
Este módulo usa `survey_upload_file`, por lo que el Survey permite preguntas de tipo “upload_file”.

- Los adjuntos se asocian a la participación/flujo del Survey.
- Al finalizar, el módulo consolida/relaciona información y adjuntos con la evaluación (`compliance.assessment`) según el flujo configurado.

### 3) OCR desde la evaluación
En la evaluación de cumplimiento (`compliance.assessment`):

- Sube documentos al **chatter** (imágenes o PDF).
- Ejecuta la acción **OCR / extraer** (según el botón/acción disponible).
- Se crean registros en `compliance.document.analysis` con:
  - diagnóstico breve (2–3 oraciones),
  - tokens/costo (si es GPT),
  - y consolidación para el dictamen.

## Instalar OCR (Poppler + pdf2image) en Docker

### A) Instalar Poppler en el contenedor
Dentro del contenedor `web`:

```bash
docker exec -it odoo-docker-17-web-1 bash
apt-get update
apt-get install -y poppler-utils
```

### B) Instalar `pdf2image` (Python)
Según tu imagen de Docker, puedes:

- instalar con pip dentro del contenedor (rápido para pruebas):

```bash
docker exec -it odoo-docker-17-web-1 bash -lc "pip3 install pdf2image"
```

> Recomendación: si quieres que quede permanente, agrégalo al `Dockerfile`/build de la imagen.

## Generar CSV KYC desde Excel

Para usar el archivo **Formulario KYC P.F corregido.xlsx** como fuente de campos KYC, convierte el Excel al CSV del módulo con el script incluido:

1. Instala la dependencia: `pip install openpyxl`
2. Ejecuta desde la carpeta del proyecto (o con ruta absoluta al Excel):

   ```bash
   python your-addons/ghr_compliance/scripts/excel_to_kyc_csv.py "ruta/al/Formulario KYC P.F corregido.xlsx"
   ```

El script escribe en `ghr_compliance/data/kyc_pf_persona_fisica.csv`. Después puedes revisar y completar, si hace falta, las columnas "Optionset Values" o "Campo en res.partner" en ese CSV.
