"""Enriquecimiento opcional de los datos scrapeados.

Contiene dos integraciones, ambas opcionales y con degradación segura (si están
deshabilitadas o falta su dependencia, el pipeline determinista funciona igual):

* `deepseek.py` — LLM (DeepSeek) que identifica/normaliza al competidor vendedor
  y limpia campos de texto a partir del anuncio. Lo usan Facebook e Instagram.
* `image_ocr.py` — OCR por red neuronal (EasyOCR) que lee el precio desde la
  imagen de un post de Instagram cuando ni el caption ni el LLM lo encontraron.
"""
