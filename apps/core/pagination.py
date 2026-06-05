from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Paginación por defecto de los listados de la API.

    Permite al cliente ajustar el tamaño de página vía `?page_size=` (con un tope
    razonable) para que las tablas del frontend controlen cuántas filas traen.
    """

    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100
