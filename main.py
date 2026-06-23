from fastapi import FastAPI,HTTPException,status
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime
import re
import json 
import urllib.request
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager



# Interruptor
load_dotenv()

# Url
MONGO_URI = os.getenv("MONGO_URI")

# Cliente
cliente = MongoClient(MONGO_URI)

# Base de datos
db = cliente["montior_precios"]

# Registro de productos
coleccion_productos = db["productos"]


# --- CORE: FUNCIÓN DETONADORA DEL SCRAPER (EL TRABAJADOR) ---
def procesar_un_producto(url:str, selector: str):
    """
    Esta función contiene la lógica central. 
    Hace el scraping, limpia el precio, compara y dispara la alerta si baja.
    """
    print(f"🤖 [CORE] Iniciando chequeo para: {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=60000)
            precio_crudo = page.locator(selector).first.inner_text()
            browser.close()
    except Exception as e:
        print(f"❌ [CORE] Error en Playwright para {url}: {str(e)}")
        return {"status": "error", "message": str(e)}

    if not precio_crudo:
        print(f"⚠️ [CORE] No se encontró el precio para {url}")
        return {"status": "error", "message": "Selector no encontrado"}

    precio_actual = limpiar_precio(precio_crudo)
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    producto_existente = db["productos"].find_one({"url": url})

    if not producto_existente:
        # Registro inicial
        nuevo_producto = {
            "url": url,
            "selector": selector,
            "precio_actual_catalogo": precio_actual,
            "ultima_actualizacion": fecha_actual
        }
        db["productos"].insert_one(nuevo_producto)
        return {"status": "success", "message": "Registrado por primera vez", "precio": precio_actual}
    
    else:
        # Comparación histórica
        precio_anterior = producto_existente["precio_actual_catalogo"]
        
        nuevo_historial = {
            "producto_id": producto_existente["_id"],
            "precio": precio_actual,
            "fecha_capture": fecha_actual
        }
        db["historial_precios"].insert_one(nuevo_historial)

        if precio_actual < precio_anterior:
            producto_titulo = url.split("/")[-2].replace("-", " ").title()
            enviar_alerta_discord(producto_titulo, precio_anterior, precio_actual, url)
            
            db["productos"].update_one(
                {"_id": producto_existente["_id"]},
                {"$set": {"precio_actual_catalogo": precio_actual, "ultima_actualizacion": fecha_actual}}
            )
            return {"status": "alerta", "precio_anterior": precio_anterior, "precio_nuevo": precio_actual}

        db["productos"].update_one(
            {"_id": producto_existente["_id"]},
            {"$set": {"ultima_actualizacion": fecha_actual}}
        )
        return {"status": "success", "message": "Precio sin cambios", "precio": precio_actual}
    
# --- TAREA PROGRAMADA (CRON JOB) ---
def tarea_automatica_monitoreo():
    """
    Esta función se ejecuta sola en segundo plano gracias al Scheduler.
    Busca TODOS los productos de la base de datos y los manda a scrapear.
    """
    print(f"\n⏰ [SCHEDULER] ¡Hora de trabajar! Iniciando ciclo automático: {datetime.now()}")
    
    # Traemos todos los productos registrados en el catálogo maestro
    productos = db["productos"].find()
    
    conteo = 0
    for prod in productos:
        procesar_un_producto(prod["url"], prod["selector"])
        conteo += 1
        
    print(f"⏰ [SCHEDULER] Ciclo terminado. Se procesaron {conteo} productos.\n")

# --- MANEJO DEL CICLO DE VIDA DE FASTAPI (LIFESPAN) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Al encender el servidor: Arrancamos el reloj en segundo plano
    scheduler = BackgroundScheduler()
    
    # CONFIGURACIÓN DEL TIEMPO:
    # Para el portafolio lo dejamos cada 30 segundos de modo que se pueda probar rápido.
    # En producción lo cambiarías a: hours=12 o days=1
    scheduler.add_job(tarea_automatica_monitoreo, 'interval', seconds=30)
    
    scheduler.start()
    print("🚀 [SISTEMA] Scheduler encendido y programado cada 30 segundos.")
    
    yield  # Aquí es donde FastAPI se mantiene corriendo felizmente
    
    # 2. Al apagar el servidor: Apagamos el reloj limpiamente
    scheduler.shutdown()
    print("🛑 [SISTEMA] Scheduler apagado limpiamente.")

# Iniciar server con uvicorn main:app --reload
app = FastAPI(title="Monitor de Precios Inteligente",
    description="API para hacer web scraping de productos y seguir su historial de precios.",
    lifespan=lifespan)

# --- FUNCIONES AUXILIARES (UTILIDADES) ---
def limpiar_precio(texto_precio: str) -> float:
    """Extrae únicamente los números y decimales de un string de precio."""
    # Busca números y puntos/comas decimales (ej: "£51.77" -> "51.77")
    coincidencia = re.search(r"[-+]?\d*\.\d+|\d+", texto_precio)
    if coincidencia:
        return float(coincidencia.group())
    return 0.0

def enviar_alerta_discord(producto_titulo: str, precio_viejo: float, precio_nuevo: float, url: str):
    """
    Envía una notificación push con un diseño elegante (Embed) a un canal de Discord
    utilizando el webhook configurado en las variables de entorno..
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    # Control de seguridad: Si no hay URL configurada, evitamos que el código falle
    if not webhook_url:
        print("⚠️ [ADVERTENCIA] No se encontró DISCORD_WEBHOOK_URL en el archivo .env")
        return
    
    # Calculamos el porcentaje de descuento para hacerlo más vistoso
    descuento = round(((precio_viejo - precio_nuevo) / precio_viejo) * 100, 1)
    # Creamos un diseño tipo 'Embed' (tarjeta elegante en Discord)
    payload = {
        "username": "Monitor de Precios Bot",
        "avatar_url": "https://i.imgur.com/vHdf7n3.png", # Icono de robot de ejemplo
        "embeds": [
            {
                "title": f"📉 ¡ALERTA DE OFERTA! - {descuento}% DE DESCUENTO",
                "description": f"El producto **{producto_titulo}** ha bajado de precio de forma drástica.",
                "url": url,
                "color": 3066993, # Color verde en formato decimal de Discord
                "fields": [
                    {
                        "name": "💰 Precio Anterior",
                        "value": f"£{precio_viejo}",
                        "inline": True
                    },
                    {
                        "name": "✨ Precio Nuevo",
                        "value": f"£{precio_nuevo}",
                        "inline": True
                    }
                ],
                "footer": {
                    "text": "Monitoreo Automatizado de Precios",
                },
                "timestamp": datetime.now().isoformat()
            }
        ]
    }

    try:
        # Convertimos el diccionario de Python a un string JSON y luego a bytes
        data_codificada = json.dumps(payload).encode('utf-8')
        
        # Preparamos la petición HTTP POST simulando ser un navegador (User-Agent)
        req = urllib.request.Request(
            webhook_url, 
            data=data_codificada, 
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        
        # Enviamos la petición
        with urllib.request.urlopen(req) as response:
            if response.status in [200, 204]:
                print(f"🚀 [WEBHOOK] Alerta enviada con éxito a Discord para: {producto_titulo}")
            else:
                print(f"⚠️ [WEBHOOK] Discord respondió con código: {response.status}")
                
    except Exception as e:
        print(f"❌ [WEBHOOK] Falló el envío al webhook de Discord: {str(e)}")

# --- MODELOS DE DATOS (PYDANTIC) ---
class ProductoRequest(BaseModel):
    url : str
    selector : str

# --- ENDPOINTS ---
@app.post("/check-price", status_code=status.HTTP_200_OK)
def check_price(request: ProductoRequest):
    # Ahora el endpoint simplemente delega el trabajo a la función central
    resultado = procesar_un_producto(request.url, request.selector)
    return resultado



@app.get("/price-history")
def get_price_history(url: str):
    producto = db["productos"].find_one({"url": url})
    
    if producto is None:
        return {"status": "error", "message": "No existe el producto en el catálogo"}
    
    # Buscamos en el historial usando el ObjectId puro de la relación
    historial_cursor = db["historial_precios"].find({"producto_id": producto["_id"]})
    
    lista_historial = []
    for registro in historial_cursor:
        lista_historial.append({
            "precio": registro["precio"],
            "fecha_capture": registro["fecha_captura"]
        })
        
    return {
        "status": 200,
        "producto": {
            "url": producto["url"],
            "selector": producto["selector"],
            "precio_actual_catalogo": producto["precio_actual_catalogo"]
        },
        "historial_de_precios": lista_historial
    }








 
