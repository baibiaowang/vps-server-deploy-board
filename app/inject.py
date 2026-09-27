"""
服务端注入层（需求①：保持 web/dashboard.html 与线上原版【字节一致】）

要做三件原版没有的事，但又要保证磁盘上的 dashboard.html 一个字节都不改，
所以全部走「响应期注入」：

  1. 手动更新按钮（你在看板里要的那一个）
     —— 复用原版 CSS 里已有但没用上的 .dbtn / .datefilter / .dflabel 类，
        所以外观与原生按钮完全一致，不需要新增一行 CSS。
  2. <title> 的日期范围（原版是生成时写死在 HTML 里的）
  3. 分类颜色补齐（可选，默认关闭 —— 会改变显示，尊重"别动我布局"）
"""
from __future__ import annotations

import json
from typing import Optional

# ---------------- 手动更新 / 补漏重扫 ----------------
# 用原版已有的 .dbtn / .datefilter / .dflabel，视觉与原生筛选按钮完全同源
#
# 两个按钮的分工（这是本次新增的核心）：
#   🔄 更新数据   → incremental，只扫最近几天。日常用，几秒完事。
#   ↻ 补漏重扫   → full + 回看 N 天（默认 90≈3 个月）。慢，但会把漏掉的日子重拉一遍。
#                   幂等：ann_id 唯一键 + ON CONFLICT DO NOTHING，
#                   已存在的跳过，只补真正缺的 —— 不用先清空/重置，也不会重复。
UPDATE_BTN_HTML = """<div class="datefilter" id="__updbox" style="gap:8px;">
  <button class="dbtn" id="__updbtn" type="button" title="拉取最新公告（只扫最近几天，快）">🔄 更新数据</button>
  <button class="dbtn" id="__bfbtn" type="button" title="重新拉取最近 {days} 天，把漏掉的日子补回来。幂等：已存在的跳过，只补缺失的，不会重复也不会清空">↻ 补漏重扫({days}天)</button>
  <span class="dflabel" id="__updtxt" style="font-weight:400;color:#6b7280;font-size:12px;"></span>
</div>
"""

UPDATE_BTN_JS = """
<script>
(function(){
  var btn=document.getElementById('__updbtn'),
      bf =document.getElementById('__bfbtn'),
      txt=document.getElementById('__updtxt');
  if(!btn||!txt) return;
  var busy=false;
  function set(t){ txt.textContent=t; }
  function lock(on){
    busy=on;
    [btn,bf].forEach(function(b){ if(!b) return; b.disabled=on; b.style.opacity=on?.6:1; });
  }
  function done(){ lock(false); }
  function relayout(){ if(window.chart) try{window.chart.resize()}catch(e){} }

  // 跑完之后顺手做一次遗漏检测 —— 重拉了不代表拉全了，得能证明没洞
  function checkGaps(msg){
    fetch('/api/gaps',{credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(g){
        var tail = (g.verdict==='ok')
          ? ('✓ 无缺口（覆盖'+g.coverage+'% / '+g.trading_days+'个交易日）')
          : ('⚠ 仍有 '+((g.missing_days||[]).length)+' 天缺数据');
        set(msg+' · '+tail+' · 刷新页面查看');
        setTimeout(function(){ location.reload(); }, 1500);
      })
      .catch(function(){
        set(msg+' · 刷新页面查看');
        setTimeout(function(){ location.reload(); }, 1200);
      });
  }

  function poll(verify){
    fetch('/api/runs?limit=1',{credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        var it=(d.items||[])[0]||{};
        if(d.running){ set('更新中…'); setTimeout(function(){poll(verify);},3000); return; }
        done();
        if(it.status==='success'){
          var msg='✓ 新增 '+(it.new_count||0)+' 条 · '+(((it.duration_ms||0)/1000).toFixed(1))+'s';
          if(verify){ set(msg+' · 校验缺口中…'); checkGaps(msg); }
          else { set(msg+' · 刷新页面查看'); setTimeout(function(){ location.reload(); }, 1200); }
        } else if(it.status==='failed'){
          set('✗ 失败，详见 data/update.log');
        } else { set(''); }
      })
      .catch(function(){ set('状态查询失败'); done(); });
  }

  function start(url, body, verify){
    if(busy) return;
    lock(true); set('启动中…');
    fetch(url,{
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    }).then(function(r){ return r.json().catch(function(){ return {}; }); })
      .then(function(d){
        if(d.ok===false){ set(d.error||'无法启动'); done(); return; }
        setTimeout(function(){ poll(verify); },1500);
      })
      .catch(function(){ set('启动失败'); done(); });
  }

  if(btn) btn.onclick=function(){ start('/api/run',{mode:'incremental'},false); };
  if(bf)  bf.onclick=function(){ start('/api/backfill',{days:__BF_DAYS__},true); };

  // 进入页面时回显上次更新状态
  fetch('/api/runs?limit=1',{credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      var it=(d.items||[])[0]; if(!it) return;
      if(d.running){ lock(true); set('更新中…'); setTimeout(function(){poll(false);},3000); return; }
      var t=(it.finished_at||'').slice(5,16).replace('T',' ');
      set('上次更新 '+t+(it.status==='failed'?' ✗':''));
    }).catch(function(){});
  relayout();
})();
</script>
"""


