from main import get_google_sheets_client
from deudas import GestorDeudas
import logging

# Config logger minimal
logging.basicConfig(level=logging.INFO)

def run_migration():
    gc = get_google_sheets_client()
    sh = gc.open("Finanzas Personales V2 - Bot")
    gestor = GestorDeudas(sh)
    
    print("Iniciando migración...")
    res = gestor.migrar_ids_legacy()
    print(res)

if __name__ == "__main__":
    run_migration()
