# Complience

## Generar CSV KYC desde Excel

Para usar el archivo **Formulario KYC P.F corregido.xlsx** como fuente de campos KYC, convierte el Excel al CSV del módulo con el script incluido:

1. Instala la dependencia: `pip install openpyxl`
2. Ejecuta desde la carpeta del proyecto (o con ruta absoluta al Excel):

   ```bash
   python your-addons/ghr_compliance/scripts/excel_to_kyc_csv.py "ruta/al/Formulario KYC P.F corregido.xlsx"
   ```

El script escribe en `ghr_compliance/data/kyc_pf_persona_fisica.csv`. Después puedes revisar y completar, si hace falta, las columnas "Optionset Values" o "Campo en res.partner" en ese CSV.