# ---------------- 收藏夹 ----------------
# 全部用 JS 动态创建，不在 HTML 里插结构性节点 —— 这样注入失败时页面完全不受影响。
# 三处入口：
#   1. 工具栏最右侧「⭐ 收藏夹 (N)」按钮 + 下拉面板（列出票、原因、跳查、删除）
#   2. 详情面板标题行的「☆ 收藏 / ★ 已收藏」按钮
#   3. 点收藏后展开的原因输入框（可留空）
FAVORITES_JS = """
<script>
(function(){
  var API='/api/favorites';
  var favs=[];
  var tb=document.querySelector('.toolbar');
  if(!tb) return;

  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  /* ---------- 1. 工具栏最右侧的收藏夹按钮 ---------- */
  var box=document.createElement('div');
  box.className='datefilter';
  box.style.position='relative';
  box.innerHTML=
    '<button class="dbtn" id="__favbtn" type="button" title="我的收藏夹">'
    + '⭐ 收藏夹 <b id="__favcnt">0</b></button>'
    + '<div id="__favpanel" style="display:none;position:absolute;right:0;top:calc(100% + 8px);'
    + 'z-index:9999;width:330px;max-height:400px;overflow-y:auto;background:var(--card);'
    + 'border:1px solid var(--border);border-radius:12px;'
    + 'box-shadow:0 12px 32px rgba(0,0,0,.14);padding:10px 12px;">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">'
        + '<b style="font-size:13px;">我的收藏</b>'
        + '<span id="__favclose" style="cursor:pointer;color:#9ca3af;font-size:15px;">×</span>'
      + '</div>'
      + '<div id="__favlist"></div>'
    + '</div>';
  // ★ 必须插在 .filters 之前，不能 append 到末尾：
  //   .toolbar 是 flex-wrap:wrap，而 .filters 宽达 1540px 会把整行占满并换行，
  //   实测 append 到末尾时按钮被挤到「第 3 行最左边」。
  //   插在 .filters 之前 + margin-left:auto，按钮就能留在【第一行】且被顶到最右。
  var _filters = tb.querySelector('.filters');
  if (_filters) { tb.insertBefore(box, _filters); } else { tb.appendChild(box); }
  box.style.marginLeft = 'auto';

  var panel=document.getElementById('__favpanel');
  var listEl=document.getElementById('__favlist');
  var cntEl=document.getElementById('__favcnt');
  var btn=document.getElementById('__favbtn');

  function findStock(code){
    var arr=null;
    try { arr = (typeof all !== 'undefined') ? all : null; } catch(e){ arr=null; }
    if(!arr) arr = window.ANNO_LIST || [];
    for(var i=0;i<arr.length;i++) if(arr[i].code===code) return arr[i];
    return null;
  }
  function curCode(){
    var el=document.getElementById('p_cd');
    var c=el?String(el.textContent||'').trim():'';
    return (c && c!=='-') ? c : '';
  }

  /* ---------- 2. 详情面板的收藏按钮 ---------- */
  var star=null, note=null;
  var t1=document.querySelector('.panel-head .t1');
  if(t1){
    star=document.createElement('button');
    star.className='dbtn'; star.id='__starbtn'; star.type='button';
    star.style.marginLeft='auto';
    star.textContent='☆ 收藏';
    t1.appendChild(star);

    note=document.createElement('div');
    note.id='__favnote';
    note.style.cssText='display:none;margin-top:8px;gap:6px;align-items:center;flex-wrap:wrap;';
    note.innerHTML=
      '<input id="__favreason" type="text" maxlength="200" placeholder="收藏原因（可留空）" '
      + 'style="flex:1;min-width:170px;padding:6px 10px;border:1px solid var(--border);'
      + 'border-radius:8px;font-size:12.5px;">'
      + '<button class="dbtn" id="__favsave" type="button" '
      + 'style="background:#1e3a8a;color:#fff;border-color:#1e3a8a;">保存</button>'
      + '<button class="dbtn" id="__favcancel" type="button">取消</button>';
    t1.parentNode.appendChild(note);

    star.onclick=function(){
      var code=curCode(); if(!code) return;
      var f=null; for(var i=0;i<favs.length;i++) if(favs[i].code===code) f=favs[i];
      document.getElementById('__favreason').value = f ? (f.reason||'') : '';
      note.style.display='flex';
      document.getElementById('__favreason').focus();
    };
    document.getElementById('__favcancel').onclick=function(){ note.style.display='none'; };
    document.getElementById('__favsave').onclick=save;
    document.getElementById('__favreason').onkeydown=function(e){
      if(e.key==='Enter'){ save(); }
      if(e.key==='Escape'){ note.style.display='none'; }
    };
  }

  function save(){
    var code=curCode(); if(!code) return;
    var s=findStock(code);
    var reasonEl=document.getElementById('__favreason');
    fetch(API,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:code, name:s?(s.name||''):'',
                           reason:reasonEl?reasonEl.value:'', category:s?(s.category||''):''})})
      .then(function(r){ return r.json(); })
      .then(function(){ if(note) note.style.display='none'; load(); })
      .catch(function(){ alert('收藏失败，请重试'); });
  }

  /* ---------- 3. 列表渲染 ---------- */
  function renderFavs(){
    if(!favs.length){
      listEl.innerHTML='<div style="color:#9ca3af;font-size:12px;padding:14px 2px;text-align:center;'
        + 'line-height:1.7;">还没有收藏<br><span style="font-size:11px;">'
        + '在右侧详情面板点「☆ 收藏」即可加入</span></div>';
      return;
    }
    listEl.innerHTML=favs.map(function(f){
      return '<div class="__favrow" data-code="'+esc(f.code)+'" '
        + 'style="padding:8px 4px;border-bottom:1px solid var(--border);cursor:pointer;">'
        + '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
          + '<b style="font-size:13px;">'+esc(f.name||f.code)+'</b>'
          + '<span style="font-size:11px;color:var(--text2);">'+esc(f.code)+'</span>'
          + (f.category ? '<span style="font-size:10px;color:#92400e;background:#fef3c7;'
              + 'padding:1px 6px;border-radius:4px;">'+esc(f.category)+'</span>' : '')
          + '<span class="__favdel" data-code="'+esc(f.code)+'" '
            + 'style="margin-left:auto;color:#dc2626;font-size:12px;padding:0 4px;" title="取消收藏">×</span>'
        + '</div>'
        + '<div style="font-size:11.5px;color:var(--text2);margin-top:3px;line-height:1.5;">'
          + (f.reason ? esc(f.reason) : '<span style="color:#c3c7cf;">（未填写原因）</span>')
        + '</div>'
      + '</div>';
    }).join('');
    listEl.querySelectorAll('.__favrow').forEach(function(r){
      r.onclick=function(e){
        if(e.target && e.target.classList && e.target.classList.contains('__favdel')) return;
        jump(r.dataset.code);
      };
    });
    listEl.querySelectorAll('.__favdel').forEach(function(d){
      d.onclick=function(e){ e.stopPropagation(); del(d.dataset.code); };
    });
  }

  function jump(code){
    var s=findStock(code);
    try{
      if(s && s.category && typeof activeCat !== 'undefined'){ activeCat=s.category; }
      if(typeof renderCats === 'function') renderCats();
      var si=document.getElementById('search'); if(si) si.value='';
      if(typeof renderList === 'function') renderList();
      if(typeof select === 'function') select(code);
    }catch(e){}
    closePanel();
  }

  function del(code){
    fetch(API+'/'+encodeURIComponent(code),{method:'DELETE',credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(){ load(); })
      .catch(function(){});
  }

  function updateStar(){
    if(!star) return;
    var code=curCode();
    if(!code){ star.style.display='none'; return; }
    star.style.display='';
    var f=null; for(var i=0;i<favs.length;i++) if(favs[i].code===code) f=favs[i];
    if(f){
      star.textContent='★ 已收藏';
      star.style.background='#fffbeb'; star.style.borderColor='#f59e0b'; star.style.color='#b45309';
    }else{
      star.textContent='☆ 收藏';
      star.style.background=''; star.style.borderColor=''; star.style.color='';
    }
  }

  function load(){
    fetch(API,{credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){ favs=(d && d.items)||[]; cntEl.textContent=favs.length;
                         renderFavs(); updateStar(); })
      .catch(function(){});
  }

  function closePanel(){ panel.style.display='none'; }
  btn.onclick=function(e){
    e.stopPropagation();
    var open = panel.style.display==='block';
    panel.style.display = open ? 'none' : 'block';
    if(!open) load();
  };
  document.getElementById('__favclose').onclick=closePanel;
  document.addEventListener('click',function(e){
    if(panel.style.display==='block' && !box.contains(e.target)) closePanel();
  });

  // 选中股票变化时同步收藏按钮状态（select() 会改写 #p_cd）
  var cdEl=document.getElementById('p_cd');
  if(cdEl && window.MutationObserver){
    new MutationObserver(function(){
      if(note) note.style.display='none';
      updateStar();
    }).observe(cdEl,{childList:true,characterData:true,subtree:true});
  }
  load();
})();
</script>
"""


