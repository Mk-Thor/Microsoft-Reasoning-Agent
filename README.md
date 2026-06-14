# Zoro — The Agri Assistant

A Next.js (App Router) frontend for the Zoro farm-advisory agent. Two panels:

- **Left — Chat with Zoro.** Shows the **Farm Advisory (Azure AI Foundry)** answer.
  If the advisory is missing or failed, it falls back to the router LLM `response`.
- **Right — Field data.** Three green-themed visuals of the FORECASTING + NDVI data,
  each with hover tooltips:
  1. Soil moisture — observed SMAP + 7-day forecast (with a "today" divider and
     dry/wet reference lines)
  2. Rainfall (bars) + max temperature (line)
  3. Sentinel-2 NDVI vegetation gauge with a marker at the mean reading

## Project layout

```
zoro/
├─ app/
│  ├─ api/chat/route.js       # runs Agent.py, returns structured JSON
│  ├─ components/
│  │  ├─ Charts.js            # recharts visuals (soil, weather, NDVI)
│  │  └─ VisualPanel.js       # arranges the three visuals
│  ├─ globals.css             # nature-green palette + layout
│  ├─ layout.js
│  └─ page.js                 # title bar + chat + visual panels
├─ lib/parseAgent.js          # parses Agent.py's llm_input / ndvi output
├─ agent/Agent.py             # <-- place your Agent.py here (or set AGENT_PATH)
├─ package.json
└─ next.config.js
```

## Setup

```bash
npm install
```

Place your agent at `agent/Agent.py` (or point `AGENT_PATH` at it), and make sure
its Python dependencies and the `.env` it reads (GROQ / NASA_TOKEN / AZURE_FOUNDRY_KEY
etc.) are available to the machine running `next`.

## Run

```bash
# Against the real agent:
npm run dev

# UI demo with sample data (no Python / API keys needed):
MOCK_AGENT=1 npm run dev
```

Open http://localhost:3000.

## Environment variables

| Variable       | Purpose                                                        |
|----------------|----------------------------------------------------------------|
| `AGENT_PATH`   | Absolute path to `Agent.py` (default `./agent/Agent.py`)       |
| `PYTHON_BIN`   | Python executable (default `python3`)                          |
| `MOCK_AGENT`   | Set to `1` to serve sample data instead of running the agent   |

## How the data flows

`POST /api/chat` spawns the agent, calls `app.invoke({user_input})`, and reads back
`response`, `area_name`, `latitude/longitude`, `llm_input`, `forecast_error`,
`ndvi`, `ndvi_error`, and `advisory`. `lib/parseAgent.js` converts the human-readable
`llm_input` block and the `ndvi` dict into the structured series the charts render.

## Production build

```bash
npm run build && npm start
```
