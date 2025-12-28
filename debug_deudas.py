import gspread
import os
from google.oauth2.service_account import Credentials

def _parse_float(val):
    try:
        print(f"DEBUG PARSE: val='{val}' type={type(val)}")
        if isinstance(val, (float, int)):
            return float(val)
        clean_val = str(val).replace(',', '.').strip()
        clean_val = "".join(c for c in clean_val if c.isdigit() or c == '.')
        print(f"DEBUG PARSE: clean='{clean_val}'")
        return float(clean_val)
    except Exception as e:
        print(f"DEBUG ERROR: {e}")
        return 0.0

def debug():
    credentials = Credentials.from_service_account_file(
        'google_credentials.json',
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    gc = gspread.authorize(credentials)
    sh = gc.open("Finanzas Personales V2 - Bot")
    ws = sh.worksheet("Deudas")
    
    # Obtener valores raw sin conversión automática de gspread
    records = ws.get_all_records(numericise_ignore=['all'])
    
    print("\n--- ANALIZANDO REGISTROS ---")
    for r in records:
        desc = r.get("Descripción", "")
        # Filtrar solo el problemático
        if "Supermercado" in desc:
            restante = r.get("Restante")
            print(f"\nITEM: {desc}")
            print(f"RAW RESTANTE: '{restante}' (Type: {type(restante)})")
            parsed = _parse_float(restante)
            print(f"PARSED RESTANTE: {parsed}")

if __name__ == "__main__":
    debug()
