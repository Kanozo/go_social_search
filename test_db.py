from supabase import create_client, Client
import os
from dotenv import load_dotenv

# Cargar las variables del archivo .env
load_dotenv()

# Obtener las credenciales
url = "https://wpsxnyzeyrrxostzqifh.supabase.co/"
key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indwc3hueXpleXJyeG9zdHpxaWZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAxMzE1OTAsImV4cCI6MjA5NTcwNzU5MH0.z3tKNqATfXyxKcroVxWOmTFp84rqP3VDvAX6KitG5DQ"

# Crear el cliente de Supabase
supabase: Client = create_client(url, key)

print("✅ Conexión establecida con Supabase")

def test_connection():
    try:
        # Intentar obtener las tablas de la base de datos
        # Esto verifica que la conexión funciona
        response = supabase.table("Instagram").select("*").limit(10).execute()
        print("✅ Conexión exitosa")
        print(f"Datos recibidos: {response.data}")
        return True
    except Exception as e:
        print(f"❌ Error al conectar: {e}")
        return False

if __name__ == "__main__":
    test_connection()