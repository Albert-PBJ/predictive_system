"""Comando de DEPURACIÓN del OCR de imágenes (EasyOCR / red neuronal).

Lee una o más imágenes (URL o ruta local) con la misma red neuronal que usa el
scraper de Instagram y muestra QUÉ texto reconoció, con qué confianza, y qué precio
se extraería de ahí. Sirve para responder "¿la imagen le llega bien a la red?" y para
ver si el precio (p. ej. '250$') se está leyendo o no — sin tener que correr un scrape.

Ejemplos:
    python manage.py test_image_ocr https://.../flyer.jpg
    python manage.py test_image_ocr ./flyer.jpg https://.../otro.png
"""

from django.core.management.base import BaseCommand

from apps.competitor_market_data.enrichment import image_ocr
from apps.competitor_market_data.scrapers.instagram_scraper import (
    _extract_price,
    _extract_price_from_ocr,
)


class Command(BaseCommand):
    help = "Prueba el OCR (EasyOCR) sobre una o más imágenes y muestra el texto y el precio detectados."

    def add_arguments(self, parser):
        parser.add_argument(
            "images",
            nargs="+",
            type=str,
            help="URL(s) o ruta(s) local(es) de imagen para leer con el OCR.",
        )

    def handle(self, *args, **options):
        images: list[str] = options["images"]

        # El comando es intención explícita, así que corre aunque USE_VISION_PRICE_OCR
        # esté apagado; pero sí necesita el paquete easyocr instalado.
        self.stdout.write(
            f"Config OCR → mag_ratio={image_ocr.OCR_MAG_RATIO}, idiomas={image_ocr.OCR_LANGUAGES}, "
            f"gpu={image_ocr.OCR_USE_GPU}, "
            f"asumir_USD_si_numero_desnudo={image_ocr.OCR_ASSUME_USD_FOR_BARE_NUMBER}"
        )

        if image_ocr._get_reader() is None:
            self.stderr.write(
                self.style.ERROR(
                    "EasyOCR no está disponible (¿falta 'pip install easyocr'?). "
                    "No se puede ejecutar la prueba."
                )
            )
            return

        for image_ref in images:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"Imagen: {image_ref}"))

            results = image_ocr.read_image(image_ref, detail=1)
            if not results:
                self.stderr.write(
                    self.style.WARNING("  Sin texto reconocido (o la imagen no se pudo leer).")
                )
                continue

            fragments: list[str] = []
            for entry in results:
                # detail=1 → (bbox, texto, confianza)
                try:
                    _bbox, fragment, confidence = entry
                except (ValueError, TypeError):
                    fragment, confidence = str(entry), None
                fragments.append(fragment)
                conf_txt = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "—"
                self.stdout.write(f"  [{conf_txt}] {fragment!r}")

            full_text = " ".join(fragments)
            strict_price, strict_cur = _extract_price(full_text)
            ocr_price, ocr_cur = _extract_price_from_ocr(full_text)

            self.stdout.write(f"  Texto unido: {full_text!r}")
            if strict_price is not None:
                self.stdout.write(
                    self.style.SUCCESS(f"  Precio (estricto, con moneda): {strict_price} {strict_cur}")
                )
            else:
                self.stdout.write("  Precio (estricto, con moneda): no encontrado")
            if ocr_price is not None:
                self.stdout.write(
                    self.style.SUCCESS(f"  Precio (modo OCR): {ocr_price} {ocr_cur}")
                )
            else:
                self.stdout.write(
                    "  Precio (modo OCR): no encontrado"
                    + (
                        ""
                        if image_ocr.OCR_ASSUME_USD_FOR_BARE_NUMBER
                        else " (activa OCR_ASSUME_USD_FOR_BARE_NUMBER para adivinar números desnudos)"
                    )
                )
