(function(){
  if (window.__apprail) return;
  window.__apprail = true;
  var ICONS = {
    lighthouse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v2"/><path d="M6 8h12"/><path d="M8 21h8"/><path d="M7 8l1 13"/><path d="M17 8l-1 13"/><path d="M12 8v5"/></svg>',
    terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7l4 4-4 4"/><path d="M12 15h7"/></svg>',
    board: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="6.5" r="2.6"/><circle cx="5.8" cy="17.5" r="2.2"/><circle cx="18.2" cy="17.5" r="2.2"/><path d="M10.7 8.8L6.9 15.6M13.3 8.8L17.1 15.6"/></svg>'
  };
  // APPS 真值唯一来源：库根 config/apps.json（各面板 server 同源转发 /assets/apps.json，
  // 改一处三面板同步）；下面 DEFAULT_APPS 仅为配置拉取失败时的兜底，勿在此维护数据。
  var DEFAULT_APPS = [
    { name: '灯塔面板',   icon: 'lighthouse', url: 'http://127.0.0.1:8720' },
    { name: '知识库终端', icon: 'terminal',   url: 'http://127.0.0.1:8719' },
    { name: '技能库面板', icon: 'board',      url: 'http://127.0.0.1:8721' }
  ];
  var LS = 'apprail.';
  var w = Math.min(300, Math.max(44, parseInt(localStorage.getItem(LS + 'w'), 10) || 200));
  var dragging = false;
  // 配色自持：内置明暗双套完整 palette（取值同三面板设计令牌），不依赖宿主 CSS 变量命名——
  // 三面板任意页上 rail 表现一致（亮一套/暗一套），明暗判定见下方 themeDark。
  var css = [
    '#apprail{--ar-bg:#FBFAF7;--ar-line:#DFDACD;--ar-ink:#50637A;--ar-ink-cur:#12283F;--ar-accent:#0F5BB5;--ar-hover:rgba(18,40,63,.07);--ar-cur:rgba(18,40,63,.10);--ar-shadow:6px 0 24px rgba(18,40,63,.18);position:fixed;left:0;top:0;bottom:0;z-index:10001;display:flex;flex-direction:column;gap:3px;padding:10px 0 14px;background:var(--ar-bg);border-right:1px solid var(--ar-line);box-shadow:var(--ar-shadow);color:var(--ar-ink);transform:translateX(-105%);transition:transform .18s ease}',
    '#apprail.dark{--ar-bg:#141B24;--ar-line:#27313F;--ar-ink:#8C99A9;--ar-ink-cur:#C7D1DD;--ar-accent:#5C9CEC;--ar-hover:rgba(199,209,221,.09);--ar-cur:rgba(199,209,221,.13);--ar-shadow:6px 0 24px rgba(0,0,0,.5)}',
    '#apprail.open{transform:translateX(0)}',
    '#apprail-shade{position:fixed;inset:0;z-index:10000;background:rgba(9,14,20,.42);opacity:0;pointer-events:none;transition:opacity .18s ease}',
    '#apprail-shade.on{opacity:1;pointer-events:auto}',
    '#apprail-hot{position:fixed;left:-8px;top:0;bottom:0;width:26px;z-index:9999}',
    '#apprail-cap{font-size:10px;letter-spacing:.14em;opacity:.5;padding:2px 12px 8px;white-space:nowrap;overflow:hidden;text-align:left}',
    '.apprail-item{display:flex;align-items:center;gap:9px;margin:0 8px;padding:9px 10px;border-radius:8px;border:1px solid transparent;background:none;cursor:pointer;font:inherit;font-size:12.5px;color:var(--ar-ink);white-space:nowrap;overflow:hidden;text-align:left}',
    '.apprail-item svg{width:17px;height:17px;flex:0 0 auto}',
    '.apprail-item span:last-child{overflow:hidden;text-overflow:ellipsis}',
    '.apprail-item:hover{background:var(--ar-hover)}',
    '.apprail-item.cur{background:var(--ar-cur);color:var(--ar-ink-cur);cursor:default;font-weight:600}',
    '.apprail-item.cur svg{color:var(--ar-accent)}',
    '#apprail-grip{position:absolute;top:0;right:-3px;width:7px;height:100%;cursor:col-resize}'
  ].join('\n');
  // 明暗判定：三面板统一由首帧引导脚本在 <html data-theme="light|dark"> 落位（最通用可探测信号）；
  // 属性缺失时兜底 prefers-color-scheme；运行期监听属性变化 + 系统偏好变化实时跟随。
  function themeDark(){
    var t = document.documentElement.getAttribute('data-theme');
    if(t === 'dark' || t === 'light') return t === 'dark';
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function el(tag, id){ var n = document.createElement(tag); if (id) n.id = id; return n; }
  var rail = el('aside', 'apprail');
  var shade = el('div', 'apprail-shade');
  var hot = el('div', 'apprail-hot');
  rail.style.width = w + 'px';
  function syncTheme(){ rail.classList.toggle('dark', themeDark()); }
  syncTheme();
  new MutationObserver(syncTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  if(window.matchMedia){
    var arMq = window.matchMedia('(prefers-color-scheme: dark)');
    if(arMq.addEventListener) arMq.addEventListener('change', syncTheme);
    else if(arMq.addListener) arMq.addListener(syncTheme);
  }
  var cap = el('div', 'apprail-cap');
  cap.textContent = '服务';
  rail.appendChild(cap);
  var grip = el('div', 'apprail-grip');
  grip.title = '拖拽调宽';
  rail.appendChild(grip);
  var mounted = [];
  function buildApps(APPS){
    mounted.forEach(function(b){ if (b.parentNode) b.parentNode.removeChild(b); });
    mounted = [];
    var here = location.origin;
    APPS.forEach(function(a){
      var b = document.createElement('button');
      b.className = 'apprail-item';
      b.title = a.name + '（' + a.url + '）';
      b.innerHTML = (ICONS[a.icon] || '') + '<span>' + a.name + '</span>';
      if (here === a.url) {
        b.classList.add('cur');
        b.disabled = true;
      } else {
        b.addEventListener('click', function(){ location.href = a.url; });
      }
      rail.insertBefore(b, grip);
      mounted.push(b);
    });
  }
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
  fetch('assets/apps.json', { cache: 'no-store' })
    .then(function(r){ return r.ok ? r.json() : null; }, function(){ return null; })
    .then(function(cfg){
      buildApps(cfg && Array.isArray(cfg.apps) && cfg.apps.length ? cfg.apps : DEFAULT_APPS);
    });
})();