def inject_favorites(html: str, enabled: bool = True) -> str:
    """注入收藏夹（工具栏按钮 + 详情面板收藏按钮 + 原因输入）"""
    if not enabled or "</body>" not in html or "__favbtn" in html:
        return html
    return html.replace("</body>", FAVORITES_JS + "</body>", 1)


# ---------------- K线请求去重 ----------------
# 原版 getKline 的缓存存的是【结果】，而 set() 发生在 await 之后 ——
# 两个并发调用会双双穿透缓存，各发一次网络请求（实测首屏就把同一只股票请求了两遍）。
# 改成缓存【Promise】：并发调用共享同一个请求，失败则不缓存（保留原版重试语义）。
_KLINE_OLD = """const _kcache=new Map();
async function getKline(code){
  if(_kcache.has(code)) return _kcache.get(code);
  const url='/api/kline?code='+encodeURIComponent(code)+(_ktok?'&token='+encodeURIComponent(_ktok):'');
  try{
    const r=await fetch(url);
    const d=await r.json();
    const kl=d.klines||[];
    _kcache.set(code,kl);
    return kl;
  }catch(e){ return []; }
}"""

_KLINE_NEW = """const _kcache=new Map();
function getKline(code){
  if(_kcache.has(code)) return _kcache.get(code);
  const p=(async()=>{
    const url='/api/kline?code='+encodeURIComponent(code)+(_ktok?'&token='+encodeURIComponent(_ktok):'');
    try{
      const r=await fetch(url);
      const d=await r.json();
      return d.klines||[];
    }catch(e){ _kcache.delete(code); return []; }
  })();
  _kcache.set(code,p);
  return p;
}"""


