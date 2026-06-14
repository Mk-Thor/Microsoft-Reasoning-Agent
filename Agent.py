from typing import TypedDict
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from pystac_client import Client
from rasterio.warp import transform
from rasterio.windows import Window
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential

import os
import re
import requests
import h5py
import numpy as np
import tempfile
import planetary_computer
import rasterio

from datetime import datetime, timedelta

load_dotenv()

class AgentState(TypedDict, total=False):
    user_input: str
    next_node: str
    response: str
    area_name: str
    latitude: float
    longitude: float
    past_smap: dict
    weather: dict
    ndvi: dict
    llm_input: str
    forecast_error: str
    ndvi_error: str
    advisory: str          

llm = ChatGroq(
    api_key=os.getenv("MS_OPENAI_KEY_ONE"),
    model="llama-3.3-70b-versatile",
    temperature=0
)

def input_node(state: AgentState):
    """
    Receives user input.
    """
    return {
        "user_input": state["user_input"]
    }


def llm_node(state: AgentState):
    """
    Decides which agent should handle the request.
    Outputs a structured tag so route_after_llm can parse it reliably.
    """

    system_prompt = """
    You are an intelligent router agent for a farm weather forecasting system.

    Your job is to:
    1. Understand the user's request.
    2. If the user is asking about weather or soil moisture forecasting for
       a specific area, region, district, city, village or place,
       output EXACTLY this on the FIRST line (no other text before it):
       Requested_area = "<area name>"
       Then on the next line write: ROUTE=forecast
    3. If the user's request does NOT mention a specific location,
       output EXACTLY on the FIRST line:
       Requested_area = "UNKNOWN"
       Then on the next line write: ROUTE=end
       Then politely ask the user to provide a location name.
    4. For any request unrelated to weather/soil/farm forecasting,
       output EXACTLY on the FIRST line:
       Requested_area = "UNKNOWN"
       Then on the next line write: ROUTE=end
       Then explain you can only handle weather and farm forecasting queries.

    Examples:

    User: "What is the soil moisture in Thanjavur?"
    Output:
    Requested_area = "Thanjavur"
    ROUTE=forecast
    This request is about soil moisture forecasting for Thanjavur district. The geocode and forecasting agents will handle it.

    User: "What is the weather now?"
    Output:
    Requested_area = "UNKNOWN"
    ROUTE=end
    Please provide a specific location name (e.g., city, district, or village) so I can fetch the forecast for you.
    """

    prompt = f"""
    {system_prompt}

    User Query:
    {state['user_input']}
    """

    response = llm.invoke(prompt)

    return {
        "response": response.content
    }


def route_after_llm(state: AgentState) -> str:
    """
    Parses the LLM response to decide the next node.
    Returns "geocode_node" or "end" — these must match
    the keys in the add_conditional_edges ends dict.
    """
    response = state.get("response", "")


    route_match = re.search(r"ROUTE\s*=\s*(\w+)", response)
    if route_match:
        route_value = route_match.group(1).strip().lower()
        if route_value == "forecast":
            return "geocode_node"
        return "end"

    area_match = re.search(r'Requested_area\s*=\s*"([^"]+)"', response)
    if area_match:
        area_name = area_match.group(1).strip()
        if area_name and area_name.upper() != "UNKNOWN":
            return "geocode_node"

    return "end"


def geocode_location(location_name: str) -> dict:
    """
    Converts a location name to latitude and longitude
    using the Open-Meteo geocoding API.
    """
    url = "https://geocoding-api.open-meteo.com/v1/search"

    response = requests.get(
        url,
        params={
            "name": location_name,
            "count": 1
        },
        timeout=10
    )
    response.raise_for_status()

    data = response.json()

    if "results" not in data or len(data["results"]) == 0:
        raise ValueError(f"Location not found: {location_name}")

    result = data["results"][0]

    return {
        "lat": result["latitude"],
        "lon": result["longitude"]
    }


