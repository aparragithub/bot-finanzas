import os
import logging
from datetime import datetime, timedelta
import base64
import json
import requests
import google.generativeai as genai
import re

# En Railway, las variables vienen directamente de os.getenv()
# No necesitamos load_dotenv()
try:
    from dotenv import load_dotenv
    load_dotenv()  # Para desarrollo local
except:
    pass


from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials

# Importar módulos locales
from tasas import GestorTasas
from saldos import GestorSaldos
from deudas import GestorDeudas
# from cuentas import GestorCuentas  <-- REMOVIDO
from prompts import SYSTEM_PROMPT
import keep_alive

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 🔑 DECODIFICAR CREDENCIALES DE RAILWAY
if os.getenv('GOOGLE_CREDENTIALS_B64'):
    try:
        creds_b64 = os.getenv('GOOGLE_CREDENTIALS_B64')
        creds_json = base64.b64decode(creds_b64).decode('utf-8')
        with open('google_credentials.json', 'w') as f:
            f.write(creds_json)
        logger.info("Credenciales de Google decodificadas")
    except Exception as e:
        logger.error(f"Error decodificando credenciales: {e}")


TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

groq_client = Groq(api_key=GROQ_API_KEY)

# Configurar Gemini si hay key disponible
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("✅ Gemini API configurada (será usada para Vision)")
else:
    logger.warning("⚠️ GEMINI_API_KEY no encontrada, usando Groq Vision (menos preciso)")

gestor_tasas = GestorTasas()  # Instancia global
gestor_deudas = None # Se inicializa al conectar con Sheets
gestor_saldos = None # Se inicializa al conectar

def normalize_input(text: str) -> str:
    """Normaliza el input para mejorar compatibilidad sin acentos"""
    normalized = text.lower()
    replacements = {
        'cambie ': 'cambié ', 'cambie,': 'cambié,', 'cambie.': 'cambié.',
        'gaste ': 'gasté ', 'gaste,': 'gasté,', 'gaste.': 'gasté.',
        'cobre ': 'cobré ', 'cobre,': 'cobré,', 'cobre.': 'cobré.',
        'compre ': 'compré ', 'compre,': 'compré,', 'compre.': 'compré.',
        'pague ': 'pagué ', 'pague,': 'pague.',
        'ves ': 'bs ', 'ves,': 'bs,', 'ves.': 'bs.', # Normalizar VES
    }
    for key, value in replacements.items():
        normalized = normalized.replace(key, value)
    
    # Asegurar mapeo global de ves a bs incluso sin espacios
    normalized = normalized.replace(' ves ', ' bs ')
    
    return normalized

def get_google_sheets_client():
    """Obtiene el cliente de Google Sheets"""
    try:
        credentials = Credentials.from_service_account_file(
            'google_credentials.json',
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
        )
        gc = gspread.authorize(credentials)
        return gc
    except Exception as e:
        logger.error(f"Error al conectar Google Sheets: {e}")
        raise

def get_or_create_spreadsheet():
    """Obtiene o crea la hoja de cálculo de finanzas personales"""
    try:
        gc = get_google_sheets_client()
        try:
            spreadsheet = gc.open("Finanzas Personales V2 - Bot")
        except gspread.SpreadsheetNotFound:
            logger.info("Hoja no encontrada. Intentando crear...")
            spreadsheet = gc.create("Finanzas Personales V2 - Bot")
            try:
                user_email = os.getenv('USER_EMAIL', 'prueba@prueba.com')
                if user_email:
                    spreadsheet.share(user_email, perm_type='user', role='writer')
            except Exception as e:
                logger.error(f"Error al compartir hoja: {e}")

        # Configurar hoja principal
        worksheet = spreadsheet.sheet1
        headers = ['Fecha', 'Tipo', 'Categoría', 'Ubicación', 'Moneda', 'Monto', 'Tasa Usada', 'USD Equivalente', 'Descripción']
        if not worksheet.acell('A1').value:
            worksheet.update(range_name='A1:I1', values=[headers])
            logger.info("Encabezados inicializados")
        
        # Inicializar gestores
        global gestor_deudas, gestor_saldos
        gestor_deudas = GestorDeudas(spreadsheet)
        gestor_saldos = GestorSaldos(worksheet, gestor_tasas) # Usamos la versión de saldos.py

        return spreadsheet
    except Exception as e:
        logger.error(f"Error al obtener/crear spreadsheet: {e}")
        raise

