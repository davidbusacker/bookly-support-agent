const chatEl = document.getElementById("chat");
const formEl = document.getElementById("form");
const inputEl = document.getElementById("input");
const statusEl = document.getElementById("status");

let sessionId = localStorage.getItem("bookly_session_id") || null;

function appendMessage(role, text, meta) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  div.textContent = text;
  if (meta) {
    const metaEl = document.createElement("span");
    metaEl.className = "meta";
    metaEl.textContent = meta;
    div.appendChild(metaEl);
  }
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function setTyping(on) {
  let el = document.getElementById("typing");
  if (on && !el) {
    el = document.createElement("div");
    el.id = "typing";
    el.className = "typing";
    el.textContent = "Bookly is typing…";
    chatEl.appendChild(el);
    chatEl.scrollTop = chatEl.scrollHeight;
  } else if (!on && el) {
    el.remove();
  }
}

async function sendMessage(text) {
  const trimmed = text.trim();
  if (!trimmed) return;

  appendMessage("user", trimmed);
  inputEl.value = "";
  inputEl.disabled = true;
  formEl.querySelector("button").disabled = true;
  setTyping(true);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: trimmed, session_id: sessionId }),
    });

    if (!res.ok) throw new Error("Request failed");

    const data = await res.json();
    sessionId = data.session_id;
    localStorage.setItem("bookly_session_id", sessionId);

    const metaParts = [];
    if (data.tool_used) metaParts.push(`tool: ${data.tool_used}`);
    if (data.awaiting_slot) metaParts.push(`awaiting: ${data.awaiting_slot}`);
    if (data.llm_mode) metaParts.push(`mode: ${data.llm_mode}`);

    appendMessage("bot", data.reply, metaParts.join(" · ") || undefined);
  } catch (err) {
    appendMessage("bot", "Sorry, something went wrong. Please try again.");
  } finally {
    setTyping(false);
    inputEl.disabled = false;
    formEl.querySelector("button").disabled = false;
    inputEl.focus();
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage(inputEl.value);
});

document.querySelectorAll(".chip").forEach((btn) => {
  btn.addEventListener("click", () => sendMessage(btn.dataset.msg));
});

async function init() {
  try {
    const res = await fetch("/health");
    const data = await res.json();
    statusEl.textContent = data.llm_mode === "openai" ? "Live LLM" : "Mock mode";
  } catch {
    statusEl.textContent = "Offline";
  }

  appendMessage(
    "bot",
    "Hi! I'm Bookly's support assistant. I can help with order status, returns, and policy questions.\n\nSample order IDs: BK-10042, BK-10087, BK-10015"
  );
}

init();
