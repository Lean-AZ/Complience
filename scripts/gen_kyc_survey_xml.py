# -*- coding: utf-8 -*-
"""Genera data/survey_kyc_pf_data.xml desde data/kyc_pf_persona_fisica.csv"""
import csv
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CSV_PATH = os.path.join(DATA_DIR, "kyc_pf_persona_fisica.csv")
OUT_PATH = os.path.join(DATA_DIR, "survey_kyc_pf_data.xml")

def esc(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:50]
    return s or "q"

def qtype(tipo, opts, field_name=None):
    """Devuelve (question_type, choices). choices = lista de (value, score) para simple_choice; [] para otros."""
    t = (tipo or "").strip().lower()
    name = (field_name or "").strip().lower()
    # Nacionalidad: dropdown de países con score
    if "many2one" in t and "res.country" in t and name == "nacionalidad":
        return "simple_choice", COUNTRIES_FOR_NACIONALIDAD
    if t == "boolean":
        bool_scores = SCORING_BY_QUESTION.get(name)
        if bool_scores and len(bool_scores) == 2:
            return "simple_choice", [("No", bool_scores[0]), ("Sí", bool_scores[1])]
        return "simple_choice", [("No", 0), ("Sí", 1)]
    if t == "selection" and opts:
        opts_list = [x.strip() for x in opts.split("|") if x.strip()]
        scores = _scores_for_title(name, opts_list)
        if scores is not None:
            return "simple_choice", list(zip(opts_list, scores))
        return "simple_choice", [(o, 0) for o in opts_list]
    if t == "date":
        return "date", []
    if t in ("integer", "monetary"):
        return "numerical_box", []
    if t == "text":
        return "text_box", []
    return "char_box", []


# Lista de países para dropdown Nacionalidad: (nombre en español, score riesgo). DO=1, otros comunes 3-5, Otro=6.
COUNTRIES_FOR_NACIONALIDAD = [
    ("República Dominicana", 1),
    ("Estados Unidos", 3),
    ("España", 3),
    ("México", 3),
    ("Colombia", 3),
    ("Argentina", 3),
    ("Chile", 3),
    ("Perú", 3),
    ("Ecuador", 3),
    ("Cuba", 5),
    ("Puerto Rico", 3),
    ("Haití", 5),
    ("Jamaica", 4),
    ("Trinidad y Tobago", 4),
    ("Panamá", 4),
    ("Costa Rica", 3),
    ("Honduras", 4),
    ("El Salvador", 4),
    ("Guatemala", 4),
    ("Nicaragua", 4),
    ("Brasil", 3),
    ("Uruguay", 3),
    ("Paraguay", 3),
    ("Bolivia", 3),
    ("Canadá", 3),
    ("Reino Unido", 3),
    ("Francia", 3),
    ("Alemania", 3),
    ("Italia", 3),
    ("Portugal", 3),
    ("China", 4),
    ("India", 4),
    ("Rusia", 5),
    ("Japón", 3),
    ("Corea del Sur", 3),
    ("Israel", 4),
    ("Suiza", 3),
    ("Países Bajos", 3),
    ("Bélgica", 3),
    ("Venezuela", 5),
    ("Sudáfrica", 4),
    ("Nigeria", 5),
    ("Egipto", 4),
    ("Marruecos", 4),
    ("Turquía", 4),
    ("Arabia Saudita", 4),
    ("Emiratos Árabes Unidos", 4),
    ("Filipinas", 4),
    ("Indonesia", 4),
    ("Tailandia", 3),
    ("Vietnam", 4),
    ("Australia", 3),
    ("Nueva Zelanda", 3),
    ("Otro", 6),
]

# Puntuación por pregunta (título en minúsculas) -> lista de answer_score por opción en orden.
SCORING_BY_QUESTION = {
    "origen de los ingresos": [1, 2, 4, 6],
    "estado civil": [1, 1, 2, 2, 2],
    "sector (asalariado)": [1, 2],
    "sexo": [1, 1],
    "tipo de documento de identidad": [1, 3],
    "condición ee.uu.": [3, 5],
    "formulario fatca": [2, 5],
    "¿es o ha sido pep o figura pública?": [0, 10],
    "¿tiene vínculo con pep o figura pública?": [0, 8],
    "tipo de vinculación con pep": [8, 7, 6, 7, 6, 5],
    "tipo de adquisición": [1, 4, 3],
    "volumen estimado de depósitos/pagos al mes": [1, 5],
    "canal transaccional": [2, 4],
    "rango monto mensual esperado (dop)": [1, 2, 3, 5, 7, 9],
    "tipo principal de transacciones": [3, 2, 1],
    "¿realiza transferencias internacionales?": [0, 5],
    "¿posee cuentas en ee.uu.?": [0, 4],
    "¿cuenta con poder de un us person?": [0, 5],
    "¿recibe/envía transferencias desde/hacia ee.uu.?": [0, 5],
    "otras nacionalidades o residencias": [0, 4],
    "¿residente o ciudadano ee.uu.?": [0, 6],
    "región empresa": [1, 1, 1, 2],
    "tipo de cuenta (ref. bancaria 1)": [1, 1, 2],
    "moneda (ref. bancaria 1)": [1, 1, 2, 2],
}