def geocode_node(state: AgentState) -> dict:
    """
    Extracts Requested_area from LLM output
    and converts it into coordinates.
    """
    llm_output = state.get("response", "")

    match = re.search(
        r'Requested_area\s*=\s*"([^"]+)"',
        llm_output
    )

    if not match:
        print("[geocode_node] No Requested_area found in LLM output.")
        return {
            "forecast_error": "Could not extract location name from LLM response."
        }

    area_name = match.group(1).strip()

    if not area_name or area_name.upper() == "UNKNOWN":
        return {
            "forecast_error": "No valid location name was provided."
        }

    try:
        coordinates = geocode_location(area_name)
    except Exception as e:
        return {
            "forecast_error": f"Geocoding failed for '{area_name}': {e}"
        }

    return {
        "area_name": area_name,
        "latitude": coordinates["lat"],
        "longitude": coordinates["lon"]
    }


def fetch_smap_value(date: str, lat: float, lon: float, headers: dict):
    """
    Fetches the nearest SMAP SPL3SMP soil moisture value for a given date and location.
    Returns a float or None if data is unavailable.
    """
    start = f"{date}T00:00:00Z"
    end   = f"{date}T23:59:59Z"

    try:
        r = requests.get(
            "https://cmr.earthdata.nasa.gov/search/granules.json",
            params={
                "short_name": "SPL3SMP",
                "version": "009",
                "temporal": f"{start},{end}",
                "page_size": 1,
            },
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()

        entries = r.json().get("feed", {}).get("entry", [])
        if not entries:
            return None

        h5_url = next(
            (lnk["href"] for lnk in entries[0].get("links", [])
             if lnk.get("href", "").endswith(".h5")),
            None,
        )
        if not h5_url:
            return None

        resp = requests.get(h5_url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

    
        with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in resp.iter_content(8192):
                tmp.write(chunk)

        try:
            with h5py.File(tmp_path, "r") as f:
                g    = f["Soil_Moisture_Retrieval_Data_AM"]
                sm   = g["soil_moisture"][:]
                lats = g["latitude"][:]
                lons = g["longitude"][:]
                fill = float(g["soil_moisture"].attrs.get("_FillValue", -9999.0))

                dist = np.sqrt((lats - lat) ** 2 + (lons - lon) ** 2)
                dist[sm == fill] = np.inf

                row, col = np.unravel_index(np.argmin(dist), dist.shape)
                value = float(sm[row, col])

                return None if value == fill else round(value, 4)
        finally:
            os.remove(tmp_path)   

    except Exception as e:
        print(f"[SMAP] Error for {date}: {e}")
        return None


def get_farm_context(lat: float, lon: float, token: str,
                     past_days: int = 7, future_days: int = 7) -> dict:
    """
    Fetches past SMAP soil moisture and Open-Meteo forecast data
    for the given coordinates and assembles an LLM-ready context string.
    """
    headers = {"Authorization": f"Bearer {token}"}

    past_smap: dict = {}
    for i in range(past_days, 0, -1):
        date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        print(f"[SMAP] Fetching {date} …")
        past_smap[date] = fetch_smap_value(date, lat, lon, headers)

    
    weather_response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":   lat,
            "longitude":  lon,
            "daily": (
                "soil_moisture_0_to_7cm_mean,"
                "precipitation_sum,"
                "temperature_2m_max"
            ),
            "timezone":      "auto",
            "past_days":     past_days,
            "forecast_days": future_days,
        },
        timeout=30,
    )
    weather_response.raise_for_status()
    weather = weather_response.json()["daily"]

    def moisture_label(v) -> str:
        if v is None:
            return "no data"
        if v < 0.20:
            return "dry"
        if v < 0.35:
            return "optimal"
        return "wet"

    
    today       = datetime.now().date()
    past_lines  = []
    future_lines = []

    for idx, d in enumerate(weather["time"]):
        current     = datetime.strptime(d, "%Y-%m-%d").date()
        rain        = weather["precipitation_sum"][idx]
        temp        = weather["temperature_2m_max"][idx]
        sm_forecast = weather["soil_moisture_0_to_7cm_mean"][idx]

        
        rain_str = f"{rain}mm" if rain is not None else "no data"
        temp_str = f"{temp}C"  if temp is not None else "no data"

        if current < today:
            smap = past_smap.get(d)
            past_lines.append(
                f"{d}: SMAP={smap} ({moisture_label(smap)}) | "
                f"Rain={rain_str} | Temp={temp_str}"
            )
        else:
            sm_str = str(sm_forecast) if sm_forecast is not None else "no data"
            future_lines.append(
                f"{d}: Soil Moisture Forecast={sm_str} "
                f"({moisture_label(sm_forecast)}) | "
                f"Rain={rain_str} | Temp={temp_str}"
            )

    llm_input = (
        f"Location: {lat}N, {lon}E\n\n"
        f"PAST {past_days} DAYS\n"
        f"{chr(10).join(past_lines)}\n\n"
        f"NEXT {future_days} DAYS\n"
        f"{chr(10).join(future_lines)}\n"
    )

    return {
        "past_smap": past_smap,
        "weather":   weather,
        "llm_input": llm_input,
    }


