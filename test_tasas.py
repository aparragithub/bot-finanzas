from tasas import GestorTasas
import logging

logging.basicConfig(level=logging.INFO)

g = GestorTasas()
print("Testing historical rate for 2025-12-22...")
rate = g.obtener_tasa_historica("2025-12-22")
print(f"Rate: {rate}")
