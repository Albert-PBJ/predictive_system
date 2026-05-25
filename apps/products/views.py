from rest_framework import viewsets

from apps.core.models import Product

from .serializer import ProductSerializer


# Create your views here.


class ProductViewset(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