def classify_ndvi(mean_ndvi: float) -> str:
    if mean_ndvi < 0.2:
        return "Bare Soil"
    if mean_ndvi < 0.4:
        return "Sparse Vegetation"
    if mean_ndvi < 0.6:
        return "Healthy Vegetation"
    if mean_ndvi < 0.8:
        return "Dense Vegetation"
    return "Very Dense Vegetation"


def calculate_ndvi_for_location(
    lat: float,
    lon: float,
    datetime_range: str = "2026-06-01/2026-06-30",
    window_size: int = 256,
) -> dict:
    catalog = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1"
    )

    search = catalog.search(
        collections=["sentinel-2-l2a"],
        intersects={
            "type": "Point",
            "coordinates": [lon, lat],
        },
        datetime=datetime_range,
        limit=10,
    )

    items = list(search.items())
    if not items:
        raise ValueError("No Sentinel-2 images found for this location and date range.")

    best_item = min(
        items,
        key=lambda item: item.properties.get("eo:cloud_cover", 100),
    )

    item = planetary_computer.sign(best_item)
    red_url = item.assets["B04"].href
    nir_url = item.assets["B08"].href

    with rasterio.open(red_url) as red, rasterio.open(nir_url) as nir:
        x, y = transform("EPSG:4326", red.crs, [lon], [lat])
        row, col = red.index(x[0], y[0])

        half = window_size // 2
        window = Window(
            col_off=max(col - half, 0),
            row_off=max(row - half, 0),
            width=min(window_size, red.width),
            height=min(window_size, red.height),
        )

        red_band = red.read(1, window=window).astype(np.float32)
        nir_band = nir.read(1, window=window).astype(np.float32)

    ndvi = (nir_band - red_band) / (nir_band + red_band + 1e-10)
    ndvi = np.where((ndvi >= -1) & (ndvi <= 1), ndvi, np.nan)

    mean_ndvi = float(np.nanmean(ndvi))
    if np.isnan(mean_ndvi):
        raise ValueError("NDVI could not be calculated because all pixels were invalid.")

    return {
        "image_id": best_item.id,
        "cloud_cover": best_item.properties.get("eo:cloud_cover"),
        "datetime_range": datetime_range,
        "mean_ndvi": round(mean_ndvi, 3),
        "vegetation_status": classify_ndvi(mean_ndvi),
    }


def ndvi_node(state: AgentState) -> dict:
    """
    Uses latitude and longitude from geocode_node to calculate Sentinel-2 NDVI.
    Runs independently from forecasting_node after geocoding succeeds.
    """
    if state.get("forecast_error"):
        return {}

    lat = state.get("latitude")
    lon = state.get("longitude")

    if lat is None or lon is None:
        return {
            "ndvi_error": "Latitude and longitude are required before ndvi_node runs."
        }

    try:
        return {"ndvi": calculate_ndvi_for_location(lat=lat, lon=lon)}
    except Exception as e:
        return {"ndvi_error": str(e)}


def forecasting_node(state: AgentState) -> dict:
    """
    Uses latitude and longitude from geocode_node to fetch farm context data.
    Skips gracefully if geocode already set a forecast_error.
    """
    if state.get("forecast_error"):
        return {} 

    lat = state.get("latitude")
    lon = state.get("longitude")

    if lat is None or lon is None:
        return {
            "forecast_error": (
                "Latitude and longitude are required before "
                "forecasting_node runs."
            )
        }

    nasa_token = os.getenv("NASA_TOKEN")
    if not nasa_token:
        return {
            "forecast_error": (
                "NASA_TOKEN is not set. "
                "Add it to your .env before running."
            )
        }

    try:
        return get_farm_context(lat=lat, lon=lon, token=nasa_token)
    except Exception as e:
        return {"forecast_error": str(e)}

