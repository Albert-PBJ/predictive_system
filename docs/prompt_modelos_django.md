# Prompt para generar modelos de Django — Sistema de Análisis Predictivo Maescar

## Contexto del proyecto

Estoy desarrollando un **Sistema de Análisis Predictivo basado en Big Data como Herramienta de Benchmarking Inteligente** para **Inversiones Maescar C.A.**, una empresa fabricante y comercializadora de mobiliario de oficina ubicada en Los Guayos, Estado Carabobo, Venezuela.

El stack es: **Django (backend) + React (frontend) + PostgreSQL + scikit-learn/XGBoost + Apify (web scraping)**.

El sistema debe:
1. **Centralizar la operación diaria**: ventas, inventario, costos, clientes, presupuestos.
2. **Recolectar datos de competidores** vía web scraping (precios, productos, promociones).
3. **Generar predicciones con IA**: proyectar demanda, detectar patrones estacionales, estimar tendencias de precios.
4. **Hacer benchmarking inteligente**: comparar indicadores propios vs competencia.
5. **Visualizar en un dashboard**: KPIs, alertas, reportes exportables.
6. **Gestionar usuarios y roles**: gerencia, vendedores, almacén, administración.

---

## Datos reales de la empresa (estructura actual en Excel)

La empresa actualmente lleva TODO en hojas de Excel. A continuación la estructura real de sus datos para que los modelos reflejen exactamente lo que manejan:

### Cuadro de Ventas (registros de venta individuales)
```
Fecha | Cliente | Producto | Cantidad (implícita en descripción, ej: "16 Sillas Eames negro") | Precio de Venta (USD) | Precio de Compra (USD) | Ganancia (USD) | Vendedor
```
Ejemplo: `2022-01-10 | Yumary Torres | 1 Stanford | $140 venta | $125 compra | $15 ganancia | Mariangel Escobar`

### Cuadro de Ventas Mensual (resumen consolidado)
```
Fecha | Cliente | Precio Venta Total | Precio Compra Total | Utilidad | Vendedor | Comisiones Ventas (10% de la utilidad)
```

### Lista de Precios / Catálogo de Productos (PDF actual)
Los productos tienen: nombre comercial (ej: "Stanford", "Manhattan", "BE"), código/SKU (ej: "OK-6611N", "OCO22N"), precio en USD, categoría implícita (Sillería Presidencial, Ejecutiva, Visitante, Gamer, Escritorios, Tandem/Espera, Archivadores, Bibliotecas), material (Malla Mesh, Bipiel, Tela), colores disponibles, y medidas físicas (asiento largo/ancho, espaldar largo/ancho, altura min/max).

Rango de precios: desde $40 (sillas de espera) hasta $315 (silla premium Lexus) en sillas; desde $95 hasta $255 en escritorios/mesas.

### Cuadro de Ajuste de Precios
```
Item | Modelo | Precio Compra $ | Precio Compra Bs | Precio Venta $ | Precio Venta Bs | Utilidad Bs | Utilidad $ | Tasa BCV | Tasa Paralela | Cantidad | Total $ Compra | Total $ Venta | Total Gastos | Total Utilidad
```
Nota importante: en Venezuela manejan **doble moneda** (USD y Bolívares/VES) con tasa BCV oficial y tasa paralela. El sistema DEBE soportar esto.

### Formato de Presupuesto
```
Cliente (nombre, RIF, dirección, teléfono) | Fecha emisión | Fecha vencimiento | Tasa BCV | Instalación (sí/no) | Despacho | Nro Presupuesto | Líneas: (Descripción, Cantidad, Precio $, Precio Bs, Total $, Total Bs) | Subtotal | IVA 16% | Total General
```

### Clientes y Prospectos
```
RIF | Nombre empresa | Sector industrial | Nombre contacto | Apellidos contacto | CI representante | Teléfono | Celular | Email | Estado | Municipio | Parroquia | Dirección fiscal | Total trabajadores
```

### Precios Históricos (2018)
```
Descripción | Precio Compra $ | Tasa del Día | Precio Compra Bs | Precio Venta $ | Precio Venta Bs
```
Esto demuestra que la empresa tiene datos históricos de precios desde al menos 2018, lo cual es valioso para los modelos predictivos.

---

## Modelos existentes en Django (a actualizar/expandir)

### App `products` (o similar) — models.py:
```python
class Product(models.Model):
    class Meta:
        db_table = "products"
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.IntegerField()
```

