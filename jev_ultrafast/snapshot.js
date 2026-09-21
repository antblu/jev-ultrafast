(() => {
  if (!document.body) return null;

  const MAX_FRAME_DEPTH = 4;
  const MAX_FRAMES = 20;
  const cache = window.__jevFast ||= {};
  cache.frameIds ||= new WeakMap();
  cache.frameStates ||= new Map();
  cache.documentIds ||= new WeakMap();
  cache.nextFrame ||= 1;
  cache.nextDocument ||= 1;
  cache.frames = new Map();

  const roles = ['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => Boolean(e?.isConnected) &&
    !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
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
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const root=e.getRootNode();
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(root.getElementById?.(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const labelForControl = e => {
    if (!e || e.tagName !== 'INPUT' || !['checkbox', 'radio'].includes(e.type)) return null;
    if (e.labels?.length) {
      for (const label of e.labels) if (visible(label)) return label;
    }
    if (e.id) {
      const label=e.getRootNode().querySelector(`label[for="${CSS.escape(e.id)}"]`);
      if (label && visible(label)) return label;
    }
    const wrapped=e.closest('label');
    return wrapped && visible(wrapped) ? wrapped : null;
  };
  const frameId = frame => {
    if (!frame) return 'f0';
    if (!cache.frameIds.has(frame)) cache.frameIds.set(frame,`f${cache.nextFrame++}`);
    return cache.frameIds.get(frame);
  };
  const documentId = doc => {
    if (!cache.documentIds.has(doc)) cache.documentIds.set(doc,cache.nextDocument++);
    return cache.documentIds.get(doc);
  };
  const frameState = id => {
    if (!cache.frameStates.has(id)) cache.frameStates.set(id,{ids:new WeakMap(),nodes:new Map(),next:1});
    return cache.frameStates.get(id);
  };
  const identity = (ctx,e) => {
    const state=ctx.state;
    if (!state.ids.has(e)) state.ids.set(e,state.next++);
    const id=state.ids.get(e);
    state.nodes.set(id,e);
    return id;
  };
  const rootsFor = doc => {
    const roots=[doc];
    for (let index=0;index<roots.length;index++) {
      for (const element of roots[index].querySelectorAll('*')) {
        if (element.shadowRoot) roots.push(element.shadowRoot);
      }
    }
    return roots;
  };
  const offsetFor = ctx => {
    if (!ctx.frame) return {x:0,y:0};
    const parent=cache.frames.get(ctx.parentId);
    if (!parent || !ctx.frame.isConnected) return null;
    const offset=offsetFor(parent);
    if (!offset) return null;
    const r=ctx.frame.getBoundingClientRect();
    return {x:offset.x+r.x+ctx.frame.clientLeft,y:offset.y+r.y+ctx.frame.clientTop};
  };
  cache.resolve = (id,node) => cache.frames.get(id)?.state.nodes.get(node) || null;
  cache.documentFor = id => cache.frames.get(id)?.doc || null;
  cache.elementFromPoint = (id,x,y) => {
    const doc=cache.documentFor(id);
    let hit=doc?.elementFromPoint(x,y) || null;
    while (hit?.shadowRoot) {
      const nested=hit.shadowRoot.elementFromPoint(x,y);
      if (!nested || nested===hit) break;
      hit=nested;
    }
    return hit;
  };
  cache.topRect = (id,e) => {
    const ctx=cache.frames.get(id);
    const offset=ctx && offsetFor(ctx);
    if (!ctx || !offset || e.ownerDocument!==ctx.doc) return null;
    const r=e.getBoundingClientRect();
    return {x:offset.x+r.x,y:offset.y+r.y,w:r.width,h:r.height};
  };
  cache.guard = (id,e) => {
    if (!e?.isConnected || !visible(e)) return null;
    const ctx=cache.frames.get(id);
    if (!ctx || e.ownerDocument!==ctx.doc) return null;
    const control=e.tagName==='LABEL' && e.control ? e.control : e;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [
      id,identity(ctx,e),documentId(ctx.doc),role(control),name(control)||name(e),
      control.value??null,control.checked??null,control.selectedIndex??null,
      control.readOnly??null,control.matches?.(':disabled')??false,
      control.getAttribute?.('aria-disabled')??null,control.getAttribute?.('aria-expanded')??null,
      control.getAttribute?.('aria-checked')??null,control.getAttribute?.('aria-selected')??null,
      control.getAttribute?.('href')??null,scope?.innerText?.slice(0,6000)||''
    ];
  };
  cache.pageKey = () => [
    performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
      [...cache.frames.values()].map(ctx=>[
      ctx.id,documentId(ctx.doc),
      rootsFor(ctx.doc).flatMap(root=>[...root.querySelectorAll('input,textarea,select')]).filter(safe)
        .map(e=>[identity(ctx,e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])
    ])
  ];

  const actions=[];
  const textParts=[];
  const scanText = (ctx,root,words) => {
    const walker=ctx.doc.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    const range=ctx.doc.createRange();
    let node,length=0;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node);
      const r=range.getBoundingClientRect();
      if (r.width>0 && r.height>0 && r.bottom>0 && r.top<ctx.win.innerHeight &&
          r.right>0 && r.left<ctx.win.innerWidth) {
        words.push(value);
        length+=value.length;
      }
    }
  };
  const scanDocument = (doc,win,frame,parentId,depth) => {
    if (!doc?.body || cache.frames.size>=MAX_FRAMES) return;
    const id=frameId(frame);
    const state=frameState(id);
    const ctx={id,doc,win,frame,parentId,state,depth};
    cache.frames.set(id,ctx);
    for (const [nodeId,e] of state.nodes) {
      if (!e.isConnected || e.ownerDocument!==doc) state.nodes.delete(nodeId);
    }

    const roots=rootsFor(doc);
    const words=[];
    for (const root of roots) for (const e of root.querySelectorAll(selector)) {
      if (!safe(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
      if (e.tagName==='INPUT' && ['checkbox','radio'].includes(e.type)) {
        const composite=e.closest(`[role="${e.type}"]`);
        if (composite && composite!==e && visible(composite)) continue;
      }
      const rname=role(e);
      if (!rname) continue;
      let target=e;
      if (e.tagName==='INPUT' && ['checkbox','radio'].includes(e.type) && !visible(e)) {
        target=labelForControl(e);
        if (!target) continue;
      }
      if (!visible(target)) continue;
      const local=target.getBoundingClientRect();
      const x=local.x+local.width/2, y=local.y+local.height/2;
      if (local.width<=0 || local.height<=0 || x<0 || y<0 ||
          x>=win.innerWidth || y>=win.innerHeight) continue;
      const rect=cache.topRect(id,target);
      if (!rect || rect.x+rect.w<=0 || rect.y+rect.h<=0 ||
          rect.x>=innerWidth || rect.y>=innerHeight) continue;
      if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
      const base={frame_id:id,node:identity(ctx,target),role:rname,
        label:name(e)||name(target)||rname,rect};
      for (const key of ['checked','selected','expanded']) {
        const value=e.getAttribute('aria-'+key);
        if (value!==null) base[key]=value;
      }
      if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
      if (e.tagName==='SELECT') {
        for (const option of e.options) {
          if (!option.selected && !option.disabled && !option.closest('optgroup[disabled]')) {
            actions.push({...base,kind:'select',value:option.value,
              current_value:[...e.selectedOptions].map(o=>o.label).join(', '),
              label:base.label+' → '+option.label});
          }
        }
      } else {
        const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
          (['textbox','searchbox','spinbutton'].includes(rname) ||
            (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
        const value='value' in e ? String(e.value) :
          e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
        actions.push({...base,kind:editable?'fill':'click',value});
        if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
      }
    }
    for (const root of roots) scanText(ctx,root,words);
    const frameText=words.join('\n').slice(0,6000);
    if (frameText) textParts.push(id==='f0' ? frameText : `[Embedded content]\n${frameText}`);
    if (depth>=MAX_FRAME_DEPTH) return;
    for (const child of roots.flatMap(root=>[...root.querySelectorAll('iframe')])) {
      if (cache.frames.size>=MAX_FRAMES || !visible(child)) continue;
      const r=child.getBoundingClientRect();
      if (r.width<=2 || r.height<=2 || r.right<=0 || r.bottom<=0 ||
          r.left>=win.innerWidth || r.top>=win.innerHeight) continue;
      let childDoc,childWin;
      try {
        childDoc=child.contentDocument;
        childWin=child.contentWindow;
        if (!childDoc?.body || !childWin || childDoc.location.origin!==doc.location.origin) continue;
      } catch (_) {
        continue;
      }
      scanDocument(childDoc,childWin,child,id,depth+1);
    }
  };

  scanDocument(document,window,null,null,0);
  const text=textParts.join('\n').slice(0,6000);
  const page_key=cache.pageKey(), guards={};
  for (const action of actions) {
    const key=`${action.frame_id}:${action.node}`;
    if (!(key in guards)) guards[key]=cache.guard(action.frame_id,cache.resolve(action.frame_id,action.node));
  }
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((action,index)=>action.id='e'+(index+1));
  const height=document.documentElement.scrollHeight;
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