def _scores_for_title(title, options_list):
    """Si la pregunta tiene scoring definido, devuelve lista de scores; si no, None."""
    if not title:
        return None
    key = title.strip().lower()
    scores = SCORING_BY_QUESTION.get(key)
    if scores is None or len(scores) != len(options_list):
        return None
    return scores


rows = []
with open(CSV_PATH, encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

sections = []
current = None
for r in rows:
    sec = r.get("Sección", "").strip()
    nombre = r.get("Nombre del Campo", "").strip()
    tecnico = r.get("Nombre Técnico del Campo", "").strip()
    tipo = r.get("Tipo de Campo", "").strip()
    opts = r.get("Optionset Values", "").strip()
    if sec and not nombre and not tecnico:
        if current:
            sections.append(current)
        current = {"title": sec, "fields": []}
    elif nombre or tecnico:
        if not current:
            current = {"title": "KYC", "fields": []}
        current["fields"].append({
            "nombre": nombre,
            "tecnico": tecnico or slug(nombre),
            "tipo": tipo or "Char",
            "opts": opts,
        })
if current:
    sections.append(current)

buf = []
buf.append('<?xml version="1.0" encoding="utf-8"?>')
buf.append('<odoo>')
buf.append('  <data noupdate="0">')
buf.append('    <record id="survey_kyc_pf" model="survey.survey">')
buf.append('      <field name="title">KYC Persona Física - Debida Diligencia</field>')
buf.append('      <field name="survey_type">assessment</field>')
buf.append('      <field name="access_mode">public</field>')
buf.append('      <field name="users_can_go_back" eval="True"/>')
buf.append('      <field name="questions_layout">page_per_section</field>')
buf.append('      <field name="description" type="html"><p>Formulario de debida diligencia para adquirentes e inversionistas. Las respuestas alimentan la matriz de riesgo.</p></field>')
buf.append('    </record>')

seq = 1
for si, sec in enumerate(sections):
    page_id = "survey_kyc_pf_p%d" % (si + 1)
    safe = re.sub(r"[^a-z0-9]", "_", sec["title"].lower())[:40].strip("_") or ("s%d" % si)
    buf.append('')
    buf.append('    <!-- %s -->' % esc(sec["title"]))
    buf.append('    <record id="%s" model="survey.question">' % page_id)
    buf.append('      <field name="title">%s</field>' % esc(sec["title"]))
    buf.append('      <field name="survey_id" ref="survey_kyc_pf"/>')
    buf.append('      <field name="sequence">%d</field>' % seq)
    buf.append('      <field name="question_type" eval="False"/>')
    buf.append('      <field name="is_page" eval="True"/>')
    buf.append('    </record>')
    seq += 1
    for fi, fd in enumerate(sec["fields"]):
        qid = "survey_kyc_pf_p%d_q%d" % (si + 1, fi + 1)
        qt, choices = qtype(fd["tipo"], fd["opts"], fd["nombre"])
        buf.append('    <record id="%s" model="survey.question">' % qid)
        buf.append('      <field name="survey_id" ref="survey_kyc_pf"/>')
        buf.append('      <field name="sequence">%d</field>' % seq)
        buf.append('      <field name="title">%s</field>' % esc(fd["nombre"]))
        buf.append('      <field name="question_type">%s</field>' % qt)
        buf.append('      <field name="constr_mandatory" eval="False"/>')
        buf.append('    </record>')
        seq += 1
        for ci, choice in enumerate(choices):
            if isinstance(choice, (list, tuple)) and len(choice) >= 2:
                val, score = choice[0], choice[1]
            else:
                val, score = choice, 0
            buf.append('    <record id="%s_sug%d" model="survey.question.answer">' % (qid, ci + 1))
            buf.append('      <field name="question_id" ref="%s"/>' % qid)
            buf.append('      <field name="sequence">%d</field>' % (ci + 1))
            buf.append('      <field name="value">%s</field>' % esc(val))
            buf.append('      <field name="answer_score" eval="%d"/>' % score)
            buf.append('    </record>')

buf.append("  </data>")
buf.append("</odoo>")

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(buf))
print("Written", OUT_PATH)