### App de datos de competencia — models.py:
```python
class CompetitorMarketData(models.Model):
    class SourceChoices(models.TextChoices):
        INSTAGRAM = "IG", _("Instagram")
        FACEBOOK = "FB", _("Facebook Marketplace")
        WEBSITE = "WEB", _("Página Web Directa")
        OTHER = "OTH", _("Otra Fuente")

    competitor_name = models.CharField(null=True, blank=True, max_length=150)
    source = models.CharField(max_length=3, choices=SourceChoices.choices, default=SourceChoices.WEBSITE)
    url = models.URLField(null=True, blank=True, max_length=500)
    product_name = models.CharField(null=True, blank=True, max_length=255)
    category = models.CharField(null=True, blank=True, max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(null=True, blank=True, max_length=3, default="USD")
    lead_time_days = models.IntegerField(null=True, blank=True)
    is_in_stock = models.BooleanField(null=True, blank=True, default=True)
    promotions = models.CharField(max_length=255, null=True, blank=True)
    raw_metadata = models.JSONField(null=True, blank=True)
    scraped_at = models.DateTimeField(auto_now_add=True)
```

---

## Tu tarea

Genera/actualiza los modelos de Django para cubrir **todas** las entidades necesarias del sistema. Organiza en las apps de Django que consideres apropiadas. A continuación las entidades que necesito:

### 1. `Product` (ACTUALIZAR — actualmente muy básico)
Debe reflejar el catálogo real de Maescar:
- SKU/código (ej: "OK-6611N"), nombre comercial (ej: "Stanford"), nombre completo
- **Categoría** (Sillería Presidencial, Ejecutiva, Secretarial, Visitante, Gamer, Cajero, Escritorios, Mesas de Conferencia, Tandem/Espera, Archivadores, Bibliotecas, Módulos de Recepción, Accesorios)
- Material (Malla Mesh, Bipiel, Tela, Metal, Madera/Melamina)
- Colores disponibles (puede ser JSONField o una M2M)
- Medidas físicas (largo asiento, ancho asiento, largo espaldar, ancho espaldar, altura min, altura max — en cm; para escritorios: largo, ancho, alto)
- Precio de compra USD, precio de venta USD (los precios actuales)
- Stock actual
- Stock mínimo (umbral para alertas de reabastecimiento)
- Indicador de si es producto fabricado por Maescar o importado/revendido
- Imagen (URL o ImageField)
- Activo/inactivo
- Timestamps

### 2. `ProductPriceHistory` (NUEVO)
Para alimentar los modelos predictivos. Cada vez que cambie un precio, se registra:
- FK a Product
- Precio compra USD, precio venta USD
- Precio compra Bs, precio venta Bs
- Tasa BCV del momento, tasa paralela del momento
- Fecha del cambio
- Motivo (opcional: ajuste de tasa, cambio de proveedor, promoción, etc.)

### 3. `Category` (NUEVO)
Para normalizar las categorías de producto en vez de un CharField libre.

### 4. `Customer` (NUEVO)
Basado en el formato real de clientes de Maescar:
- RIF (cédula fiscal venezolana, sirve como identificador único)
- Razón social / nombre empresa
- Tipo: institucional, empresarial, particular
- Sector industrial
- Nombre y apellido del contacto/representante, CI
- Teléfono, celular, email
- Ubicación: estado, municipio, parroquia, dirección fiscal
- Total de trabajadores (útil para segmentar prospectos por tamaño de empresa)
- Es cliente activo o solo prospecto
- Timestamps

### 5. `Seller` (NUEVO)
- FK a User de Django (o datos propios si prefieres)
- Nombre, teléfono, email
- Porcentaje de comisión (por defecto 10% de la utilidad, según los datos reales)
- Activo/inactivo

### 6. `Sale` y `SaleItem` (NUEVO — ventas)
Sale (encabezado):
- FK a Customer, FK a Seller
- Fecha de la venta
- Tipo de venta: detal, proyecto institucional
- Total venta USD, total venta Bs
- Total costo USD
- Utilidad total
- Comisión generada
- Tasa BCV al momento, tasa paralela al momento
- Estado (pendiente, completada, anulada)
- Notas/observaciones

SaleItem (detalle por línea):
- FK a Sale, FK a Product
- Cantidad
- Precio unitario venta USD
- Precio unitario compra/costo USD
- Subtotal venta, subtotal costo
- Utilidad de la línea

