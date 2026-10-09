(function(){
  if (window.__apprail) return;
  window.__apprail = true;
  var ICONS = {
    lighthouse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v2"/><path d="M6 8h12"/><path d="M8 21h8"/><path d="M7 8l1 13"/><path d="M17 8l-1 13"/><path d="M12 8v5"/></svg>',
    terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7l4 4-4 4"/><path d="M12 15h7"/></svg>'
  };
  var APPS = [
    { name: '灯塔面板',  icon: 'lighthouse', url: 'http://127.0.0.1:8720' },
    { name: '知识库终端', icon: 'terminal',   url: 'http://127.0.0.1:8719' }
  ];
  var LS = 'apprail.';
  var w = Math.min(300, Math.max(44, parseInt(localStorage.getItem(LS + 'w'), 10) || 200));
  var dragging = false;
  var css = [
    '#apprail{position:fixed;left:0;top:0;bottom:0;z-index:10001;display:flex;flex-direction:column;gap:3px;padding:10px 0 14px;background:var(--panel-2,var(--panel,#f5f6f8));border-right:1px solid var(--line,#e3e6ea);box-shadow:6px 0 24px rgba(0,0,0,.2);transform:translateX(-105%);transition:transform .18s ease}',
    '#apprail.open{transform:translateX(0)}',
    '#apprail-shade{position:fixed;inset:0;z-index:10000;background:rgba(9,14,20,.42);opacity:0;pointer-events:none;transition:opacity .18s ease}',
    '#apprail-shade.on{opacity:1;pointer-events:auto}',
    '#apprail-hot{position:fixed;left:-8px;top:0;bottom:0;width:26px;z-index:9999}',
    '#apprail-cap{font-size:10px;letter-spacing:.14em;opacity:.5;padding:2px 12px 8px;white-space:nowrap;overflow:hidden;text-align:left}',
    '.apprail-item{display:flex;align-items:center;gap:9px;margin:0 8px;padding:9px 10px;border-radius:8px;border:1px solid transparent;background:none;cursor:pointer;font:inherit;font-size:12.5px;color:var(--ink-2,var(--ink,#333));white-space:nowrap;overflow:hidden;text-align:left}',
    '.apprail-item svg{width:17px;height:17px;flex:0 0 auto}',
    '.apprail-item span:last-child{overflow:hidden;text-overflow:ellipsis}',
    '.apprail-item:hover{background:rgba(127,127,127,.14)}',
    '.apprail-item.cur{background:rgba(127,127,127,.18);color:var(--ink,#111);cursor:default;font-weight:600}',
    '#apprail-grip{position:absolute;top:0;right:-3px;width:7px;height:100%;cursor:col-resize}'
  ].join('\n');
  function el(tag, id){ var n = document.createElement(tag); if (id) n.id = id; return n; }
  var rail = el('aside', 'apprail');
  var shade = el('div', 'apprail-shade');
  var hot = el('div', 'apprail-hot');
  rail.style.width = w + 'px';
  var cap = el('div', 'apprail-cap');
  cap.textContent = '服务';
  rail.appendChild(cap);
  var here = location.origin;
  APPS.forEach(function(a){
    var b = document.createElement('button');
    b.className = 'apprail-item';
    b.title = a.name + '（' + a.url + '）';
    b.innerHTML = ICONS[a.icon] + '<span>' + a.name + '</span>';
    if (here === a.url) {
      b.classList.add('cur');
      b.disabled = true;
    } else {
      b.addEventListener('click', function(){ location.href = a.url; });
    }
    rail.appendChild(b);
  });
  var grip = el('div', 'apprail-grip');
  grip.title = '拖拽调宽';
  grip.addEventListener('mousedown', function(e){
    e.preventDefault();
    dragging = true;
    rail.style.transition = 'none';
    var startX = e.clientX, startW = rail.getBoundingClientRect().width;
    function mv(ev){
      rail.style.width = Math.min(300, Math.max(44, startW + ev.clientX - startX)) + 'px';
    }
    function up(){
      document.removeEventListener('mousemove', mv);
      document.removeEventListener('mouseup', up);
      rail.style.transition = '';
      dragging = false;
      w = rail.getBoundingClientRect().width;
      localStorage.setItem(LS + 'w', String(Math.round(w)));
    }
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
  });
  rail.appendChild(grip);
  function show(){
    rail.classList.add('open');
    shade.classList.add('on');
  }
  function hide(){
    rail.classList.remove('open');
    shade.classList.remove('on');
  }
  hot.addEventListener('mouseenter', show);
  rail.addEventListener('mouseleave', function(){ if (!dragging) hide(); });
  shade.addEventListener('click', hide);
  window.addEventListener('blur', hide);
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') hide(); });
  var style = el('style');
  style.textContent = css;
  document.head.appendChild(style);
  document.body.appendChild(shade);
  document.body.appendChild(hot);
  document.body.appendChild(rail);
})();