def classify_transaction(text: str) -> dict:
    """Usa Groq para clasificar la transacción con ubicación y moneda"""
    try:
        normalized_text = normalize_input(text)
        
        # ⚠️ Detectar Comandos Cashea Naturales antes de llamar a la IA
        if "cashea" in normalized_text and "gasto" in normalized_text:
            return {
                "tipo": "Egreso",
                "categoria": "Compras",
                "ubicacion": "Venezuela",
                "moneda": "USD", # Default cashea
                "monto": 0, # Se calculará después
                "descripcion": normalized_text,
                "es_cashea": True, # Flag especial
                "raw_text": normalized_text
            }

        prompt_content = SYSTEM_PROMPT.replace("{normalized_text}", normalized_text)

        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Responde siempre en JSON puro."},
                {"role": "user", "content": prompt_content}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            max_tokens=500
        )

        result_text = response.choices[0].message.content.strip()
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.strip().startswith('json'):
                result_text = result_text.strip()[4:]
            result_text = result_text.strip()

        result = json.loads(result_text)
        
        # Validación básica y correcciones
        required_keys = ['tipo', 'categoria', 'ubicacion', 'moneda', 'monto', 'descripcion']
        for key in required_keys:
            if key not in result:
                if key == 'ubicacion': result['ubicacion'] = 'Venezuela'
                elif key == 'moneda': result['moneda'] = 'Bs'
                else: raise ValueError(f"Falta campo requerido: {key}")

        try:
            result['monto'] = float(result['monto'])
        except:
             result['monto'] = 0

        return result

    except Exception as e:
        logger.error(f"Error en clasificación: {e}")
        raise