### 7. `Quote` y `QuoteItem` (NUEVO — presupuestos)
Quote (encabezado):
- Número de presupuesto (formato tipo "08052026-8")
- FK a Customer
- FK a Seller (quien lo emitió)
- Fecha emisión, fecha vencimiento
- Tasa BCV, tasa paralela
- Incluye instalación (bool), incluye despacho (bool)
- Subtotal USD, subtotal Bs
- IVA (16%), total general USD, total general Bs
- Estado: borrador, enviado, aprobado, rechazado, convertido_a_venta
- FK a Sale (si se convirtió en venta, nullable)

QuoteItem (líneas):
- FK a Quote, FK a Product
- Cantidad, precio unitario USD, precio unitario Bs
- Total línea USD, total línea Bs

### 8. `InventoryMovement` (NUEVO)
Para registrar toda entrada y salida de almacén:
- FK a Product
- Tipo: entrada (compra/reposición), salida (venta), ajuste, devolución
- Cantidad
- Referencia (FK a Sale si es salida por venta, o texto libre para compras)
- Responsable (FK a User)
- Fecha
- Notas

### 9. `ExchangeRate` (NUEVO)
Para registrar la tasa de cambio diaria, fundamental en Venezuela:
- Fecha
- Tasa BCV (oficial)
- Tasa paralela (referencial)
- Fuente (BCV, Monitor Dólar, etc.)
- Auto-timestamp

### 10. `CompetitorMarketData` (REVISAR el existente)
El modelo existente está bien estructurado. Sugerencias de mejora:
- Agregar un campo `competitor` como FK a un modelo `Competitor` en vez de solo `competitor_name` como CharField (para normalizar)
- Mantener `competitor_name` como fallback por si el scraper trae un nombre nuevo no registrado

### 11. `Competitor` (NUEVO)
- Nombre de la empresa competidora
- Ubicación (estado, municipio)
- Sitio web, Instagram, Facebook
- Notas
- Activo/inactivo

### 12. `Alert` (NUEVO)
Para el sistema de alertas automáticas:
- Tipo: quiebre_stock, sobrestock, cambio_precio_competidor, caida_demanda, meta_cumplida
- Severidad: info, warning, critical
- Título, mensaje descriptivo
- FK a Product (nullable), FK a Competitor (nullable)
- Leída (bool)
- Resuelta (bool)
- Fecha de creación

### 13. `PredictionLog` (NUEVO — repositorio de modelos y métricas)
Para versionar los modelos de IA y sus métricas:
- Nombre del modelo (ej: "demand_forecast_xgboost_v3")
- Tipo: demand_forecast, price_trend, seasonal_pattern, competitor_benchmark
- Métricas: R², RMSE, MAE (como JSONField o campos individuales)
- Parámetros/hiperparámetros (JSONField)
- Fecha de entrenamiento
- Dataset usado (descripción o referencia)
- Ruta al archivo del modelo serializado (FileField o CharField con path)
- Activo (bool) — para marcar cuál es el modelo en producción

### 14. `KPI` (NUEVO)
Para almacenar indicadores calculados periódicamente para el dashboard:
- Nombre del KPI (ej: "rotacion_inventario_mensual", "margen_promedio", "indice_competitividad")
- Valor numérico
- Unidad (%, USD, días, índice)
- Período (mes/año)
- Categoría del KPI: financiero, inventario, ventas, competencia
- Fecha de cálculo
- Metadata adicional (JSONField)

---

## Instrucciones técnicas

- Usa `django.db.models` estándar. No uses librerías externas para los modelos.
- Agrega `help_text` descriptivos en español para cada campo importante.
- Agrega `class Meta` con `db_table`, `verbose_name`, `verbose_name_plural`, `ordering` e `indexes` donde sea pertinente para rendimiento (especialmente en tablas que se consultarán con Pandas para Big Data).
- Agrega métodos `__str__` descriptivos.
- Usa `DecimalField` para todos los montos monetarios (nunca FloatField).
- Los campos de moneda siempre deben ir en pares USD/Bs donde aplique.
- Agrega `created_at` y `updated_at` en todos los modelos que lo ameriten.
- Respeta las convenciones de Django: ForeignKey con `on_delete` explícito, `related_name` descriptivo.
- Usa `TextChoices` / `IntegerChoices` para los campos con opciones fijas.
- Organiza los modelos en apps lógicas. Sugiero: `core` (Product, Category, Customer, Seller, ExchangeRate), `sales` (Sale, SaleItem, Quote, QuoteItem), `inventory` (InventoryMovement), `benchmarking` (Competitor, CompetitorMarketData), `analytics` (PredictionLog, KPI, Alert). Pero puedes proponer otra organización si te parece mejor.
- Genera también las migraciones iniciales si es posible.
- NO generes vistas, serializers ni URLs; solo los modelos.
