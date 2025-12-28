import logging
import gspread
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class GestorDeudas:
    """
    Gestiona la hoja de "Deudas" y la lógica de CASHEA.
    """
    
    NOMBRE_HOJA = "Deudas"
    ENCABEZADOS = ["ID", "Fecha", "Descripción", "Monto Total", "Pagado", "Restante", "Estado", "Tipo", "Próximo Vencimiento"]

    # Límites de Crédito (Configurables)
    LIMITE_COTIDIANA = 150.0  # 1 Cuota
    LIMITE_PRINCIPAL = 400.0  # 3+ Cuotas

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
                self.worksheet.update('A1:I1', [self.ENCABEZADOS])
                logger.info(f"Hoja '{self.NOMBRE_HOJA}' creada")
        except Exception as e:
            logger.error(f"Error inicializando hoja de deudas: {e}")

    def obtener_credito_disponible(self):
        """Calcula cuánto crédito queda disponible en ambas líneas"""
        try:
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

    def crear_deuda(self, descripcion: str, monto_total: float, monto_inicial: float, tipo: str = "Normal", fecha_compra: str = None, proximo_vencimiento: str = None):
        """Registra una nueva deuda con soporte para tipos (Cashea/Normal) y vencimiento manual"""
        try:
            if not fecha_compra:
                fecha_compra = datetime.now().strftime("%Y-%m-%d")
                
            deuda_id = f"DEUDA-{int(datetime.now().timestamp())}"
            restante = monto_total - monto_inicial
            estado = "Pendiente" if restante > 0.01 else "Pagado"

            # Calcular próx vencimiento (14 días si es Cashea)
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
                prox_venc
            ]
            
            self.worksheet.append_row(row, table_range="A1")
            logger.info(f"Deuda creada: {descripcion} ({tipo}) - Restante: {restante}")
            return deuda_id, restante

        except Exception as e:
            logger.error(f"Error creando deuda: {e}")
            return None, 0

    def registrar_pago_cuota(self, referencia: str, monto_pago: float):
        """Abonar a una deuda existente"""
        try:
            todas = self.worksheet.get_all_records()
            candidato = None
            candidato_idx = -1
            
            referencia = referencia.lower()
            
            for i, deuda in enumerate(todas):
                desc = str(deuda.get("Descripción", "")).lower()
                estado = str(deuda.get("Estado", "")).lower()
                if estado != "pagado" and referencia in desc:
                    candidato = deuda
                    candidato_idx = i
                    break
            
            if not candidato:
                return False, f"No encontré deuda pendiente con '{referencia}'"

            try:
                pagado_anterior = float(candidato.get("Pagado", 0))
                total = float(candidato.get("Monto Total", 0))
                prox_venc_actual = candidato.get("Próximo Vencimiento", "")
            except:
                return False, "Error leyendo datos numéricos"

            nuevo_pagado = pagado_anterior + monto_pago
            nuevo_restante = total - nuevo_pagado
            
            nuevo_estado = "Pagado" if nuevo_restante <= 0.01 else "Pendiente"
            if nuevo_restante < 0: nuevo_restante = 0
            
            # Actualizar vencimiento si es Cashea y sigue pendiente
            nuevo_venc = prox_venc_actual
            if nuevo_estado == "Pendiente" and prox_venc_actual:
                 # Sumar 14 días al vencimiento actual
                 try:
                     f_venc = datetime.strptime(prox_venc_actual, "%Y-%m-%d")
                     nuevo_venc = (f_venc + timedelta(days=14)).strftime("%Y-%m-%d")
                 except:
                     pass

            # Fila en hoja (1-based + header)
            fila = candidato_idx + 2
            
            # Actualizar E(Pagado), F(Restante), G(Estado), I(Prox Venc)
            # A=1, B=2, C=3, D=4, E=5, F=6, G=7, H=8, I=9
            self.worksheet.update(values=[[nuevo_pagado, nuevo_restante, nuevo_estado]], range_name=f"E{fila}:G{fila}")
            self.worksheet.update(values=[[nuevo_venc]], range_name=f"I{fila}") # Actualizar vencimiento separadamente
            
            return True, f"✅ Abonado ${monto_pago} a '{candidato['Descripción']}'. Resta: ${nuevo_restante:.2f}\n📅 Prox Venc: {nuevo_venc}"

        except Exception as e:
            logger.error(f"Error registrando pago: {e}")
            return False, f"Error: {str(e)}"

    def obtener_resumen(self):
        """Retorna resumen de deudas, líneas de crédito y custodias"""
        try:
            creditos = self.obtener_credito_disponible()
            todas = self.worksheet.get_all_records()
            pendientes = [d for d in todas if str(d.get("Estado")).lower() != "pagado"]
            
            msg = "💳 **ESTADO DE CRÉDITO (CASHEA)**\n"
            msg += f"• **Principal:** Disp ${creditos['principal']['disponible']:.2f} / ${self.LIMITE_PRINCIPAL}\n"
            msg += f"• **Cotidiana:** Disp ${creditos['cotidiana']['disponible']:.2f} / ${self.LIMITE_COTIDIANA}\n\n"
            
            if not pendientes:
                msg += "✅ **No tienes deudas pendientes.**"
                return msg
                
            deudas_reales = []
            custodias = []
            
            for d in pendientes:
                tipo = str(d.get("Tipo", "")).lower()
                if "custodia" in tipo:
                    custodias.append(d)
                else:
                    deudas_reales.append(d)

            total_deuda = 0
            if deudas_reales:
                msg += "📉 **DEUDAS POR PAGAR**\n"
                for d in deudas_reales:
                    restante = float(d.get("Restante", 0))
                    total_deuda += restante
                    venc = d.get("Próximo Vencimiento", "N/A")
                    msg += f"• {d['Descripción']}: **${restante:.2f}** (Vence: {venc})\n"
                msg += f"💰 **Total Deuda:** ${total_deuda:.2f}\n"

            if custodias:
                total_custodia = sum(float(c.get("Restante", 0)) for c in custodias)
                msg += "\n🔐 **FONDOS EN CUSTODIA (Terceros)**\n"
                for c in custodias:
                    msg += f"• {c['Descripción']}: ${float(c.get('Restante', 0)):.2f}\n"
                msg += f"🏦 **Total Custodia:** ${total_custodia:.2f}\n"
            
            return msg
            
        except Exception as e:
            return f"Error leyendo deudas: {e}"
