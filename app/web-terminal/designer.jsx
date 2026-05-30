/* global React */
/* WEBAGENT — "designer" assistant. A floating chat pill below the header.
   Understands design intents and can actually re-skin the UI, swap the logo
   animation, or launch a game. */

function designerRespond(text, api) {
  const norm = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const lc = " " + norm(text.toLowerCase()) + " ";
  const THEMES = window.THEMES, ORDER = window.THEME_ORDER, ANIMS = window.ANIMS, GAMES = window.GAMES;
  const LIGHT = ["paper", "latte", "solarlight", "rosedawn", "mint", "sorbet", "skylight"];
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
  const label = (id) => THEMES[id].label;

  // explicit theme by name
  for (const id of ORDER) {
    const lab = norm(label(id).toLowerCase());
    if (lc.includes(lab) || lc.includes(" " + id + " ")) {
      return { text: "→ applied " + label(id) + " (" + THEMES[id].tags + "). nice pick.", run: () => api.setTheme(id) };
    }
  }
  // animation by name
  for (const a of ANIMS) {
    if (lc.includes(" " + a.name + " ") || lc.includes(a.id)) {
      return { text: "→ logo set to “" + a.name + "” — " + a.desc + ".", run: () => api.setAnimByName(a.id) };
    }
  }
  // game by name
  for (const g of GAMES) {
    if (lc.includes(g.name.toLowerCase()) || lc.includes(" " + g.id + " ")) {
      return { text: "loading " + g.name + " — have fun. (esc to come back)", run: () => api.openGame(g.id) };
    }
  }
  // intents
  if (/\b(light|lighter|bright|day|paper|airy|clean|pastel)\b/.test(lc)) {
    const id = pick(LIGHT.filter((x) => x !== api.currentTheme)) || "latte";
    return { text: "going lighter ☀  applied " + label(id) + ". there are 7 light sets now — paper, latte, solarized light, rosé dawn, mint, sorbet, daylight.", run: () => api.setTheme(id) };
  }
  if (/\b(dark|darker|night|moody|dim|low.?light)\b/.test(lc)) {
    const id = pick(["tokyonight", "dracula", "ocean", "nord", "catppuccin"]);
    return { text: "dimming the lights 🌙  applied " + label(id) + ".", run: () => api.setTheme(id) };
  }
  if (/\b(surprise|random|whatever|anything|feeling lucky)\b/.test(lc)) {
    const id = pick(ORDER), an = pick(ANIMS);
    return { text: "surprise → " + label(id) + " + “" + an.name + "” logo. bold.", run: () => { api.setTheme(id); api.setAnimByName(an.id); } };
  }
  if (/\b(bored|fun|game|play|arcade)\b/.test(lc)) {
    const g = pick(GAMES);
    return { text: "say less — launching " + g.name + ". " + g.controls + ".", run: () => api.openGame(g.id) };
  }
  if (/\b(colou?r|palette|scheme|vibe|mood|theme)\b/.test(lc)) {
    return { text: "depends on the mood — calm: Nord or Tokyo Night. warm: Gruvbox or Rosé Dawn. high-energy: Synthwave or Cyberpunk. clean: Paper or Latte. name one and i'll apply it." };
  }
  if (/\b(anim|animation|logo|motion|moving|banner)\b/.test(lc)) {
    return { text: "cycling the logo animation. there are 20 — donut, cube, globe, starfield, tunnel, fire, plasma, life… ask for one by name.", run: () => api.cycleAnim() };
  }
  if (/\b(round|corner|radius|softer|sharper)\b/.test(lc)) {
    return { text: "everything's already rounded — pills, cards, the chat. want me to dial the radius softer or sharper? (i can pass that to the build)" };
  }
  if (/\b(hi|hey|hello|yo|sup|help|what can you)\b/.test(lc)) {
    return { text: "hey — i'm your design assistant. i can re-skin the UI (23 themes), swap the logo animation (20), or fire up a game. try “make it light”, “synthwave”, “surprise me”, or “i'm bored”." };
  }
  if (/\b(minimal|simple|plain|less)\b/.test(lc)) {
    return { text: "stripping it back → Paper White, the cleanest set.", run: () => api.setTheme("paper") };
  }
  return { text: "i can change the theme, logo animation, or open a game. e.g. “go lighter”, “rosé dawn”, “plasma logo”, or “play tetris”. what are we going for?" };
}

function DesignerChat({ onClose, api }) {
  const { useState, useRef, useEffect } = React;
  const [msgs, setMsgs] = useState(() => [{ who: "designer", text: "hey — i'm your design assistant. ask me to re-skin the UI, change the logo animation, or open a game. try the chips below." }]);
  const [input, setInput] = useState("");
  const bodyRef = useRef(null);
  const inRef = useRef(null);
  useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight; }, [msgs]);
  useEffect(() => { if (inRef.current) inRef.current.focus(); }, []);

  const sendText = (t) => {
    const text = (t || "").trim(); if (!text) return;
    setInput("");
    setMsgs((m) => [...m, { who: "you", text }]);
    const r = designerRespond(text, api);
    setTimeout(() => {
      setMsgs((m) => [...m, { who: "designer", text: r.text }]);
      if (r.run) setTimeout(r.run, 220);
    }, 280);
  };

  const CHIPS = ["suggest a palette", "make it light", "surprise me", "i'm bored"];
  return (
    <div className="designer-pop">
      <div className="dp-top">
        <span className="dp-name">✎ designer<span className="dp-status">online</span></span>
        <button className="menu-x" onClick={onClose}>[esc]</button>
      </div>
      <div className="dp-body" ref={bodyRef}>
        {msgs.map((m, i) => (
          <div key={i} className={"dp-msg dp-" + m.who}>
            <span className="dp-who">{m.who === "you" ? "you" : "◈"}</span>
            <span className="dp-text">{m.text}</span>
          </div>
        ))}
      </div>
      <div className="dp-chips">
        {CHIPS.map((c) => <button key={c} className="dp-chip" onClick={() => sendText(c)}>{c}</button>)}
      </div>
      <div className="dp-input">
        <span className="dp-prompt">❯</span>
        <input ref={inRef} value={input} placeholder="talk to your designer…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") sendText(input); }} />
        <button className="dp-send" onClick={() => sendText(input)}>send ⏎</button>
      </div>
    </div>
  );
}

Object.assign(window, { DesignerChat });
