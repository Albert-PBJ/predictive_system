"""Carga datos de ejemplo para probar los módulos de ventas e inventario.

Crea categorías, productos (con stock y precios), clientes, una tasa de cambio
del día y vendedores ligados a usuarios (incluido el admin, para poder registrar
ventas de inmediato). Es **idempotente**: se puede correr varias veces sin
duplicar (usa get_or_create / update_or_create por clave natural).

Uso:
    python manage.py seed_demo_data
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from apps.accounts.models import Role
from apps.core.models import Category, Customer, ExchangeRate, Product, Seller

# (nombre de categoría, [productos...])  — datos representativos del mercado
# venezolano de mobiliario de oficina. Algunos productos quedan deliberadamente
# en stock bajo para que se vea la alerta de reabastecimiento en la UI.
CATEGORIES = ["Sillas de Oficina", "Sillas de Visita", "Escritorios", "Archivadores", "Mesas de Reunión"]

# sku, nombre, categoría, material, compra USD, venta USD, stock, stock mínimo
PRODUCTS = [
    ("OK-6611N", "Silla Ejecutiva Stanford", "Sillas de Oficina", "MESH", 75, 140, 12, 4),
    ("OK-3302", "Silla Operativa Boston", "Sillas de Oficina", "MESH", 45, 85, 25, 6),
    ("OK-9001G", "Silla Gerencial Ginebra", "Sillas de Oficina", "BIPIEL", 95, 185, 2, 4),
    ("VIS-1201", "Silla de Visita Madrid", "Sillas de Visita", "FABRIC", 30, 60, 18, 5),
    ("VIS-0450", "Silla Apilable Caracas", "Sillas de Visita", "METAL", 22, 45, 40, 10),
    ("ESC-2300", "Escritorio Ejecutivo Lima", "Escritorios", "WOOD", 120, 230, 7, 3),
    ("ESC-3200L", "Escritorio en L Toronto", "Escritorios", "WOOD", 160, 320, 4, 2),
    ("ESC-1650", "Escritorio Secretarial Quito", "Escritorios", "WOOD", 85, 165, 6, 3),
    ("ARC-4G", "Archivador Metálico 4 Gavetas", "Archivadores", "METAL", 90, 175, 9, 3),
    ("ARC-3M", "Archivador Móvil 3 Gavetas", "Archivadores", "METAL", 55, 110, 1, 3),
    ("MES-8OV", "Mesa de Reunión Oval 8 Puestos", "Mesas de Reunión", "WOOD", 210, 420, 3, 1),
    ("MES-AUX", "Mesa Auxiliar Cúcuta", "Mesas de Reunión", "WOOD", 40, 80, 14, 4),
]

# rif, razón social, tipo, sector, estado, municipio, contacto, teléfono, email
CUSTOMERS = [
    ("J-12345678-9", "Corporación Andina C.A.", "CORP", "Manufactura", "Carabobo", "Valencia",
     "Pedro", "González", "0241-8001122", "compras@corpandina.com"),
    ("J-29876543-1", "Inversiones El Sol S.A.", "CORP", "Comercio", "Distrito Capital", "Libertador",
     "Ana", "Martínez", "0212-5550099", "administracion@elsol.com"),
    ("G-20000123-4", "Alcaldía de Naguanagua", "INST", "Gobierno", "Carabobo", "Naguanagua",
     "Luis", "Hernández", "0241-8675309", "proveeduria@naguanagua.gob.ve"),
    ("V-15678234-0", "María Pérez", "IND", "", "Carabobo", "Valencia",
     "María", "Pérez", "0414-1234567", "maria.perez@gmail.com"),
    ("J-31122334-5", "Clínica Santa Ana C.A.", "CORP", "Salud", "Aragua", "Girardot",
     "Carmen", "Rojas", "0243-2461010", "compras@clinicasantaana.com"),
]

# username, password, nombre, apellido, comisión %  (el primero se liga al admin)
DEMO_SELLER = ("vendedor1", "Vendedor2026!", "Carlos", "Rivas", Decimal("8.00"))


class Command(BaseCommand):
    help = "Carga datos de ejemplo (productos, clientes, tasa, vendedores) para ventas e inventario."

    @transaction.atomic
    def handle(self, *args, **options):
        cats = {}
        for name in CATEGORIES:
            cat, _ = Category.objects.get_or_create(
                name=name, defaults={"slug": slugify(name)}
            )
            cats[name] = cat
        self.stdout.write(self.style.SUCCESS(f"Categorías: {len(cats)}"))

        created_products = 0
        for sku, name, cat_name, material, buy, sell, stock, min_stock in PRODUCTS:
            _, created = Product.objects.get_or_create(
                sku=sku,
                defaults={
                    "name": name,
                    "full_name": name,
                    "category": cats[cat_name],
                    "material": material,
                    "purchase_price_usd": Decimal(buy),
                    "sale_price_usd": Decimal(sell),
                    "stock": stock,
                    "min_stock": min_stock,
                    "is_active": True,
                },
            )
            created_products += int(created)
        self.stdout.write(self.style.SUCCESS(
            f"Productos: {created_products} nuevos (de {len(PRODUCTS)})."
        ))

        created_customers = 0
        for rif, company, ctype, sector, state, municipality, fn, ln, phone, email in CUSTOMERS:
            _, created = Customer.objects.get_or_create(
                rif=rif,
                defaults={
                    "company_name": company,
                    "customer_type": ctype,
                    "sector": sector,
                    "state": state,
                    "municipality": municipality,
                    "contact_first_name": fn,
                    "contact_last_name": ln,
                    "phone": phone,
                    "email": email,
                    "is_active_customer": True,
                },
            )
            created_customers += int(created)
        self.stdout.write(self.style.SUCCESS(
            f"Clientes: {created_customers} nuevos (de {len(CUSTOMERS)})."
        ))

        rate, _ = ExchangeRate.objects.update_or_create(
            date=date.today(),
            defaults={
                "bcv_rate": Decimal("36.5000"),
                "parallel_rate": Decimal("40.0000"),
                "source": ExchangeRate.SourceChoices.BCV,
            },
        )
        self.stdout.write(self.style.SUCCESS(
            f"Tasa de cambio {rate.date}: BCV {rate.bcv_rate} | Paralela {rate.parallel_rate}"
        ))

        # Vendedor ligado al admin: permite registrar ventas como el usuario admin
        # sin tener que crear otro usuario primero.
        admin = User.objects.filter(is_superuser=True).order_by("id").first()
        if admin:
            Seller.objects.get_or_create(
                user=admin,
                defaults={
                    "first_name": admin.first_name or "Admin",
                    "last_name": admin.last_name or "Maescar",
                    "email": admin.email or "admin@maescar.com",
                    "commission_rate": Decimal("10.00"),
                },
            )
            self.stdout.write(self.style.SUCCESS(
                f"Vendedor ligado al admin '{admin.username}'."
            ))

        # Usuario vendedor de demostración (rol SELLER) con su perfil de Seller,
        # para probar el acceso por rol.
        username, password, fn, ln, commission = DEMO_SELLER
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": "carlos.rivas@maescar.com"},
        )
        if created:
            user.set_password(password)
            user.save()
        profile = user.profile  # creado por signal
        profile.role = Role.SELLER
        profile.first_name = fn
        profile.last_name = ln
        profile.email = "carlos.rivas@maescar.com"
        profile.save()
        Seller.objects.get_or_create(
            user=user,
            defaults={
                "first_name": fn,
                "last_name": ln,
                "email": "carlos.rivas@maescar.com",
                "commission_rate": commission,
            },
        )
        creds = f" (contraseña: {password})" if created else " (ya existía)"
        self.stdout.write(self.style.SUCCESS(
            f"Vendedor de demo '{username}'{creds}."
        ))

        self.stdout.write(self.style.MIGRATE_HEADING("\nDatos de ejemplo cargados correctamente."))
