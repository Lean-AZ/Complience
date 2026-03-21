#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script para probar la API de OpenAI (GPT) desde tu PC.
NO pongas la clave en este archivo. Úsala por variable de entorno o como argumento.

Uso:
  set OPENAI_API_KEY=sk-tu-clave-aqui
  python test_openai_api.py

  o:

  python test_openai_api.py sk-tu-clave-aqui
"""
import os
import sys
import urllib.request
import urllib.error
import json

def test_openai(api_key):
    api_key = (api_key or "").strip()
    if not api_key or not api_key.startswith("sk-"):
        print("ERROR: Necesitas una API key de OpenAI que empiece por sk-")
        print("Uso: set OPENAI_API_KEY=sk-...  y luego  python test_openai_api.py")
        print("  o:  python test_openai_api.py sk-...")
        return False

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Di hola en una palabra."}],
        "max_tokens": 20,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print("OK - La API respondio:", text.strip() or "(vacío)")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print("ERROR HTTP %s: %s" % (e.code, e.reason))
        print(body)
        return False
    except Exception as e:
        print("ERROR:", e)
        return False


if __name__ == "__main__":
    key = os.environ.get("OPENAI_API_KEY") or (sys.argv[1] if len(sys.argv) > 1 else None)
    ok = test_openai(key)
    sys.exit(0 if ok else 1)
