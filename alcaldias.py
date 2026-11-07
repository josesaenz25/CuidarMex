import random

# Punto central aproximado de cada alcaldía (latitud, longitud)
ALCALDIAS = {
    "Álvaro Obregón": (19.3587, -99.2037),
    "Azcapotzalco": (19.4869, -99.1840),
    "Benito Juárez": (19.3727, -99.1590),
    "Coyoacán": (19.3467, -99.1617),
    "Cuajimalpa de Morelos": (19.3557, -99.2990),
    "Cuauhtémoc": (19.4450, -99.1460),
    "Gustavo A. Madero": (19.4827, -99.1130),
    "Iztacalco": (19.3950, -99.0970),
    "Iztapalapa": (19.3550, -99.0620),
    "La Magdalena Contreras": (19.3200, -99.2400),
    "Miguel Hidalgo": (19.4290, -99.2000),
    "Milpa Alta": (19.1920, -99.0230),
    "Tláhuac": (19.2870, -99.0050),
    "Tlalpan": (19.2900, -99.1670),
    "Venustiano Carranza": (19.4260, -99.1050),
    "Xochimilco": (19.2570, -99.1030),
}


def coordenadas(alcaldia: str, dispersion: float = 0.012):
    """Devuelve (lat, lng) cerca del centro de la alcaldía, o (None, None)."""
    centro = ALCALDIAS.get(alcaldia)
    if centro is None:
        return None, None

    return (
        round(centro[0] + random.uniform(-dispersion, dispersion), 6),
        round(centro[1] + random.uniform(-dispersion, dispersion), 6),
    )