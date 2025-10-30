import logging
from typing import Dict, List, Tuple
from tasas import GestorTasas

logger = logging.getLogger(__name__)

class GestorSaldos:
    """
    Gestiona los saldos por ubicación y moneda
    - Calcula saldo actual por ubicación (Ecuador, Binance, Venezuela)
    - Convierte a USD usando tasas actuales
    - Proporciona resumen del portafolio
    """

    def __init__(self, sheet, gestor_tasas: GestorTasas):
        self.sheet = sheet
        self.gestor_tasas = gestor_tasas

    def obtener_todas_transacciones(self) -> List[Dict]:
        """Obtiene todas las transacciones de Google Sheets"""
        try:
            # Usar get_all_values() para traer RAW values como strings
            todas_filas_raw = self.sheet.get_all_values()
            
            if not todas_filas_raw or len(todas_filas_raw) < 2:
                return []
            
            # Primera fila son los encabezados
            encabezados = todas_filas_raw[0]
            
            # Convertir a lista de diccionarios
            transacciones = []
            for fila in todas_filas_raw[1:]:
                if not any(fila):  # Skip empty rows
                    continue
                
                trans_dict = {}
                for i, encabezado in enumerate(encabezados):
                    valor = fila[i] if i < len(fila) else ""
                    
                    # Si es la columna Monto, normalizar la coma
                    if encabezado == "Monto" and valor:
                        valor = valor.replace(',', '.')
                    
                    trans_dict[encabezado] = valor
                
                transacciones.append(trans_dict)
            
            return transacciones
            
        except Exception as e:
            logger.error(f"Error al obtener transacciones: {e}")
            return []

    def obtener_saldo_por_ubicacion(self) -> Dict:
        """
        Calcula el saldo actual por ubicación y moneda

        NOTA: Los egresos se guardan con signo NEGATIVO en la hoja
        Por lo tanto, solo SUMAMOS todos los montos

        Ejemplo:
        Ingreso 1000 USD  → +1000
        Egreso 50 USD     → -50
        Total             → 950

        Retorna:
        {
            "Ecuador": {"USD": 1234.56},
            "Binance": {"USDT": 250.00},
            "Venezuela": {"Bs": 3650}
        }
        """
        transacciones = self.obtener_todas_transacciones()
        saldos = {}

        for trans in transacciones:
            try:
                ubicacion = trans.get("Ubicación", "").strip()
                moneda = trans.get("Moneda", "").strip()

                # Saltar filas vacías o encabezados
                if not ubicacion or not moneda:
                    continue

                # Inicializar ubicación si no existe
                if ubicacion not in saldos:
                    saldos[ubicacion] = {}

                # Inicializar moneda si no existe
                if moneda not in saldos[ubicacion]:
                    saldos[ubicacion][moneda] = 0

                # Obtener monto (ya incluye el signo negativo si es egreso)
                try:
                    # Google Sheets en español usa comas, convertir a puntos
                    monto_str = str(trans.get("Monto", 0)).replace(',', '.')
                    monto = float(monto_str)
                except (ValueError, AttributeError):
                    monto = 0

                # 🔑 SIMPLEMENTE SUMAR (el signo negativo ya está en el monto)
                saldos[ubicacion][moneda] += monto

            except Exception as e:
                logger.warning(f"Error procesando transacción: {e}")
                continue

        logger.info(f"Saldos calculados: {saldos}")
        return saldos

    def convertir_a_usd(self, monto: float, moneda: str) -> Tuple[float, float]:
        """
        Convierte cualquier moneda a USD

        Retorna: (monto_usd, tasa_usada)
        """
        if moneda.upper() in ["USD", "USDT"]:
            return monto, 1.0

        elif moneda.upper() == "BS":
            tasa = self.gestor_tasas.obtener_tasa()
            if not tasa:
                logger.warning("No hay tasa disponible para convertir Bs")
                return 0, 0
            monto_usd = monto / tasa
            return monto_usd, tasa

        else:
            logger.warning(f"Moneda desconocida: {moneda}")
            return 0, 0

    def obtener_saldo_total_usd(self) -> float:
        """
        Calcula el saldo total en USD considerando todas las ubicaciones y monedas

        Retorna: monto total en USD
        """
        saldos = self.obtener_saldo_por_ubicacion()
        total_usd = 0

        for ubicacion, monedas in saldos.items():
            for moneda, monto in monedas.items():
                monto_usd, _ = self.convertir_a_usd(monto, moneda)
                total_usd += monto_usd

        return total_usd

    def obtener_portafolio_detallado(self) -> str:
        """
        Retorna un resumen formateado del portafolio
        """
        saldos = self.obtener_saldo_por_ubicacion()
        tasa_actual = self.gestor_tasas.obtener_tasa()
        total_usd = 0

        mensaje = "💰 TU PORTAFOLIO ACTUAL\n"
        mensaje += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        for ubicacion in sorted(saldos.keys()):
            monedas = saldos[ubicacion]
            mensaje += f"\n📍 {ubicacion.upper()}\n"

            for moneda, monto in monedas.items():
                monto_usd, tasa_usada = self.convertir_a_usd(monto, moneda)

                if moneda.upper() == "BS":
                    mensaje += f"   {moneda}: {monto:.0f}\n"
                    mensaje += f"   💱 Tasa: {tasa_usada:.2f} Bs/USD\n"
                    mensaje += f"   💵 Equivalente: ${monto_usd:.2f} USD\n"
                else:
                    mensaje += f"   {moneda}: {monto:.2f}\n"
                    mensaje += f"   💵 Equivalente: ${monto_usd:.2f} USD\n"

                total_usd += monto_usd

        mensaje += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        mensaje += f"📊 SALDO TOTAL EN USD: ${total_usd:.2f}\n"

        if tasa_actual:
            mensaje += f"\n💱 Tasa BCV actual: {tasa_actual:.2f} Bs/USD"

        return mensaje

    def obtener_saldo_por_ubicacion_formateado(self, ubicacion: str) -> str:
        """
        Retorna el saldo de una ubicación específica
        """
        saldos = self.obtener_saldo_por_ubicacion()

        if ubicacion.lower() not in [u.lower() for u in saldos.keys()]:
            return f"❌ No hay registros para {ubicacion}"

        # Encontrar ubicación (case-insensitive)
        ubicacion_real = next(
            (u for u in saldos.keys() if u.lower() == ubicacion.lower()),
            ubicacion
        )

        monedas = saldos.get(ubicacion_real, {})
        total_usd = 0

        mensaje = f"💰 SALDO EN {ubicacion_real.upper()}\n"
        mensaje += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        for moneda, monto in monedas.items():
            monto_usd, tasa = self.convertir_a_usd(monto, moneda)

            if moneda.upper() == "BS":
                mensaje += f"\n{moneda}: {monto:.0f}\n"
                mensaje += f"Tasa: {tasa:.2f}\n"
                mensaje += f"💵 ${monto_usd:.2f} USD\n"
            else:
                mensaje += f"\n{moneda}: {monto:.2f}\n"
                mensaje += f"💵 ${monto_usd:.2f} USD\n"

            total_usd += monto_usd

        mensaje += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        mensaje += f"Total: ${total_usd:.2f} USD"

        return mensaje