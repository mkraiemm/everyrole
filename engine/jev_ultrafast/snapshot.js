(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['file','hidden'].includes(e.type);
  // Password contents never leave the page: report only whether the field is filled.
  const secret = e => e.type==='password';
  const shown = e => secret(e) ? (e.value ? '[filled]' : '') : e.value;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel','password'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),secret(e)?e.value.length:e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),(secret(e)?e.value.length:e.value)??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  // Same-origin iframes (an embedded sign-in form, an editor) are part of the page: their controls are listed
  // with page coordinates. A cross-origin frame cannot be read and stays out.
  const frames=[{doc:document,x:0,y:0}];
  for (let i=0;i<frames.length && frames.length<12;i++) for (const f of frames[i].doc.querySelectorAll('iframe,frame')) {
    let inner=null;
    try { inner=f.contentDocument; } catch (err) { inner=null; }
    if (!inner?.body || !visible(f)) continue;
    const fr=f.getBoundingClientRect();
    frames.push({doc:inner,x:frames[i].x+fr.x+f.clientLeft,y:frames[i].y+fr.y+f.clientTop,frame:f});
  }
  for (const {doc,x:dx,y:dy,frame} of frames) for (const e of doc.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const lr=e.getBoundingClientRect(), r={x:lr.x+dx,y:lr.y+dy,width:lr.width,height:lr.height};
    const x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    // Something else is drawn over its middle (a cookie banner, a chat button, a backdrop): a click would land on
    // that instead. Say what covers it, so the choice is to deal with the overlay (or scroll), not to retry.
    const onPage=document.elementFromPoint(x,y);
    const top=frame ? (onPage===frame || frame.contains(onPage) ? doc.elementFromPoint(x-dx,y-dy) : onPage) : onPage;
    if (top && !e.contains(top) && !top.contains(e)) {
      const layer=[...function*(n){ for (; n && n!==document.body; n=n.parentElement) yield n; }(top)]
        .reverse().find(n=>['fixed','sticky'].includes(getComputedStyle(n).position)) || top;
      base.covered_by=(layer.innerText||layer.getAttribute('aria-label')||layer.tagName).replace(/\s+/g,' ').trim().slice(0,80);
    }
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(shown(e)) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
      // P12: single-line inputs that act on Enter (search, add item, chat) need a way to press it
      if (editable && e.tagName==='INPUT') actions.push({...base,kind:'submit',value,label:'Press Enter in '+base.label});
    }
  }
  const words=[]; let length=0;
  for (const {doc,x:dx,y:dy} of frames) {
  const walker=doc.createTreeWalker(doc.body,NodeFilter.SHOW_TEXT), range=doc.createRange(); let node;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const lr=range.getBoundingClientRect();
    const r={width:lr.width,height:lr.height,top:lr.top+dy,bottom:lr.bottom+dy,left:lr.left+dx,right:lr.right+dx};
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  }
  // Apps often scroll an inner panel (a dashboard's main area) while the document itself never scrolls: then the
  // largest visible scrollable panel is what "Scroll down" moves, so content below its fold can be reached.
  const pageScrolls=document.documentElement.scrollHeight>innerHeight+2;
  let panel=null, panelArea=0;
  if (!pageScrolls) for (const e of document.querySelectorAll('body *')) {
    if (e.scrollHeight<=e.clientHeight+2 || !/(auto|scroll|overlay)/.test(getComputedStyle(e).overflowY)) continue;
    const r=e.getBoundingClientRect();
    const area=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*
      Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0));
    if (area>panelArea) { panel=e; panelArea=area; }
  }
  const scrolled=panel ? panel.scrollTop : scrollY;
  const text=words.join('\n').slice(0,6000), height=panel ? panel.scrollHeight : document.documentElement.scrollHeight;
  const viewport=panel ? panel.clientHeight : innerHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],scrolled];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  // the wheel goes where the scrolling happens: the middle of the scrolling panel, or of the page
  const pr=panel ? panel.getBoundingClientRect() : null;
  const at=pr ? {x:Math.round(Math.max(pr.left,0)+Math.min(pr.width,innerWidth)/2),
                 y:Math.round(Math.max(pr.top,0)+Math.min(pr.height,innerHeight)/2)}
              : {x:Math.round(innerWidth/2),y:Math.round(innerHeight/2)};
  const step=Math.max(200,Math.round(viewport*0.8));
  if (scrolled+viewport<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down: more of the page is below, with elements not listed yet',delta:step,...at});
  if (scrolled>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up: more of the page is above',delta:-step,...at});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrolled,height},actions,marker,page_key,guards,omitted_actions};
})()
