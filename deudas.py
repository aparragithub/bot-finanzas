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
    LIMITE_COTIDIANA = 146.0  # 1 Cuota
    LIMITE_PRINCIPAL = 391.0  # 3+ Cuotas

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
            # Utilizar numericise_ignore=['all'] para evitar que GSheets parseé "50,65" como 5065 (miles)
            todas = self.worksheet.get_all_records(numericise_ignore=['all'])
            pendientes = [d for d in todas if str(d.get("Estado")).lower() != "pagado"]
            
            uso_cotidiana = 0
            uso_principal = 0
            
            for d in pendientes:
                tipo = str(d.get("Tipo", "Normal")).lower()
                restante = self._parse_float(d.get("Restante", 0))
                
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
                
            # Lógica de ID Secuencial
            deuda_id = self._generar_proximo_id()
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

    def registrar_pago_cuota(self, referencia: str, monto_pago: float):
        """Abonar a una deuda existente"""
        try:
            # Utilizar numericise_ignore=['all'] para evitar que GSheets parseé "50,65" como 5065
            todas = self.worksheet.get_all_records(numericise_ignore=['all'])
            candidato = None
            candidato_idx = -1
            
            referencia = referencia.lower()
            
            for i, deuda in enumerate(todas):
                deuda_id_sheet = str(deuda.get("ID", "")).lower()
                desc = str(deuda.get("Descripción", "")).lower()
                estado = str(deuda.get("Estado", "")).lower()
                
                # Check ID matching OR Description matching
                if estado != "pagado":
                    if referencia == deuda_id_sheet or referencia in desc:
                        candidato = deuda
                        candidato_idx = i
                        break
            
            if not candidato:
                return False, f"No encontré deuda pendiente con ID o Descripción '{referencia}'"

            try:
                # Usar _parse_float para leer los valores string
                pagado_anterior = self._parse_float(candidato.get("Pagado", 0))
                total = self._parse_float(candidato.get("Monto Total", 0))
                prox_venc_actual = candidato.get("Próximo Vencimiento", "")
            except:
                return False, "Error leyendo datos numéricos"

            nuevo_pagado = pagado_anterior + monto_pago
            nuevo_restante = total - nuevo_pagado
            
            nuevo_estado = "Pagado" if nuevo_restante <= 0.01 else "Pendiente"
            if nuevo_restante < 0: nuevo_restante = 0
            
            # Actualizar vencimiento y filas
            # ... (Lógica de fechas simplificada para evitar complejidad aquí)
            tipo = str(candidato.get("Tipo", "")).lower()
            nuevo_venc = prox_venc_actual
            
            # Solo actualizar fecha si no es importado
            if "importado" not in tipo and nuevo_estado == "Pendiente" and prox_venc_actual:
                 try:
                     f_venc = datetime.strptime(prox_venc_actual, "%Y-%m-%d")
                     nuevo_venc = (f_venc + timedelta(days=14)).strftime("%Y-%m-%d")
                 except: pass

            fila = candidato_idx + 2
            
            self.worksheet.update(values=[[nuevo_pagado, nuevo_restante, nuevo_estado]], range_name=f"E{fila}:G{fila}")
            self.worksheet.update(values=[[nuevo_venc]], range_name=f"I{fila}")
            
            return True, f"✅ Abonado ${monto_pago} a '{candidato['Descripción']}'. Resta: ${nuevo_restante:.2f}"

        except Exception as e:
            logger.error(f"Error registrando pago: {e}")
            return False, f"Error: {str(e)}"

    def pagar_deuda_completa(self, deuda_id: str, fecha_pago: str, tasa_cambio: float):
        """
        Paga la totalidad restante de una deuda.
        Retorna (exito, mensaje, dict_transaccion_bs)
        """
        try:
            # Refresh data
            todas = self.worksheet.get_all_records(numericise_ignore=['all'])
            candidato = None
            candidato_idx = -1
            
            # Buscar por ID Exacto (Case insensitive)
            for i, deuda in enumerate(todas):
                if str(deuda.get("ID", "")).upper() == deuda_id.upper():
                    candidato = deuda
                    candidato_idx = i
                    break
            
            if not candidato:
                return False, f"No existe deuda con ID {deuda_id}", None

            try:
                restante = self._parse_float(candidato.get("Restante", 0))
                descripcion = candidato.get("Descripción", "Deuda")
                estado = candidato.get("Estado", "")
            except:
                return False, "Error leyendo datos de la deuda", None

            # AUTO-REPAIR: Si dice pagado/restante 0 pero math dice contrario
            total_val = self._parse_float(candidato.get("Monto Total", 0))
            pagado_ant = self._parse_float(candidato.get("Pagado", 0))
            restante_calc = total_val - pagado_ant
            
            if restante <= 0.01 and restante_calc > 0.01:
                logger.warning(f"AUTO-REPAIR Deuda {deuda_id}: Restante sheet 0 vs Calc {restante_calc}. Proceeding.")
                restante = restante_calc
                estado = "Pendiente"

            if estado.lower() == "pagado" or restante <= 0.01:
                return False, f"La deuda {deuda_id} ya está registrada como PAGADA.", None

            # Calcular monto en Bs
            monto_bs = restante * tasa_cambio

            # -- Actualizar Hoja Deudas --
            # Pagado += Restante
            # Restante = 0
            # Estado = Pagado
            # pagado_ant ya lo tenemos
            nuevo_pagado = pagado_ant + restante
            
            fila = candidato_idx + 2
            # Actualizar E, F, G (Pagado, Restante, Estado) -> Cols 5, 6, 7
            self.worksheet.update(values=[[nuevo_pagado, 0, "Pagado"]], range_name=f"E{fila}:G{fila}")
            
            # -- Preparar Transaccion de Egreso (Bs) --
            transaccion = {
                "fecha": fecha_pago,
                "tipo": "Egreso",
                "categoria": "Pago de Deuda",
                "ubicacion": "Venezuela",
                "moneda": "Bs",
                "monto": monto_bs,
                "tasa_usada": tasa_cambio,
                "usd_equivalente": restante,
                "descripcion": f"Pago {deuda_id}: {descripcion}"
            }
            
            return True, f"✅ Deuda {deuda_id} pagada.\n💵 Monto: {monto_bs:,.2f} Bs (Tasa: {tasa_cambio})", transaccion

        except Exception as e:
            logger.error(f"Error pagar completa: {e}")
            return False, f"Error: {e}", None

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
            todas = self.worksheet.get_all_records(numericise_ignore=['all'])
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
                    id_deuda = d.get("ID", "S/ID")
                    msg += f"• [{id_deuda}] {d['Descripción']}{fuente_str}: **${restante:.2f}** ({venc})\n"
                
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

    def _generar_proximo_id(self):
        """Genera DEUDA-N+1 basado en los existentes"""
        try:
            ids = self.worksheet.col_values(1) # Columna ID
            max_id = 0
            for i in ids:
                if i.startswith("DEUDA-"):
                    try:
                        num = int(i.split("-")[1])
                        if num > max_id: max_id = num
                    except: pass
            return f"DEUDA-{max_id + 1}"
        except:
            return "DEUDA-1"

    def migrar_ids_legacy(self):
        """Migra IDs antiguos (timestamp) a secuenciales (DEUDA-1...)"""
        try:
            filas = self.worksheet.get_all_values()
            if not filas or len(filas) < 2: return "Sin datos"
            
            headers = filas[0]
            data = filas[1:]
            
            # Ordenar por fecha (más viejo primero para ser DEUDA-1)
            try:
                data.sort(key=lambda x: datetime.strptime(x[1], "%Y-%m-%d"))
            except: pass 
            
            updates = []
            for idx, row in enumerate(data):
                nuevo_id = f"DEUDA-{idx + 1}"
                self.worksheet.update_cell(idx + 2, 1, nuevo_id)
                updates.append(nuevo_id)
                
            return f"Migrados {len(updates)} IDs."
        except Exception as e:
            return f"Error migrando: {e}"
