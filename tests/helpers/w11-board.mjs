import * as board from '../../web/js/board.js';
export const tick = () => new Promise(resolve => setImmediate(resolve));
export const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };
export class Element {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.style={}; this.listeners={}; this.attributes={}; this.className=''; this._text=''; this.checked=false; this.files=[]; }
  set textContent(v) {this.children=[];this._text=String(v);} get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  get value(){return this._value??(this.tagName==='SELECT'?this.children[0]?.value||'':'');} set value(v){this._value=String(v);}
  get classList(){return {add:()=>{},remove:()=>{}};}
  append(...children){this.children.push(...children);children.forEach(c=>c.parentNode=this);}
  replaceChildren(...children){this.children=[];this._text='';this.append(...children);if(this.tagName==='SELECT')this._value=undefined;}
  setAttribute(k,v){this.attributes[k]=v;} addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);}
  dispatch(k){return Promise.all((this.listeners[k]||[]).map(fn=>fn({preventDefault(){},target:this})));}
  focus(){} remove(){this.parentNode.children=this.parentNode.children.filter(c=>c!==this);}
}
export const walk = root => [root,...root.children.flatMap(walk)];
export function mountBoard(overrides={}, state={}) {
  const host=new Element('div'), listeners=new Map(), events=[];
  globalThis.document={head:new Element('head'),createElement:t=>new Element(t),getElementById:id=>id==='cqa-board'?host:null,querySelector:()=>({})};
  const CQA={config:{mock:false},state:{activeConversationId:'a',checklistVersion:1,...state},bus:{on(n,f){if(!listeners.has(n))listeners.set(n,new Set());listeners.get(n).add(f);return ()=>listeners.get(n).delete(f);},emit(n,p){events.push([n,p]);for(const f of listeners.get(n)||[])f(p);}},api:{
    conversations:async()=>[{id:'a',total:80},{id:'b',total:90}],checklists:async()=>[{version:1}],config:async()=>({profile:'cloud',mode:'fast'}),...overrides}};
  const destroy=board.mount(CQA);
  return {host,CQA,events,destroy,find:cls=>walk(host).find(n=>n.className.split(' ').includes(cls))};
}

