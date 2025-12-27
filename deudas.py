import logging
import gspread
from datetime import datetime

logger = logging.getLogger(__name__)

class GestorDeudas:
    """
    Gestiona la hoja de "Deudas" para compras a crédito.
    """
    
    NOMBRE_HOJA = "Deudas"
    ENCABEZADOS = ["ID", "Fecha", "Descripción", "Monto Total", "Pagado", "Restante", "Estado"]

    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self._inicializar_hoja()

    def _inicializar_hoja(self):
        """Crea la hoja si no existe y pone encabezados"""
        try:
            try:
                self.worksheet = self.spreadsheet.worksheet(self.NOMBRE_HOJA)
            except gspread.WorksheetNotFound:
                self.worksheet = self.spreadsheet.add_worksheet(title=self.NOMBRE_HOJA, rows=100, cols=10)
                self.worksheet.update('A1:G1', [self.ENCABEZADOS])
                logger.info(f"Hoja '{self.NOMBRE_HOJA}' creada")
        except Exception as e:
            logger.error(f"Error inicializando hoja de deudas: {e}")

    def crear_deuda(self, descripcion: str, monto_total: float, monto_inicial: float, moneda: str = "USD"):
        """
        Registra una nueva deuda.
        NOTA: Asumimos que todo se lleva internamente en USD para simplificar.
        Si la deuda es en BS, se debería convertir antes de llegar aquí.
        """
        try:
            fecha = datetime.now().strftime("%Y-%m-%d")
            deuda_id = f"DEUDA-{int(datetime.now().timestamp())}"
            
            restante = monto_total - monto_inicial
            estado = "Pendiente" if restante > 0.01 else "Pagado"

            row = [
                deuda_id,
                fecha,
                descripcion,
                monto_total,
                monto_inicial,
                restante,
                estado
            ]
            
            # Usar append_row con table_range por si acaso, aunque es hoja nueva
            self.worksheet.append_row(row, table_range="A1")
            logger.info(f"Deuda creada: {descripcion} - Restante: {restante}")
            return deuda_id, restante

        except Exception as e:
            logger.error(f"Error creando deuda: {e}")
            return None, 0

    def registrar_pago_cuota(self, referencia: str, monto_pago: float):
        """
        Busca una deuda por coincidencia parcial en descripción y abona el pago.
        """
        try:
            # Obtener todas las deudas
            todas = self.worksheet.get_all_records()
            
            # Buscar la mejor coincidencia
            candidato = None
            candidato_idx = -1 # 0-based en la lista, pero sheet es 1-based + 1 header
            
            referencia = referencia.lower()
            
            for i, deuda in enumerate(todas):
                desc = str(deuda.get("Descripción", "")).lower()
                estado = str(deuda.get("Estado", "")).lower()
                
                # Solo buscar en deudas pendientes
                if estado != "pagado" and referencia in desc:
                    candidato = deuda
                    candidato_idx = i
                    break
            
            if not candidato:
                return False, f"No encontré deuda pendiente que coincida con '{referencia}'"

            # Calcular nuevos valores
            # Importante: get_all_records devuelve números o strings dependiendo de gspread
            try:
                pagado_anterior = float(candidato.get("Pagado", 0))
                total = float(candidato.get("Monto Total", 0))
            except:
                return False, "Error al leer montos de la deuda"

            nuevo_pagado = pagado_anterior + monto_pago
            nuevo_restante = total - nuevo_pagado
            
            nuevo_estado = "Pagado" if nuevo_restante <= 0.01 else "Pendiente"
            if nuevo_restante < 0:
                nuevo_restante = 0 # No permitir negativo visualmente
            
            # Actualizar en Sheets
            # Fila real = índice lista (0-based) + 2 (header + 1-based adjustment)
            fila_sheet = candidato_idx + 2
            
            # Actualizar col E (Pagado), F (Restante), G (Estado)
            # Columnas: A=1, B=2, C=3, D=4, E=5, F=6, G=7
            
            updates = [
                # range, value
                (f"E{fila_sheet}", nuevo_pagado),
                (f"F{fila_sheet}", nuevo_restante),
                (f"G{fila_sheet}", nuevo_estado)
            ]
            
            # Hacer update por batch o celda a celda
            self.worksheet.update(values=[[nuevo_pagado, nuevo_restante, nuevo_estado]], range_name=f"E{fila_sheet}:G{fila_sheet}")
            
            logger.info(f"Pago registrado para '{candidato['Descripción']}'. Restante: {nuevo_restante}")
            return True, f"Abonado ${monto_pago} a '{candidato['Descripción']}'. Resta: ${nuevo_restante:.2f}"

        except Exception as e:
            logger.error(f"Error registrando pago de deuda: {e}")
            return False, f"Error interno: {str(e)}"

    def obtener_resumen(self):
        """Retorna texto con deudas pendientes"""
        try:
            todas = self.worksheet.get_all_records()
            pendientes = [d for d in todas if str(d.get("Estado")).lower() != "pagado"]
            
            if not pendientes:
                return "✅ No tienes deudas activas."
                
            msg = "📉 DEUDAS PENDIENTES\n"
            msg += "━━━━━━━━━━━━━━━━━━━\n"
            total_deuda = 0
            
            for d in pendientes:
                restante = float(d.get("Restante", 0))
                total_deuda += restante
                msg += f"• {d['Descripción']}: ${restante:.2f}\n"
                
            msg += "\n"
            msg += f"💰 TOTAL POR PAGAR: ${total_deuda:.2f}"
            return msg
            
        except Exception as e:
            return f"Error leyendo deudas: {e}"
