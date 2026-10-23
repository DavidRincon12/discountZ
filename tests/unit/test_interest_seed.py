"""Calidad de la semilla de intereses (revisión final del PR 2).

Un interés salta el perfil, el tope de la fuente y los mínimos de precio: un término ambiguo
(«portátil» también es «portable», «impermeable» es un adjetivo) manda tarjetas inútiles.
"""

from __future__ import annotations

import json

from discountz.config import load_interests, load_topics
from discountz.domain.community import assign_channel_topic
from discountz.domain.interests import match_interest
from tests.profiles import SPEC_MODES
from tests.unit.test_topic_precision import LABELED, _post

# Títulos reales de los canales y de las fixtures de tiendas que no son lo que busca el interés.
NOT_INTERESTS = (
    "LOFTILLA Mini Massage Gun: Calor, 6 Velocidades, 4 Cabezas - Portátil y Silenciosa",
    'Espejo de maquillaje portátil YEKKOPT 8", LED ajustable, batería recargable 1200mAh',
    "Botiquín de Primeros Auxilios 276 Piezas Portátil",
    "Secador de ropa portátil, secador de viaje de voltaje global, 600W",
    "Cuna portátil para bebé",
    "Bandeja Giratoria Portátil para Snacks y Frutas con Tapa",
    "Barras para flexiones portátiles EYESUN antideslizantes ergonómicas",
    "Base refrigerante para laptop con 5 ventiladores",
    "Procesador de Alimentos Oster 3 Tazas",
    "Procesador de alimentos para bebés.",
    "Manta de Playa Impermeable Secado Rápido Extra Grande",
    "Funda impermeable para colchón",
    "Yineel HC600: Kit de Afeitado y Recorte Todo en Uno, Impermeable y Con Batería",
    "Auranso Shorts Niña Algodón Bicicleta 4 Pack",
    "Bicicleta Estática Spinning con Pantalla",
    "Anti Static Wrist Strap ESD",
    "Computador de Escritorio All in One Lenovo",
    "Organizador de escritorio de bambú",
    "Mochila pañalera para bebé",
    "Diadema para el cabello con perlas",
    "Placa madre ASUS Prime B650 DDR5",
    "Placa base GIGABYTE Z890 AORUS PRO ICE, DDR5, PCIe 5.0, cinco M.2",
    "Mini PC BOSGAME E2 Plus, AMD Ryzen 5 3501U, 8GB DDR4, 256GB SSD, triple pantalla 4K",
    "Enfriador CPU Thermaltake UX400 ARGB 4 tubos cobre 120mm PWM Intel AMD negro",
    "Disipador CPU Thermalright Peerless Assassin 120 SE, 6 tubos de calor",
    "Pulsera inteligente fitness, monitor de frecuencia cardíaca, sueño y estrés 24/7",
    "Pantalla de Monitor Selfie Magnética para Creadores: Control Remoto",
    "Cargador Rápido 20W USB-C 3 Pack iPhone iPad",
    "Funda magnética para iPhone 17 Pro Max",
    "Estabilizador de gimbal 3 ejes DJI OM 5 para smartphone, varilla extensible integrada",
    "Estación de Carga 3 en 1 Anlmz: Carga Rápida para iPhone, Apple Watch y AirPods",
    "Ríe y Aprende Audífonos Juega Conmigo - Fisher Price",
    "Dell ALIENWARE M16 R2 Intel Core Ultra 7 155H CPU 155H RTX 4070 Motherboard",
)

# Títulos que sí son lo que busca el interés, con el interés que deben sacar.
INTERESTS = {
    "Portátil HP 15-fd0010la Intel Core i5 8GB 512GB SSD": "Portátil",
    "Portátil Lenovo IdeaPad Slim 3 AMD Ryzen 5 16GB": "Portátil",
    "Laptop ASUS Vivobook 15 OLED": "Portátil",
    "Batería portátil 30.000 mAh carga rápida": "Power bank",
    "Parlante Portátil JBL Flip 6": "Parlante",
    "Procesador AMD Ryzen 7 7800X3D": "Procesador",
    "Escritorio de pie ajustable Sweetcrispy 1.2m x 0.6m": "Escritorio",
    "Amseatec Criss Cross Office Chair for $115 + free shipping": "Silla de oficina",
    "Chaqueta impermeable Columbia para hombre": "Chaqueta impermeable",
    "Memoria RAM Kingston Fury 16GB DDR5": "Memoria RAM",
    "Sony WH-1000XM5 Wireless Noise Canceling Headphone": "Audífonos",
    "Física Universitaria Sears Zemansky Volumen 1": "Libros de ingeniería",
    "Rocketbook Spiral Notebook, Executive Size": "Cuadernos",  # no «Portátil»
    "Smartwatch KEEPONFIT para mujer, IP68, monitor de frecuencia cardíaca": "Smartwatch",
    "Memoria RAM para portátil HP 16GB DDR4 SODIMM": "Memoria RAM",
    "Celular REDMI Note 17 Pro 256 5G Negro + Power Bank 165W": "Celular",
    "Parlante Bluedee SK800 10W RGB jack auriculares USB": "Parlante",  # no «Audífonos»
    "Juego de destornilladores de precisión para reparación de celulares": (
        "Destornilladores de precisión"
    ),
}


def test_seed_skips_known_false_positives() -> None:
    seeds = load_interests().seeds
    wrong = [(t, hit.name) for t in NOT_INTERESTS if (hit := match_interest(t, seeds))]
    assert wrong == []


def test_seed_matches_what_it_is_for() -> None:
    seeds = load_interests().seeds
    got = {t: hit.name if (hit := match_interest(t, seeds)) else None for t in INTERESTS}
    assert got == INTERESTS


def test_seed_rescues_nothing_from_off_topics() -> None:
    """De las 946 ofertas etiquetadas, ninguna de un tema apagado sale por un interés."""
    seeds, rules = load_interests().seeds, load_topics().channel_rules
    rows = [json.loads(line) for line in LABELED.read_text(encoding="utf-8").splitlines()]
    rescued = [
        (row["titulo"], hit.name)
        for row in rows
        if SPEC_MODES.get(assign_channel_topic(_post(row), rules)) == "off"
        and (hit := match_interest(str(row["titulo"]), seeds))
    ]
    assert rescued == []
