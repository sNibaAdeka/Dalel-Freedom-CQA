// Run: node --experimental-default-type=module tests/frontend_audio_weights.test.mjs
// Minimal DOM/media harness validates state transitions; it is not a browser layout test.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

class Node {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.style={};this.dataset={};this.attrs={};this.listeners={};
    this._text='';this._cls='';this.hidden=false;this.disabled=false;this.value='';this.width=400;
    this.classList={add:(...xs)=>{this._cls=[...new Set([...this._cls.split(' ').filter(Boolean),...xs])].join(' ');}};
    this.readyState=0;this.duration=60;this.currentTime=0;this.playbackRate=1;this.played=0;this.paused=0;
  }
  set className(v){this._cls=v;} get className(){return this._cls;}
  set textContent(v){this._text=String(v);this.children=[];} get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  get isConnected(){return this===document.head || !!this.connected || !!this.parentNode?.isConnected;}
  set src(v){this.attrs.src=v;} get src(){return this.attrs.src||'';}
  append(...nodes){for(const n of nodes){if(typeof n==='string'){this._text+=n;continue;}n.parentNode=this;this.children.push(n);}}
  replaceChildren(...nodes){for(const n of this.children)n.parentNode=null;this.children=[];this._text='';this.append(...nodes);}
  setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k]??null;} removeAttribute(k){delete this.attrs[k];}
  addEventListener(k,fn){(this.listeners[k]??=[]).push(fn);}
  dispatch(k){for(const fn of this.listeners[k]||[])fn({target:this});}
  pause(){this.paused++;}load(){this.readyState=0;}play(){this.played++;return Promise.resolve();}
  remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(x=>x!==this);this.parentNode=null;}}
  getBoundingClientRect(){
    const parentWidth=this.parentNode?.getBoundingClientRect().width||400;
    const width=this.style.width?.endsWith('%')?parseFloat(this.style.width)*parentWidth/100:parseFloat(this.style.width)||this.width;
    let left=0;
    if(this.parentNode&&this.className.includes('__segment')){for(const node of this.parentNode.children){if(node===this)break;left+=parseFloat(node.style.width||'0')*parentWidth/100;}}
    return {left,width,top:0,height:32};
  }
  animate(frames,options){animationCalls++;return {cancel(){},frames,options};}
  cloneNode(deep){const n=new Node(this.tagName);n.className=this.className;n.style={...this.style};n.dataset={...this.dataset};n._text=this._text;if(deep)for(const c of this.children)n.append(c.cloneNode(true));return n;}
}
let animationCalls=0,reduced=false;
const hosts=new Map();
globalThis.document={
 baseURI:'http://localhost/dev/test.html',
 head:new Node('head'),
 createElement:tag=>new Node(tag),
 getElementById:id=>hosts.get(id)||null,
 querySelector:selector=>{
   const name=selector.match(/data-cqa-style="([^"]+)"/)?.[1];
   return document.head.children.find(n=>n.dataset.cqaStyle===name)||null;
 }
};
globalThis.Element=Node;
globalThis.Option=class extends Node{constructor(text,value){super('option');this.textContent=text;this.value=value;}};
globalThis.window={matchMedia:()=>({matches:reduced})};
function host(id){const n=new Node('div');n.connected=true;hosts.set(id,n);return n;}
function walk(root){return [root,...root.children.flatMap(walk)];}
function find(root,cls){return walk(root).find(n=>n.className.split(' ').includes(cls));}
function bus(){const map=new Map();return{on(k,fn){if(!map.has(k))map.set(k,new Set());map.get(k).add(fn);return()=>map.get(k).delete(fn);},emit(k,v){for(const fn of map.get(k)||[])fn(v);},count(k){return map.get(k)?.size||0;}};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const root=fileURLToPath(new URL('../', import.meta.url)).replace(/\/$/, '');
const {mount:audioMount}=await import(root+'/web/js/audio.js');
const {mount:weightsMount}=await import(root+'/web/js/weights.js');

let ahost=host('cqa-audio'), calls=0;
let audioResponse={src:'https://example.test/audio.wav',offsets:{'A:0':1,'A:1':12}};
const CQA={state:{activeConversationId:'A'},bus:bus(),api:{audio:async()=>{calls++;return audioResponse;}}};
const cleanup=audioMount(CQA);
assert.equal(audioMount(CQA),cleanup);assert.equal(CQA.bus.count('conv:loaded'),1);
const detail={id:'A',channel:'call',messages:[{id:'A:0',rec_sec:1,ts_raw:'00:01'},{id:'A:1',rec_sec:12,ts_raw:'00:12'},{id:'A:2',rec_sec:null,ts_raw:'пауза'}]};
CQA.bus.emit('conv:loaded',detail);
CQA.bus.emit('audio:seek',{conv_id:'A',message_id:'A:1'});
await tick();
let audio=walk(ahost).find(n=>n.tagName==='AUDIO');
assert.equal(audio.played,0,'must await metadata');
audio.readyState=1;audio.dispatch('loadedmetadata');await tick();
assert.equal(audio.currentTime,10);assert.equal(audio.played,1);
CQA.bus.emit('audio:seek',{conv_id:'A',message_id:'A:0'});await tick();assert.equal(audio.currentTime,0,'negative pre-roll clamps');
let rate=walk(ahost).find(n=>n.tagName==='SELECT');
rate.value='1.5';rate.dispatch('change');assert.equal(audio.playbackRate,1.5);
audio.currentTime=24;audio.dispatch('error');await tick();
assert.equal(calls,2,'refresh occurs once');
audio.readyState=1;audio.dispatch('loadedmetadata');await tick();
assert.equal(audio.currentTime,24,'refresh resumes exact playback position');
assert.equal(audio.playbackRate,1.5);
audio.dispatch('error');await tick();assert.equal(calls,2,'no repeated refresh loop');
assert.equal(ahost.dataset.state,'unavailable');
assert.equal(find(ahost,'cqa-audio__media').hidden,true);

audioResponse={src:null,offsets:{'A:1':12}};
CQA.bus.emit('conv:loaded',detail);await tick();
CQA.bus.emit('audio:seek',{conv_id:'A',message_id:'A:1'});
assert.equal(ahost.dataset.state,'unavailable');assert.match(find(ahost,'cqa-audio__cue').textContent,/00:12/);
CQA.bus.emit('conv:loaded',{...detail,channel:'chat'});assert.equal(ahost.hidden,true);

let resolveOld;
CQA.api.audio=id=>id==='A'?new Promise(resolve=>{resolveOld=resolve;}):Promise.resolve({src:null,offsets:{}});
CQA.bus.emit('conv:loaded',detail);
CQA.state.activeConversationId='B';CQA.bus.emit('conv:open','B');
CQA.bus.emit('conv:loaded',{id:'B',channel:'call',messages:[]});await tick();
resolveOld({src:'https://example.test/old.wav',offsets:{}});await tick();
assert.equal(ahost.dataset.state,'unavailable');assert.equal(walk(ahost).find(n=>n.tagName==='AUDIO').src,'','stale src ignored');
cleanup();assert.equal(CQA.bus.count('conv:loaded'),0);

let whost=host('cqa-weights');
const wcqa={bus:bus(),state:{}};
const wcleanup=weightsMount(wcqa);assert.equal(weightsMount(wcqa),wcleanup);
let picked=null;wcqa.bus.on('criterion:focus',x=>picked=x);
const lines=[
 {criterion_id:'C1',weight:20,weight_effective:25,points:1,max_points:1,answer:'yes',is_na:false},
 {criterion_id:'C2',weight:30,weight_effective:37.5,points:1,max_points:2,answer:1,is_na:false},
 {criterion_id:'C3',weight:20,weight_effective:0,points:null,max_points:1,answer:'na',is_na:true},
 {criterion_id:'C4',weight:30,weight_effective:37.5,points:0,max_points:1,answer:'no',is_na:false}];
const wd={id:'W',checklist:{criteria:[{id:'C1',title:'<img onerror=bad>'}]},score:{total:43.75,lines,formula_steps:['20 / 80 = 25%']}};
wcqa.bus.emit('conv:loaded',wd);
let segs=walk(whost).filter(n=>n.className==='cqa-weights__segment');
assert.deepEqual(segs.map(n=>n.style.width),['25%','37.5%','0%','37.5%']);
assert.equal(wd.score.total,43.75,'official score untouched');assert.match(find(whost,'cqa-weights__note').textContent,/20%.*3/);
find(whost,'cqa-weights__criterion').dispatch('click');assert.deepEqual(picked,{conv_id:'W',criterion_id:'C1'});
assert.match(whost.textContent,/<img onerror=bad>/,'untrusted title remains text');
let toggle=find(whost,'cqa-weights__toggle');toggle.dispatch('click');
assert.deepEqual(segs.map(n=>n.style.width),['20%','30%','20%','30%']);
assert.ok(animationCalls>0);
reduced=true;animationCalls=0;toggle.dispatch('click');assert.equal(animationCalls,0,'reduced-motion skips animations');
wcqa.bus.emit('score:updated',{conv_id:'other',score:{lines:[]}});assert.equal(walk(whost).filter(n=>n.className==='cqa-weights__segment').length,4);
wcqa.bus.emit('score:updated',{conv_id:'W',score:{total:null,lines:lines.map(x=>({...x,is_na:true,answer:'na',weight_effective:0,points:null}))}});
assert.match(find(whost,'cqa-weights__note').textContent,/Нет применимых/);assert.equal(find(whost,'cqa-weights__toggle').disabled,true);
assert.ok(walk(whost).every(n=>!String(n.style.width).match(/NaN|Infinity/)));
wcqa.bus.emit('conv:loaded',{...wd,checklist:{criteria:[{id:'C8',critical:true,title:'Flag must stay outside the weight bar'}]},score:{...wd.score,lines:[...lines,{criterion_id:'C8',answer:'no',weight:0,weight_effective:0,points:0,max_points:0}]}});
assert.equal(walk(whost).filter(n=>n.className==='cqa-weights__segment').length,4,'zero-weight critical flag is outside the bar');
assert.doesNotMatch(whost.textContent,/Flag must stay outside/);
find(whost,'cqa-weights__toggle').dispatch('click');
wcqa.bus.emit('conv:loaded',wd);
assert.equal(find(whost,'cqa-weights__bar').dataset.mode,'original','refreshing the same card preserves the weight comparison mode');
wcqa.bus.emit('conv:loaded',{id:'W',score:{lines:[]}});assert.match(whost.textContent,/после оценки/);
wcleanup();assert.equal(wcqa.bus.count('conv:loaded'),0);

for(const file of ['audio','weights']){
 const html=readFileSync(root+'/web/dev/'+file+'.html','utf8');
 const script=html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
 assert.ok(script);new vm.Script(script);
}
console.log('PASS: audio/weights state assertions; metadata seek, one-refresh resume, stale responses, missing audio, N/A layout, reduced motion, score isolation, and demo script syntax.');
