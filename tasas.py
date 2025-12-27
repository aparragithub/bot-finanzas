import requests
import logging

logger = logging.getLogger(__name__)

class GestorTasas:
    """
    Gestiona las tasas de cambio Bs/USD
    - Obtiene automáticamente de API (Aves Data)
    - Permite override manual
    - Cachea el valor para evitar múltiples requests
    """

    def __init__(self):
        self.tasa_manual = None
        self.ultima_tasa_api = None
        self.ultima_tasa_usada = None

    def obtener_tasa_bcv_api(self):
        """
        Obtiene la tasa BCV de Aves Data API
        Retorna: float (tasa) o None si falla
        """
        try:
            response = requests.get(
                "https://api.exchangerate-api.com/v4/latest/USD",
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            # Obtener tasa VES (Bolívar Venezolano)
            tasa = data.get("rates", {}).get("VES")

            if tasa:
                self.ultima_tasa_api = tasa
                logger.info(f"Tasa BCV obtenida de API: {tasa}")
                return tasa
            else:
                logger.warning("No se encontró tasa en respuesta de API")
                return None

        except requests.exceptions.Timeout:
            logger.error("Timeout al conectar con API de tasas")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error al obtener tasa de API: {e}")
            return None
        except Exception as e:
            logger.error(f"Error inesperado en obtener_tasa_bcv_api: {e}")
            return None

    def establecer_tasa_manual(self, tasa: float):
        """
        Establece un override manual de la tasa
        Esto prevalece sobre la tasa de API
        """
        try:
            tasa = float(tasa)
            if tasa > 0:
                self.tasa_manual = tasa
                self.ultima_tasa_usada = tasa
                logger.info(f"Tasa manual establecida: {tasa}")
                return True
            else:
                logger.warning(f"Tasa inválida (debe ser positiva): {tasa}")
                return False
        except ValueError:
            logger.warning(f"No se pudo convertir a float: {tasa}")
            return False

    def obtener_tasa(self):
        """
        Obtiene la tasa actual con este orden de prioridad:
        1. Tasa manual (si está configurada)
        2. Tasa de API
        3. Última tasa usada
        4. None (si no hay ninguna)
        """
        # Si hay tasa manual, usarla
        if self.tasa_manual:
            self.ultima_tasa_usada = self.tasa_manual
            return self.tasa_manual

        # Intentar obtener de API
        tasa_api = self.obtener_tasa_bcv_api()
        if tasa_api:
            self.ultima_tasa_usada = tasa_api
            return tasa_api

        # Si falla, retornar última conocida
        if self.ultima_tasa_usada:
            logger.warning(f"Usando última tasa conocida: {self.ultima_tasa_usada}")
            return self.ultima_tasa_usada

        logger.error("No hay tasa disponible")
        return None

    def limpiar_manual(self):
        """Limpia el override manual para usar API nuevamente"""
        self.tasa_manual = None
        logger.info("Override manual limpiado")

    def obtener_info(self):
        """Retorna información detallada de la tasa"""
        tasa_actual = self.obtener_tasa()

        info = {
            "tasa_actual": tasa_actual,
            "es_manual": bool(self.tasa_manual),
            "tasa_manual": self.tasa_manual,
            "ultima_api": self.ultima_tasa_api,
            "ultima_usada": self.ultima_tasa_usada
        }

        return info

    def obtener_tasa_historica(self, fecha_str: str):
        """
        Obtiene la tasa histórica para una fecha dada (YYYY-MM-DD).
        Si cae feriado/fin de semana, busca hacia atrás hasta encontrar.
        API: api.dolarvzla.com
        """
        import datetime
        
        try:
            target_date = datetime.datetime.strptime(fecha_str, "%Y-%m-%d")
        except ValueError:
            logger.error(f"Fecha inválida para histórico: {fecha_str}")
            return None

        # Intentar buscar hasta 5 días atrás
        for i in range(5):
            fecha_query = (target_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            
            try:
                url = f"https://api.dolarvzla.com/public/exchange-rate/list?from={fecha_query}&to={fecha_query}"
                response = requests.get(url, timeout=5)
                
                if response.status_code == 200:
                    data = response.json()
                    item = data.get('rates', [])
                    if item and len(item) > 0:
                        tasa = float(item[0]['usd'])
                        logger.info(f"Tasa histórica encontrada para {fecha_query}: {tasa} (Original: {fecha_str})")
                        return tasa
            except Exception as e:
                logger.error(f"Error api historica: {e}")
                
        logger.warning(f"No se encontró tasa histórica cerca de {fecha_str}")
        return None