def advisory_node(state: AgentState) -> dict:
    """
    Synthesizes SMAP, weather, and NDVI data into farm advisory
    using Azure AI Foundry GPT-4.1-mini.
    """
    if state.get("forecast_error") and state.get("ndvi_error"):
        return {"advisory": "Could not generate advisory due to upstream data errors."}

    area   = state.get("area_name", "the requested location")
    llm_in = state.get("llm_input", "No forecast data available.")
    ndvi   = state.get("ndvi", {})

    ndvi_summary = (
        f"NDVI: {ndvi.get('mean_ndvi')} → {ndvi.get('vegetation_status')} "
        f"(image: {ndvi.get('image_id')}, cloud cover: {ndvi.get('cloud_cover')}%)"
        if ndvi else state.get("ndvi_error", "NDVI data unavailable.")
    )

    user_message = f"""
Location: {area}

=== WEATHER & SOIL MOISTURE (Past 7 Days + Forecast) ===
{llm_in}

=== VEGETATION INDEX (Sentinel-2 NDVI) ===
{ndvi_summary}

Based on the above data provide a practical farm advisory in this exact format:
- Irrigation: should the farmer irrigate, wait, or reduce watering?
- Crop risk: any heat stress, flood risk, or drought risk?
- Vegetation health: is the crop healthy or struggling?
- Action this week: one specific thing the farmer should do right now.
- Next 7 days outlook: brief summary.
Keep language simple and actionable.
"""

    try:
        from openai import OpenAI

        client = OpenAI(
            base_url="https://zoro-agro-assistant-resource.openai.azure.com/openai/v1",
            api_key=os.getenv("AZURE_FOUNDRY_KEY"),
        )

        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert agricultural advisor for smallholder "
                        "farmers in India. Be concise and practical."
                    )
                },
                {
                    "role": "user",
                    "content": user_message
                },
            ],
            temperature=0.3,
            max_tokens=600,
        )

        advice = response.choices[0].message.content
        return {"advisory": advice}

    except Exception as e:
        return {"advisory": f"Advisory generation failed: {type(e).__name__}: {e}"}

graph = StateGraph(AgentState)

graph.add_node("input_node",           input_node)
graph.add_node("Decision_Maker_LLM_1", llm_node)
graph.add_node("geocode_node",         geocode_node)
graph.add_node("forecasting_node",     forecasting_node)
graph.add_node("ndvi_node",            ndvi_node)
graph.add_node("advisory_node",        advisory_node)    

graph.add_edge(START, "input_node")
graph.add_edge("input_node", "Decision_Maker_LLM_1")

graph.add_conditional_edges(
    "Decision_Maker_LLM_1",
    route_after_llm,
    {
        "geocode_node": "geocode_node",
        "end":          END,
    }
)

graph.add_edge("geocode_node",     "forecasting_node")
graph.add_edge("geocode_node",     "ndvi_node")
graph.add_edge("forecasting_node", "advisory_node")   
graph.add_edge("ndvi_node",        "advisory_node")   
graph.add_edge("advisory_node",    END)               

app = graph.compile()


if __name__ == "__main__":
    state = {
        "user_input": input("Enter your prompt: ")
    }

    result = app.invoke(state)

    print("\n----- LLM RESPONSE -----")
    print(result.get("response"))

    print("\n----- LOCATION INFO -----")
    print("Area      :", result.get("area_name", "N/A"))
    print("Latitude  :", result.get("latitude",  "N/A"))
    print("Longitude :", result.get("longitude", "N/A"))

    print("\n----- FORECASTING DATA -----")
    if result.get("forecast_error"):
        print("Error:", result.get("forecast_error"))
    else:
        print(result.get("llm_input", "No forecast data available."))

    print("\n----- NDVI DATA -----")
    if result.get("ndvi_error"):
        print("Error:", result.get("ndvi_error"))
    else:
        print(result.get("ndvi", "No NDVI data available."))
    
    print("\n----- FARM ADVISORY (Azure AI Foundry) -----")
    if result.get("advisory"):
        print(result.get("advisory"))
    else:
        print("No advisory generated.")