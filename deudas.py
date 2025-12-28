import logging
import gspread
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class GestorDeudas:
    """
    Gestiona la hoja de "Deudas" y la lógica de CASHEA.
    """
    
    NOMBRE_HOJA = "Deudas"
    # Actualizamos encabezados con Fuente
    ENCABEZADOS = ["ID", "Fecha", "Descripción", "Monto Total", "Pagado", "Restante", "Estado", "Tipo", "Próximo Vencimiento", "Fuente"]

    # Límites de Crédito (Configurables)
    LIMITE_COTIDIANA = 150.0  # 1 Cuota
    LIMITE_PRINCIPAL = 400.0  # 3+ Cuotas

    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self._inicializar_hoja()

    def _inicializar_hoja(self):
        """Crea la hoja si no existe y asegura encabezados correctos"""
        try:
            try:
                self.worksheet = self.spreadsheet.worksheet(self.NOMBRE_HOJA)
                # Verificar si faltan columnas (Headers fix)
                current_headers = self.worksheet.row_values(1)
                if len(current_headers) < len(self.ENCABEZADOS):
                    logger.info("Actualizando encabezados faltantes...")
                    self.worksheet.update('A1:J1', [self.ENCABEZADOS])
            except gspread.WorksheetNotFound:
                self.worksheet = self.spreadsheet.add_worksheet(title=self.NOMBRE_HOJA, rows=100, cols=11)
                self.worksheet.update('A1:J1', [self.ENCABEZADOS])
                logger.info(f"Hoja '{self.NOMBRE_HOJA}' creada")
        except Exception as e:
            logger.error(f"Error inicializando hoja de deudas: {e}")

    def obtener_credito_disponible(self):
        """Calcula cuánto crédito queda disponible en ambas líneas"""
        try:
            # Re-leer hoja para tener datos frescos
            self.worksheet = self.spreadsheet.worksheet(self.NOMBRE_HOJA)
            todas = self.worksheet.get_all_records()
            pendientes = [d for d in todas if str(d.get("Estado")).lower() != "pagado"]
            
            uso_cotidiana = 0
            uso_principal = 0
            
            for d in pendientes:
                tipo = str(d.get("Tipo", "Normal")).lower()
                restante = float(d.get("Restante", 0))
                
                if "cotidiana" in tipo:
                    uso_cotidiana += restante
                elif "cashea" in tipo or "principal" in tipo:
                    uso_principal += restante
                    
            disp_cotidiana = max(0, self.LIMITE_COTIDIANA - uso_cotidiana)
            disp_principal = max(0, self.LIMITE_PRINCIPAL - uso_principal)
            
            return {
                "cotidiana": {"limite": self.LIMITE_COTIDIANA, "usado": uso_cotidiana, "disponible": disp_cotidiana},
                "principal": {"limite": self.LIMITE_PRINCIPAL, "usado": uso_principal, "disponible": disp_principal}
            }
        except Exception as e:
            logger.error(f"Error calculando crédito: {e}")
            return None

    def simular_compra_cashea(self, monto_compra: float, linea: str = "principal"):
        """
        Calcula la inicial requerida basada en el crédito disponible.
        Regla: Lo financiado (Total - Inicial) NO puede superar el Disponible.
        """
        creditos = self.obtener_credito_disponible()
        if not creditos:
            return None

        linea = linea.lower()
        if "cotidiana" in linea:
            datos_linea = creditos["cotidiana"]
            min_inicial_pct = 0.40 # 40%
        else:
            datos_linea = creditos["principal"]
            min_inicial_pct = 0.40 # 40%

        disponible = datos_linea["disponible"]
        
        # 1. Cálculo Base (40% inicial)
        inicial_base = monto_compra * min_inicial_pct
        financiamiento_base = monto_compra - inicial_base
        
        # 2. Ajuste por Límite
        nueva_inicial = inicial_base
        
        if financiamiento_base > disponible:
            # El crédito no alcanza, hay que subir la inicial
            exceso = financiamiento_base - disponible
            nueva_inicial += exceso
            financiamiento_real = disponible
            mensaje_alerta = f"⚠️ Crédito limitado. Inicial ajustada de ${inicial_base:.2f} a ${nueva_inicial:.2f}"
            es_ajustado = True
        else:
            financiamiento_real = financiamiento_base
            mensaje_alerta = "✅ Crédito suficiente."
            es_ajustado = False
            
        return {
            "monto_compra": monto_compra,
            "inicial_a_pagar": nueva_inicial,
            "monto_financiar": financiamiento_real,
            "disponible_antes": disponible,
            "es_ajustado": es_ajustado,
            "mensaje": mensaje_alerta
        }

    def crear_deuda(self, descripcion: str, monto_total: float, monto_inicial: float, tipo: str = "Normal", fecha_compra: str = None, proximo_vencimiento: str = None, fuente: str = "Binance"):
        """Registra una nueva deuda (single row)"""
        try:
            if not fecha_compra:
                fecha_compra = datetime.now().strftime("%Y-%m-%d")
                
            deuda_id = f"DEUDA-{int(datetime.now().timestamp() * 1000)}" # ID único ms
            restante = monto_total - monto_inicial
            estado = "Pendiente" if restante > 0.01 else "Pagado"

            # Calcular próx vencimiento (14 días si es Cashea y no se da manual)
            if proximo_vencimiento:
                prox_venc = proximo_vencimiento
            elif "cashea" in tipo.lower() or "cotidiana" in tipo.lower():
                fecha_dt = datetime.strptime(fecha_compra, "%Y-%m-%d")
                prox_venc = (fecha_dt + timedelta(days=14)).strftime("%Y-%m-%d")
            else:
                prox_venc = ""

            row = [
                deuda_id,
                fecha_compra,
                descripcion,
                monto_total,
                monto_inicial,
                restante,
                estado,
                tipo,
                prox_venc,
                fuente
            ]
            
            # Insertar al inicio (fila 2) para respetar el formato de Tabla en GSheets
            self.worksheet.insert_row(row, index=2)
            logger.info(f"Deuda creada: {descripcion}")
            return deuda_id, restante

        except Exception as e:
            logger.error(f"Error creando deuda: {e}")
            return None, 0

    def crear_plan_cuotas(self, descripcion: str, monto_cuota: float, num_cuotas: int, fecha_inicio: str, linea: str, fuente: str = "Binance"):
        """
        Crea MÚLTIPLES deudas separadas, una por cada cuota pendiente.
        Intervalo: 14 días entre cada una.
        """
        try:
            fecha_base = datetime.strptime(fecha_inicio, "%Y-%m-%d")
            total_creada = 0
            
            for i in range(num_cuotas):
                fecha_venc = (fecha_base + timedelta(days=14 * i)).strftime("%Y-%m-%d")
                num_actual = i + 1
                
                # Descripción única: "Monitor (Cuota 1/3)"
                desc_cuota = f"{descripcion} (Cuota {num_actual}/{num_cuotas})"
                
                # Para la hoja de deudas, cada cuota es una "Micro deuda"
                # Monto Total = Monto Cuota
                # Inicial = 0 (Porque es lo que falta por pagar)
                # Restante = Monto Cuota
                self.crear_deuda(
                    descripcion=desc_cuota,
                    monto_total=monto_cuota, # Cada row es 1 cuota
                    monto_inicial=0,
                    tipo=f"Cashea ({linea}) - Importado",
                    proximo_vencimiento=fecha_venc,
                    fuente=fuente
                )
                total_creada += monto_cuota
                
            return True, f"Se crearon {num_cuotas} cuotas totalizando ${total_creada:.2f}"
            
        except Exception as e:
            return False, f"Error creando plan: {e}"

    def _parse_float(self, val):
        """Parsea valores numéricos manejando puntos y comas"""
        try:
            if isinstance(val, (float, int)):
                return float(val)
            # Reemplazar coma por punto y limpiar
            clean_val = str(val).replace(',', '.').strip()
            # Remover caracteres no numéricos excepto punto (ej: $)
            clean_val = "".join(c for c in clean_val if c.isdigit() or c == '.')
            return float(clean_val)
        except:
            return 0.0

    def obtener_resumen(self, tasa_local: float = None):
        """Retorna resumen visual, opcionalmente con contravalor en Bs"""
        try:
            self.worksheet = self.spreadsheet.worksheet(self.NOMBRE_HOJA)
            creditos = self.obtener_credito_disponible()
            todas = self.worksheet.get_all_records()
            pendientes = [d for d in todas if str(d.get("Estado")).lower() != "pagado"]
            
            msg = "💳 **ESTADO DE CRÉDITO**\n"
            msg += f"• Principal: ${creditos['principal']['disponible']:.2f}\n"
            msg += f"• Cotidiana: ${creditos['cotidiana']['disponible']:.2f}\n\n"
            
            if not pendientes:
                return msg + "✅ **Sin deudas.**"

            custodias = [d for d in pendientes if "custodia" in str(d.get("Tipo", "")).lower()]
            deudas = [d for d in pendientes if d not in custodias]

            if deudas:
                msg += "📉 **POR PAGAR**\n"
                total = 0
                for d in deudas:
                    val_str = d.get("Restante", 0)
                    restante = self._parse_float(val_str)
                    total += restante
                    venc = d.get("Próximo Vencimiento", "N/A")
                    fuente = d.get("Fuente", "")
                    fuente_str = f" ({fuente})" if fuente else ""
                    msg += f"• {d['Descripción']}{fuente_str}: **${restante:.2f}** ({venc})\n"
                
                msg += f"💰 **Total USD:** ${total:.2f}\n"
                if tasa_local and tasa_local > 0:
                    total_bs = total * tasa_local
                    msg += f"🇻🇪 **Total Bs:** Bs. {total_bs:,.2f} (Tasa: {tasa_local})\n"

            if custodias:
                msg += "\n🔐 **CUSTODIA**\n"
                for c in custodias:
                    val_str = c.get("Restante", 0)
                    val = self._parse_float(val_str)
                    msg += f"• {c['Descripción']}: ${val:.2f}\n"
            
            return msg
        except Exception as e:
            logger.error(f"Error resumen: {e}")
            return f"Error: {e}"
