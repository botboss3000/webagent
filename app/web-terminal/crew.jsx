/* global React */
/* WEBAGENT — crew: little ASCII characters that stroll, dance, and
   get to work (hammering) while the agent is thinking.
   Each sprite is 3 lines tall, consistent width per character.        */

window.CREW = [
  { id: "robot", name: "Robot", color: "secondary", float: false,
    walk:  [["[o--o]","|████|"," /  \\"], ["[o--o]","|████|"," \\  /"]],
    dance: [["\\o--o/","|████|"," |  |"], ["/o--o\\","|████|"," |  |"]],
    work:  [["[o--o]","|████|"," |  |╾"], ["[o--o]","|████|","╼|  |"]],
  },
  { id: "cat", name: "Cat", color: "tool", float: false,
    walk:  [["/\\_/\\","(o.o)"," n n "], ["/\\_/\\","(o.o)"," u u "]],
    dance: [["/\\_/\\","(^.^)","\\u u/"], ["/\\_/\\","(^.^)","/u u\\"]],
    work:  [["/\\_/\\","(o.o)"," u u╾"], ["/\\_/\\","(o.o)","╼u u "]],
  },
  { id: "dog", name: "Dog", color: "primary", float: false,
    walk:  [[" /^^\\","( oo)"," n  n"], [" /^^\\","( oo)"," u  u"]],
    dance: [["\\/^^\\","(^ ^)","\\u  u"], ["/^^\\/","(^ ^)","u  u/"]],
    work:  [[" /^^\\","( oo)"," u  u╾"], [" /^^\\","( oo)","╼u  u"]],
  },
  { id: "ghost", name: "Ghost", color: "secondary", float: true,
    walk:  [[" .-. ","(o o)"," ‿‿‿ "], [" .-. ","(o o)"," ~~~ "]],
    dance: [[" .-. ","(O O)","\\‿‿/"], [" .-. ","(O O)","/‿‿\\"]],
    work:  [[" .-. ","(o o)"," ‿‿‿╾"], [" .-. ","(o o)","╼‿‿‿ "]],
  },
  { id: "dino", name: "Dino", color: "success", float: false,
    walk:  [["  __/"," (o )","_/ n "], ["  __/"," (o )","_/ u "]],
    dance: [[" \\__/"," (^ )","_/ | "], ["  __/"," (^ )"," \\_| "]],
    work:  [["  __/"," (o )","_/ |╾"], ["  __/"," (o )","╼/ | "]],
  },
  { id: "wizard", name: "Wizard", color: "primary", float: false,
    walk:  [["  /\\ ","(•‿•)"," /||\\"], ["  /\\ ","(•‿•)"," \\||/"]],
    dance: [["\\ /\\ ","(•‿•)"," /||\\"], ["  /\\ /","(•‿•)"," /||\\"]],
    work:  [["  /\\ ","(•‿•)"," /||\\╾"], ["  /\\ ","(•‿•)","╼/||\\"]],
  },
  { id: "bird", name: "Bird", color: "tool", float: true,
    walk:  [["       ","<('< )","  ^^^ "], ["       ","( >')>","  ^^^ "]],
    dance: [["  \\    ","<(^v^)>","  ^^^ "], ["    /  ","<(^v^)>","  ^^^ "]],
    work:  [["       ","<('< )","  ^^^╾"], ["       ","( >')>","╼ ^^^ "]],
  },
  { id: "slime", name: "Slime", color: "success", float: false,
    walk:  [["     ","(•▿•)","‿___‿"], ["     "," (•▿•)","‿__‿ "]],
    dance: [["  ^  ","(•▿•)","\\___/"], ["  ^  ","(^▿^)","/___\\"]],
    work:  [["     ","(•▿•)","‿__‿╾"], ["     ","(•▿•)","╼__‿ "]],
  },
];

function CrewMenu({ active, onToggle, onClose }) {
  return (
    <div className="menu-scrim" onClick={onClose}>
      <div className="menu" onClick={(e) => e.stopPropagation()}>
        <div className="menu-top"><span>┤ crew · pick your companions ├</span>
          <button className="menu-x" onClick={onClose}>[esc]</button></div>
        <div className="menu-list">
          {window.CREW.map((c) => {
            const on = active.includes(c.id);
            return (
              <button key={c.id} className={"menu-row crew-row " + (on ? "sel" : "")} onClick={() => onToggle(c.id)}>
                <span className="crew-check">{on ? "[x]" : "[ ]"}</span>
                <span className="crew-prev" style={{ color: "var(--" + c.color + ")" }}>{c.walk[0][1]}</span>
                <span className="menu-label">{c.name}</span>
                <span className="menu-tag">{c.float ? "floats" : "walks"}</span>
              </button>
            );
          })}
        </div>
        <div className="menu-foot">they stroll &amp; dance — and get to work while the agent thinks · saved locally</div>
      </div>
    </div>
  );
}

function CrewStage({ active, thinking }) {
  const { useRef, useReducer, useEffect } = React;
  const stRef = useRef({});
  const [, bump] = useReducer((x) => (x + 1) % 1e6, 0);

  // ensure a state object per active character
  active.forEach((id, i) => {
    if (!stRef.current[id]) stRef.current[id] = { x: 0.08 + i * 0.13, dir: i % 2 ? -1 : 1, frame: 0, mode: "walk", danceT: 0 };
  });
  Object.keys(stRef.current).forEach((id) => { if (!active.includes(id)) delete stRef.current[id]; });

  useEffect(() => {
    const iv = setInterval(() => {
      const s = stRef.current;
      Object.keys(s).forEach((id) => {
        const c = s[id];
        c.frame = (c.frame + 1) % 2;
        if (thinking) {
          c.mode = "work";
          // gather toward the workbench in the middle
          c.x += (0.46 - c.x) * 0.04;
        } else if (c.mode === "dance") {
          c.danceT -= 1; if (c.danceT <= 0) c.mode = "walk";
        } else {
          c.mode = "walk";
          if (Math.random() < 0.01) { c.mode = "dance"; c.danceT = 10 + (Math.random() * 10 | 0); }
          else {
            c.x += c.dir * 0.012;
            if (c.x <= 0.02) { c.x = 0.02; c.dir = 1; }
            if (c.x >= 0.9) { c.x = 0.9; c.dir = -1; }
          }
        }
      });
      bump();
    }, 150);
    return () => clearInterval(iv);
  }, [thinking]);

  return (
    <div className={"crew-stage" + (thinking ? " working" : "")}>
      <div className="crew-label">{thinking ? "crew: working…" : "crew"}</div>
      {active.map((id) => {
        const c = window.CREW.find((x) => x.id === id); if (!c) return null;
        const cs = stRef.current[id]; if (!cs) return null;
        const frames = c[cs.mode] || c.walk;
        const fr = frames[cs.frame % frames.length];
        const bob = c.float ? Math.sin(Date.now() / 240 + cs.x * 10) * 2 : 0;
        return (
          <div key={id} className="crew-sprite" style={{ left: (cs.x * 100) + "%", color: "var(--" + c.color + ")", transform: "translateY(" + bob + "px)" }}>
            {cs.mode === "work" && <span className="crew-spark">✦</span>}
            {cs.mode === "dance" && <span className="crew-note">♪</span>}
            <pre className="crew-art">{fr.join("\n")}</pre>
          </div>
        );
      })}
      {active.length === 0 && <div className="crew-empty">no crew — add companions from the ☺ crew menu</div>}
      <div className="crew-ground" />
    </div>
  );
}

Object.assign(window, { CrewMenu, CrewStage });