def save_to_sheets(transaction_data: dict, tasa_usada: float = None) -> bool:
    """Guarda la transacción en Google Sheets"""
    try:
        spreadsheet = get_or_create_spreadsheet()
        worksheet = spreadsheet.sheet1
        
        msg_extra = ""
        fecha_compra = datetime.now().strftime("%Y-%m-%d")

        # 🟢 LÓGICA CASHEA (V3)
        if transaction_data.get('es_cashea'):
            texto = transaction_data.get('raw_text', '').lower()
            
            numeros = re.findall(r'\d+\.?\d*', texto)
            if not numeros: return False, "No encontré el monto de la compra"
            # Asumimos que el primer número es el monto total si no está especificado
            monto_total = float(numeros[0])
            
            linea = "cotidiana" if "cotidiana" in texto else "principal"
            
            # Detectar Fuente (Cashea, Binance, etc)
            fuentes = ["binance", "mercantil", "banesco", "zelle", "efectivo", "cashea"]
            fuente_usada = "Cashea" # Default
            for f in fuentes:
                if f in texto:
                    fuente_usada = f.capitalize()
                    break

            # 1. Buscar porcentaje explícito (ej: "40% inicial" o "inicial 40%")
            match_porcentaje = re.search(r'(\d+(?:\.\d+)?)%\s*inicial|inicial\s*(\d+(?:\.\d+)?)%', texto)
            # 2. Buscar monto fijo explícito (ej: "inicial 50")
            match_fijo = re.search(r'inicial\s+(\d+(?:\.\d+)?)', texto)
            
            inicial_usuario = None
            
            if match_porcentaje:
                # Extraer el grupo que no sea None
                pct_str = match_porcentaje.group(1) or match_porcentaje.group(2)
                pct = float(pct_str)
                inicial_usuario = monto_total * (pct / 100)
            elif match_fijo:
                inicial_usuario = float(match_fijo.group(1))

            # Simulación y Validación
            simulacion = gestor_deudas.simular_compra_cashea(monto_total, linea)
            
            if inicial_usuario is not None:
                if simulacion and simulacion['es_ajustado'] and inicial_usuario < simulacion['inicial_a_pagar']:
                    msg_extra = f"\n⚠️ OJO: Tu inicial manual (${inicial_usuario}) es menor a la requerida por límite (${simulacion['inicial_a_pagar']:.2f})."
                monto_inicial_real = inicial_usuario
            else:
                if not simulacion: return False, "Error simulando crédito"
                monto_inicial_real = simulacion['inicial_a_pagar']
                if simulacion['es_ajustado']:
                    msg_extra = f"\n⚠️ Inicial Ajustada Automáticamente: ${monto_inicial_real:.2f}"

            desc = f"Cashea: {transaction_data.get('descripcion', 'Compra')}"
            gestor_deudas.crear_deuda(
                descripcion=desc, 
                monto_total=monto_total, 
                monto_inicial=monto_inicial_real, 
                tipo=f"Cashea ({linea})",
                fuente=fuente_usada
            )
            
            transaction_data['monto'] = monto_inicial_real
            transaction_data['descripcion'] = f"{desc} (Inicial)"
            msg_extra += f"\n📦 Deuda {fuente_usada} creada. Resta: ${monto_total - monto_inicial_real:.2f}"

        elif transaction_data.get('es_credito'):
             gestor_deudas.crear_deuda(transaction_data['descripcion'], transaction_data['monto_total_credito'], transaction_data['monto'])

        elif transaction_data.get('es_pago_cuota'):
            success, info = gestor_deudas.registrar_pago_cuota(transaction_data.get('referencia_deuda'), transaction_data['monto'])
            msg_extra = f"\n💳 {info}" if success else f"\n⚠️ {info}"

        # 📅 FECHA
        fecha_str = transaction_data.get('fecha')
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if fecha_str:
            try:
                if fecha_str.lower() == 'ayer': fecha_dt = datetime.now() - timedelta(days=1)
                elif fecha_str.lower() == 'hoy': fecha_dt = datetime.now()
                else: fecha_dt = datetime.strptime(fecha_str.replace('-', '/'), "%d/%m/%Y")
                fecha = fecha_dt.strftime("%Y-%m-%d %H:%M:%S")
            except: pass

        # Convertir a USD / Bs logic
        monto_original = transaction_data['monto']
        moneda = transaction_data['moneda']
        tipo = transaction_data['tipo'].lower()

        if tipo == "egreso":
            monto_original = -abs(monto_original)
            monto_usd_multiplicador = -1
        else:
            monto_usd_multiplicador = 1

        tasa_usada_final = 1.0
        monto_usd = 0

        if moneda == "Bs":
            if not tasa_usada:
                try:
                    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d %H:%M:%S")
                    if fecha_dt.date() < datetime.now().date():
                        tasa_usada = gestor_tasas.obtener_tasa_historica(fecha_dt.strftime("%Y-%m-%d"))
                except: pass
                if not tasa_usada: tasa_usada = gestor_tasas.obtener_tasa()
            
            tasa_usada_final = tasa_usada if tasa_usada else 0
            if tasa_usada_final > 0:
                monto_usd = (monto_original / tasa_usada_final) * monto_usd_multiplicador
        
        elif moneda in ["USD", "USDT"]:
             monto_usd = monto_original
        
        row = [
            fecha, transaction_data['tipo'], transaction_data['categoria'],
            transaction_data['ubicacion'], moneda, monto_original,
            tasa_usada_final if moneda == "Bs" else "", monto_usd,
            transaction_data['descripcion']
        ]

        worksheet.append_row(row, table_range="A1")
        return True, msg_extra

    except Exception as e:
        logger.error(f"Error guardar Sheets: {e}")
        return False, str(e)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos de facturas (Integración Gemini)"""
    if not update.message or not update.message.photo: return
    await update.message.reply_text("📸 Analizando factura...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        from io import BytesIO
        img_buffer = BytesIO()
        await photo_file.download_to_memory(out=img_buffer)
        
        prompt_vision = """Analiza esta imagen. Responde SOLO JSON:
        {
            "tipo": "Egreso",
            "categoria": "Alimentación, Transporte, Salud, Servicios, Compras, Limpieza u Otro",
            "ubicacion": "Ecuador" o "Venezuela" (inferir por moneda: Bs=Venezuela, USD=Ecuador),
            "moneda": "USD" o "Bs",
            "subtotal": número o null,
            "iva": número o null,
            "total": número,
            "descripcion": "nombre del local + items",
            "fecha": "DD/MM/YYYY" o null (verifica año 2025),
            "tasa_especifica": número o null (SOLO Tasa de Cambio BCV/Paralelo. NO confundir con 16% IVA o Alícuota),
        }"""
        
        # Lógica simplificada Gemini
        img_buffer.seek(0)
        from PIL import Image
        image = Image.open(img_buffer)
        
        model = genai.GenerativeModel('models/gemini-flash-latest')
        response = model.generate_content([prompt_vision, image])
        result_text = response.text.strip()
        
        if result_text.startswith('```'): 
            result_text = result_text.split('```')[1].replace('json','').strip()
            
        transaction = json.loads(result_text)
        
        # Mapear 'total' a 'monto' si es necesario
        if 'total' in transaction and 'monto' not in transaction:
            transaction['monto'] = transaction['total']
             
        tasa = transaction.get('tasa_especifica') if transaction['moneda'] == 'Bs' else None
        
        success, msg = save_to_sheets(transaction, tasa)
        if success:
            await update.message.reply_text(f"✅ Factura Guardada!\n💵 Total: {transaction['moneda']} {transaction.get('monto')}\n📝 {transaction.get('descripcion')}")
        else:
            await update.message.reply_text(f"❌ Error: {msg}")

    except Exception as e:
        logger.error(f"Error Vision: {e}")
        await update.message.reply_text("❌ Error analizando imagen.")

# --- COMANDOS ESTRUCTURALES ---

async def comando_cashea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simulador de Compra Cashea"""
    try:
        args = context.args
        if not args or len(args) < 1:
            await update.message.reply_text("🔎 Uso: `/cashea [monto] [linea:p/c]`\nEj: `/cashea 120`")
            return
            
        monto = float(args[0])
        linea = args[1] if len(args) > 1 else "principal"
        
        if not gestor_deudas: get_or_create_spreadsheet()
        
        res = gestor_deudas.simular_compra_cashea(monto, linea)
        
        msg = f"🛍️ **SIMULACIÓN CASHEA (${monto:.2f})**\n\n"
        msg += f"• **Inicial:** `${res['inicial_a_pagar']:.2f}`\n"
        msg += f"• **Crédito:** `${res['monto_financiar']:.2f}`\n"
        msg += f"• **Disponible Antes:** `${res['disponible_antes']:.2f}`\n\n"
        msg += f"{res['mensaje']}"
        
        await update.message.reply_text(msg, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")



async def comando_importardeuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Importa deuda con parsing inteligente y desglose.
    Soporta: /importardeuda [Fuente?] [Monto] [Cuotas] [Desc] [Fecha]
    Ej: /importardeuda Cashea 56 usd 2 cuota Monitor 30/12/2025
    """
    try:
        args = context.args
        if not args:
            await update.message.reply_text("❌ Uso: `/importardeuda 56 usd 2 cuotas Monitor Cashea`")
            return
            
        full_text = " ".join(args)
        
        # 1. Extraer FECHA
        fecha_match = re.search(r'\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b', full_text)
        prox_venc = datetime.now().strftime("%Y-%m-%d")
        if fecha_match:
            try:
                dt = datetime.strptime(fecha_match.group(1).replace('-', '/'), "%d/%m/%Y")
                prox_venc = dt.strftime("%Y-%m-%d")
                full_text = full_text.replace(fecha_match.group(0), "")
            except: pass
        elif "hoy" in full_text.lower():
            full_text = re.sub(r'\bhoy\b', '', full_text, flags=re.IGNORECASE)
        
        # 2. Extraer CUOTAS
        num_cuotas = 1
        cuotas_match = re.search(r'\b(\d+)\s*(?:cuota|plazo|mes|pago)s?\b', full_text, re.IGNORECASE)
        if cuotas_match:
             num_cuotas = int(cuotas_match.group(1))
             full_text = full_text.replace(cuotas_match.group(0), "")
        else:
             numeros = re.findall(r'\b\d+(?:\.\d+)?\b', full_text)
             if len(numeros) >= 2:
                 pos = float(numeros[1])
                 if pos.is_integer() and pos < 24:
                     num_cuotas = int(pos)
                     full_text = re.sub(r'\b' + str(int(pos)) + r'\b', '', full_text, count=1)

        # 3. Extraer MONTO
        monto_match = re.search(r'\b\d+(?:\.\d+)?\b', full_text)
        if not monto_match:
            await update.message.reply_text("❌ Falta el monto.")
            return
        monto_cuota = float(monto_match.group(0))
        full_text = full_text.replace(monto_match.group(0), "", 1)
        
        # 4. Extraer FUENTE (Detectar palabras clave)
        fuentes_conocidas = ["cashea", "binance", "banesco", "mercantil", "zelle", "pagomovil", "tdc"]
        fuente_detectada = "Binance" # Default
        
        for f in fuentes_conocidas:
            if re.search(r'\b' + f + r'\b', full_text, re.IGNORECASE):
                fuente_detectada = f.capitalize()
                full_text = re.sub(r'\b' + f + r'\b', '', full_text, flags=re.IGNORECASE)
                break
        
        # 5. Limpieza Final
        basura = ['usd', 'bs', 'pesos', 'dolares', 'bolivares', '$', '€', 'de', 'del', 'la', 'el']
        for b in basura:
            full_text = re.sub(r'\b' + re.escape(b) + r'\b', '', full_text, flags=re.IGNORECASE)
            
        descripcion = re.sub(r'\s+', ' ', full_text).strip()
        if not descripcion: descripcion = "Importado"

        if not gestor_deudas: get_or_create_spreadsheet()

        # Usar la nueva lógica de plan de cuotas o simple
        if num_cuotas > 1:
            linea = "Principal"
            success, msg_plan = gestor_deudas.crear_plan_cuotas(
                descripcion=descripcion,
                monto_cuota=monto_cuota,
                num_cuotas=num_cuotas,
                fecha_inicio=prox_venc,
                linea=linea,
                fuente=fuente_detectada
            )
            msg = f"✅ **Plan Registrado ({fuente_detectada})**\n{msg_plan}\n📅 Inicio: {prox_venc}"
        else:
            # Una sola cuota
            if fuente_detectada.lower() == "cashea":
                tipo_deuda = "Cashea (Cotidiana) - Importado"
            else:
                tipo_deuda = f"Deuda ({fuente_detectada})"
                
            gestor_deudas.crear_deuda(
                descripcion=f"Imp: {descripcion}",
                monto_total=monto_cuota,
                monto_inicial=0,
                tipo=tipo_deuda,
                proximo_vencimiento=prox_venc,
                fuente=fuente_detectada
            )
            msg = f"✅ **Deuda Registrada ({fuente_detectada})**\n📦 {descripcion}\n💰 ${monto_cuota} (1 cuota)\n📅 Vence: {prox_venc}\n🏷️ Tipo: {tipo_deuda}"
        
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")



async def comando_custodia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Registra fondos de terceros (Pasivo).
    Uso: /custodia [monto] [descripcion]
    Ej: /custodia 100 Ahorros Papa
    """
    try:
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text("❌ Uso: `/custodia [monto] [descripcion]`\nEj: `/custodia 100 Ahorro Papa`")
            return
            
        monto = float(args[0])
        descripcion = " ".join(args[1:])
        
        if not gestor_deudas: get_or_create_spreadsheet()
        
        # Crear Pasivo tipo 'Custodia'
        gestor_deudas.crear_deuda(
            descripcion=f"Custodia: {descripcion}",
            monto_total=monto,
            monto_inicial=0,
            tipo="Custodia (Pasivo)",
            proximo_vencimiento="N/A" # No vence, es indeterminado
        )
        
        msg = f"🔐 **Fondo en Custodia Registrado**\n"
        msg += f"📝 Concepto: {descripcion}\n"
        msg += f"💰 Monto: ${monto}\n"
        msg += "⚠️ Recuerda registrar el INGRESO real si el dinero entró a tus cuentas (ej: `ingreso 100 usd binance`)."
        
        await update.message.reply_text(msg, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ El monto debe ser numérico")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def comando_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ver saldo acumulado (suma de transacciones)"""
    try:
        if not gestor_saldos: get_or_create_spreadsheet()
        
        # Si pasan argumentos, filtrar ubicación
        if context.args:
            ubicacion = context.args[0]
            mensaje = gestor_saldos.obtener_saldo_por_ubicacion_formateado(ubicacion)
        else:
            mensaje = gestor_saldos.obtener_portafolio_detallado()
            
        await update.message.reply_text(mensaje)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_msg = """👋 **Bienvenido a tu Bot Financiero V3** 🚀

Aquí tienes tu "Chuleta" de comandos rápidos:

📝 **GASTOS E INGRESOS (Básico)**
• `gasto 50 bs comida` (Gastos del día a día)
• `ingreso 2000 sueldo` (Tus entradas)
• `gasté 15 usd uber` (Reconoce monedas)

🛍️ **MODO CASHEA (V3)**
• **Nueva Compra:** `gasto 120 zapatos cashea`
  *(El bot calcula tu inicial y crea las cuotas automáticamente)*
• **Importar Deuda Vieja:** `/importardeuda 20 3 "TV" 15/01/2025`
  *(Para registrar lo que ya debes: 3 cuotas de $20)*

🏦 **CONTROL DE SALDOS**
• **Cargar Saldo Inicial:** `ingreso 500 bs banesco saldo inicial`
• **Ver mis Cuentas:** `/saldo`
• **Dinero de Terceros (Papá):** `/custodia 100 Ahorros Papa`
  *(Registra que 100 de tu saldo son prestados/custodia)*

💱 **CONVERSIONES (Binance)**
• `cambié 100 usd a 98 usdt`
• `cambié 50 usdt a 2500 bs`

📸 **FACTURAS**
¡Solo envíame una foto! Yo leo los montos y la fecha.

💡 **COMANDOS ÚTILES**
/saldo - Resumen total de tu dinero
/deudas - Ver tus créditos pendientes
/tasa - Ver precio del dólar BCV
"""
    await update.message.reply_text(help_msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text
    
    # 💸 DETECTAR PAGO DE DEUDA ESPECÍFICA (ID)
    # Formato: "pagué deuda-5 [25/12/2025]"
    match_pago = re.search(r'pagu[ée]\s+(deuda-\d+)(?:\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))?', text, re.IGNORECASE)
    if match_pago:
        deuda_id = match_pago.group(1)
        fecha_pago_raw = match_pago.group(2)
        
        # Determinar Fecha y Tasa
        if fecha_pago_raw:
             # Normalizar fecha
             try:
                 parts = re.split(r'[-/]', fecha_pago_raw)
                 # Asumir DD/MM/YYYY
                 fecha_pago = f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts[2])==4 else f"20{parts[2]}-{parts[1]}-{parts[0]}"
             except:
                 fecha_pago = datetime.now().strftime("%Y-%m-%d")
                 
             # Tasa Histórica
             tasa = gestor_tasas.obtener_tasa_historica(fecha_pago)
             if not tasa: tasa = gestor_tasas.obtener_tasa()
        else:
             fecha_pago = datetime.now().strftime("%Y-%m-%d")
             tasa = gestor_tasas.obtener_tasa()
        
        # Procesar Pago
        exito, msg, transaccion = gestor_deudas.pagar_deuda_completa(deuda_id, fecha_pago, tasa)
        
        if exito and transaccion:
            s, m = save_to_sheets(transaccion)
            await update.message.reply_text(f"{msg}\n✅ Egreso registrado: {m}")
        else:
            await update.message.reply_text(f"❌ {msg}")
        return
    
    if "cashea" in text.lower() and "gasto" in text.lower():
        t_data = classify_transaction(text)
        success, msg = save_to_sheets(t_data)
        if success: await update.message.reply_text(f"🛍️ **Cashea Registrado!**\n{msg}")
        else: await update.message.reply_text(f"❌ Error: {msg}")
        return

    await update.message.reply_text("🔄 Procesando...")
    try:
        t_data = classify_transaction(text)
        
        # 🔄 LÓGICA DE CONVERSIÓN (Forex)
        # 🔄 LÓGICA DE CONVERSIÓN (Forex)
        if t_data.get('tipo', '').lower() == 'conversión' or t_data.get('moneda_destino'):
            
            def obtener_ubicacion_por_moneda(moneda):
                moneda = moneda.upper()
                if moneda in ['USDT', 'BTC', 'ETH', 'BNB']: return 'Binance'
                if moneda in ['BS', 'VES']: return 'Venezuela'
                if moneda == 'USD': return 'Ecuador'
                return 'Venezuela' # Default safe

            # Transacción 1: Salida (Egreso)
            t_salida = t_data.copy()
            t_salida['tipo'] = 'Egreso'
            t_salida['categoria'] = 'Conversión'
            t_salida['descripcion'] = f"Conversión a {t_data.get('moneda_destino')}"
            # Forzar ubicación de salida basada en su moneda
            t_salida['ubicacion'] = obtener_ubicacion_por_moneda(t_salida.get('moneda', ''))

            # Transacción 2: Entrada (Ingreso)
            t_entrada = t_data.copy()
            t_entrada['tipo'] = 'Ingreso'
            t_entrada['categoria'] = 'Conversión'
            t_entrada['monto'] = t_data.get('monto_destino')
            t_entrada['moneda'] = t_data.get('moneda_destino')
            t_entrada['descripcion'] = f"Conversión desde {t_data.get('moneda')}"
            # Forzar ubicación de entrada basada en su moneda
            t_entrada['ubicacion'] = obtener_ubicacion_por_moneda(t_entrada.get('moneda', ''))
            
            # 💱 Calcular Tasa Implícita para Conversiones
            try:
                m_sale = float(t_salida.get('monto', 0))
                m_entra = float(t_entrada.get('monto', 0))
                mon_sale = t_salida.get('moneda', '').upper()
                mon_entra = t_entrada.get('moneda', '').upper()
                
                # Caso: Venta de USD/USDT a Bs (Entrada en Bs)
                if mon_entra in ['BS', 'VES'] and mon_sale in ['USD', 'USDT'] and m_sale > 0:
                    tasa_calc = m_entra / m_sale
                    t_entrada['tasa_especifica'] = tasa_calc
                
                # Caso: Compra de USD/USDT con Bs (Salida en Bs)
                elif mon_sale in ['BS', 'VES'] and mon_entra in ['USD', 'USDT'] and m_entra > 0:
                    tasa_calc = m_sale / m_entra
                    t_salida['tasa_especifica'] = tasa_calc
            except: pass
                
            # Guardar ambas
            s1, m1 = save_to_sheets(t_salida)
            s2, m2 = save_to_sheets(t_entrada)
            
            if s1 and s2:
                await update.message.reply_text(f"✅ **Conversión Exitosa**\n📤 Salió: {t_salida['monto']} {t_salida['moneda']}\n📥 Entró: {t_entrada['monto']} {t_entrada['moneda']}")
            else:
                await update.message.reply_text(f"⚠️ **Conversión Parcial**\nSalida: {m1}\nEntrada: {m2}")
                
        else:
            # Flujo Normal
            success, msg = save_to_sheets(t_data)
            await update.message.reply_text("✅ Listo!" + msg if success else "❌ Error: " + str(msg))

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ No entendí.")


async def comando_simple_tasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tasa: {gestor_tasas.obtener_tasa()}")

async def comando_simple_deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasa = gestor_tasas.obtener_tasa()
    except:
        tasa = 0
    await update.message.reply_text(gestor_deudas.obtener_resumen(tasa_local=tasa))

def main():
    if not TELEGRAM_TOKEN: return
    try: get_or_create_spreadsheet()
    except: pass
    keep_alive.keep_alive()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cashea", comando_cashea))
    app.add_handler(CommandHandler("importardeuda", comando_importardeuda))
    app.add_handler(CommandHandler("custodia", comando_custodia))
    app.add_handler(CommandHandler("saldo", comando_saldo))
    app.add_handler(CommandHandler("tasa", comando_simple_tasa))
    app.add_handler(CommandHandler("deudas", comando_simple_deudas))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()