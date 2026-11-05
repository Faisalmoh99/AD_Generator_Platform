// Demo recording: a visible cursor that follows the mouse, and a subtle click ripple.
(() => {
  const install = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div');
    c.id = '__cur';
    c.innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24"><path d="M3 2 L3 19.5 L7.8 15 L11.2 22.4 L14.6 20.9 L11.2 13.6 L17.8 13.6 Z" fill="#ffffff" stroke="#111111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position: 'fixed', left: (window.__cx || -40) + 'px', top: (window.__cy || -40) + 'px',
      zIndex: 2147483647, pointerEvents: 'none', transform: 'translate(-3px,-2px)',
      filter: 'drop-shadow(0 1px 1px rgba(0,0,0,.6))'});
    document.documentElement.appendChild(c);
    const st = document.createElement('style');
    st.textContent = '@keyframes __rip{from{transform:translate(-50%,-50%) scale(.25);opacity:.7}to{transform:translate(-50%,-50%) scale(1);opacity:0}}' +
      '.__rip{position:fixed;width:46px;height:46px;border-radius:50%;border:2px solid #e3a33f;pointer-events:none;z-index:2147483646;animation:__rip .55s ease-out forwards}';
    document.documentElement.appendChild(st);
    document.addEventListener('mousemove', (e) => { window.__cx = e.clientX; window.__cy = e.clientY;
      c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    document.addEventListener('mousedown', (e) => { const r = document.createElement('div'); r.className = '__rip';
      r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.documentElement.appendChild(r);
      setTimeout(() => r.remove(), 650); }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
})();
