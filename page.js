"use client";

import { useState, useRef, useEffect } from "react";
import VisualPanel from "./components/VisualPanel";

const SUGGESTIONS = [
  "Soil moisture in Thanjavur?",
  "Weather and crop advice for Erode",
  "Should I irrigate my farm in Madurai?",
];

export default function Home() {
  const [messages, setMessages] = useState([]); // {role, text, source}
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [vizData, setVizData] = useState(null);
  const chatRef = useRef(null);

  useEffect(() => {
    if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight;
  }, [messages, loading]);

  async function send(text) {
    const q = (text ?? input).trim();
    if (!q || loading) return;

    setMessages((m) => [...m, { role: "user", text: q }]);
    setInput("");
    setLoading(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: q }),
      });
      const data = await res.json();

      if (!res.ok) {
        setMessages((m) => [
          ...m,
          { role: "bot", text: data.error || "Something went wrong.", source: null },
        ]);
      } else {
        setMessages((m) => [
          ...m,
          { role: "bot", text: data.reply, source: data.advisorySource },
        ]);
        // Update visuals only when we actually have farm data for a location.
        if (data.area || data.llmInput || data.ndvi || data.forecastError || data.ndviError) {
          setVizData(data);
        }
      }
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "bot", text: "Could not reach the assistant. Check the server is running.", source: null },
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="shell">
      <header className="titlebar">
        <div className="leaf-mark">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <path
              d="M12 21c0-6 3-11 9-13-1 7-4 11-9 13Zm0 0C8 17 4 14 3 6c7 1 9 7 9 15Z"
              fill="#fff"
              opacity="0.95"
            />
          </svg>
        </div>
        <div className="title-text">
          <h1>Zoro — The Agri Assistant</h1>
          <p>Satellite + forecast data, turned into practical farm advice</p>
        </div>
      </header>

      <div className="panels">
        {/* LEFT — Chatbot */}
        <section className="panel">
          <div className="panel-head">
            <span className="dot" />
            <h2>Chat with Zoro</h2>
            <span className="sub">Farm advisory</span>
          </div>

          <div className="chat-body" ref={chatRef}>
            {messages.length === 0 && !loading && (
              <div className="empty-chat">
                <div className="big">🌾</div>
                <h3>Namaste! I&apos;m Zoro.</h3>
                <p>
                  Ask me about weather, soil moisture or crop health for any place.
                  I&apos;ll read the satellite and forecast data and tell you what to
                  do this week.
                </p>
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className={`avatar ${m.role}`}>{m.role === "bot" ? "Z" : "You".slice(0, 1)}</div>
                <div>
                  <div className="bubble">{m.text}</div>
                  {m.role === "bot" && m.source && (
                    <span className={`source-tag ${m.source}`}>
                      {m.source === "azure" ? "Azure AI Foundry advisory" : "Assistant response"}
                    </span>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div className="msg bot">
                <div className="avatar bot">Z</div>
                <div className="bubble">
                  <span className="typing"><span /><span /><span /></span>
                </div>
              </div>
            )}
          </div>

          <div className="composer">
            <input
              value={input}
              placeholder="Ask about a place — e.g. soil moisture in Thanjavur"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              disabled={loading}
            />
            <button onClick={() => send()} disabled={loading || !input.trim()}>
              Send
            </button>
          </div>
        </section>

        {/* RIGHT — Visualizations */}
        <section className="panel">
          <div className="panel-head">
            <span className="dot" />
            <h2>Field data</h2>
            <span className="sub">Forecast · NDVI</span>
          </div>
          <div className="viz-body">
            <VisualPanel data={vizData} />
          </div>
        </section>
      </div>
    </div>
  );
}