def inject_kline_dedup(html: str, enabled: bool = True) -> str:
    """K线并发请求去重（纯性能修复，不改变任何显示）"""
    if not enabled or _KLINE_OLD not in html:
        return html
    return html.replace(_KLINE_OLD, _KLINE_NEW, 1)


def inject_update_button(html: str, enabled: bool = True) -> str:
    """在看板工具栏注入「更新数据 / 补漏重扫」按钮（插在搜索框与筛选区之间）"""
    if not enabled:
        return html
    anchor = '<div class="filters">'
    if anchor not in html or "__updbtn" in html:
        return html
    days = default_backfill_days()
    box = UPDATE_BTN_HTML.replace("{days}", str(days))
    js = UPDATE_BTN_JS.replace("__BF_DAYS__", str(days))
    return html.replace(anchor, box + anchor, 1) \
               .replace("</body>", js + "</body>", 1)


def default_backfill_days() -> int:
    """补漏重扫的回看天数：schedule.backfill.lookback_days，默认 90（≈3 个月）"""
    try:
        from .config import rules_config
        sc = (rules_config().get("schedule") or {})
        return int(((sc.get("backfill") or {}).get("lookback_days")) or 90)
    except Exception:
        return 90


# ---------------- 分类颜色补齐 ----------------
def inject_category_colors(html: str, colors: dict, enabled: bool = False) -> str:
    """
    补齐 / 修正 CAT_COLORS。

    原版的坑：CAT_COLORS 只映射了 7 类，且 key 是 "业绩快报"/"立案/诉讼"，
    与实际标签 "业绩预告"/"立案/处罚" 对不上 → 这 4 类分组标题显示为灰色。
    线上 4219 只股票里 1107 只（26%）受影响。

    const 声明的对象本身可以改属性，所以 Object.assign 是合法的：
    注入点必须紧跟在 const CAT_COLORS = {...}; 之后、renderList() 之前。
    """
    if not enabled or not colors:
        return html
    marker = "const CAT_COLORS = {"
    i = html.find(marker)
    if i < 0:
        return html
    j = html.find("};", i)
    if j < 0:
        return html
    at = j + 2
    patch = "\nObject.assign(CAT_COLORS, " + json.dumps(colors, ensure_ascii=False) + ");"
    return html[:at] + patch + html[at:]


# ---------------- 汇总 ----------------
def render_dashboard(html: str, title_range: Optional[dict] = None,
                     category_colors: Optional[dict] = None) -> str:
    """把三件注入一次做完"""
    from .config import rules_config

    disp = (rules_config().get("display") or {})
    out = html
    if disp.get("manual_update_button", True):
        out = inject_update_button(out, True)
    if disp.get("dedupe_kline_requests", True):
        out = inject_kline_dedup(out, True)
    if disp.get("favorites", True):
        out = inject_favorites(out, True)
    if title_range:
        from .board_data import apply_title
        out = apply_title(out, title_range)
    out = inject_category_colors(
        out,
        category_colors or {},
        bool(disp.get("patch_category_colors", False)),
    )
    return out