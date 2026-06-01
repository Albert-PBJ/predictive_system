"""Enriquecimiento opcional de los datos scrapeados mediante un LLM.

Hoy contiene la integración con DeepSeek (`deepseek.py`), usada por el scraper
de Facebook Marketplace para identificar/normalizar al competidor vendedor a
partir del texto del anuncio. Todo el paquete es opcional: si el enriquecimiento
está deshabilitado, el pipeline determinista funciona igual.
"""
