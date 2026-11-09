(async()=>{
// Run the generated production script. DOM/canvas are the external boundary.
const fs=require('node:fs'),vm=require('node:vm'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const events={},requests=[],replies=[],timers=new Map();let next=0,layoutQueries=0;
function element(){return {children:[],_text:'',get textContent(){return this._text},set textContent(v){this._text=v;this.children=[]},style:{},hidden:false,disabled:false,
 appendChild(c){this.children.push(c)},getBoundingClientRect(){layoutQueries++;return {width:input.width,left:0,top:0}}}}
const elements=Object.fromEntries(['c','s','mean','tvl','legend','popup','refresh'].map(k=>[k,element()]));
const labels=[];elements.s.textContent='●';elements.refresh.textContent='↻';
elements.c.getContext=()=>new Proxy({}, {get:(o,k)=>o[k]||(k==='fillText'?((...args)=>labels.push(args)):(()=>{})),set:(o,k,v)=>(o[k]=v,true)});
const sdk={publicData:input.publicData,chatData:input.chatData,onEvent:(e,f)=>events[e]=f,ready(){},
 request(data){requests.push(data);return input.deferred?new Promise(resolve=>replies.push(resolve)):Promise.resolve({result:'{"status":"pending"}'})}};
const context=vm.createContext({window:{TelegramWidget:sdk,devicePixelRatio:1},document:{getElementById:id=>elements[id],createElement:element},
 setTimeout:fn=>{timers.set(++next,fn);return next},clearTimeout:id=>timers.delete(id),ResizeObserver:class{constructor(fn){this.fn=fn}observe(){this.fn([{contentRect:{width:input.width}}])}}});
vm.runInContext(input.script,context);
const inspect=()=>vm.runInContext('({rows:D,pick,revision:A.r,status:document.getElementById("s").className})',context),states=[inspect()];
for(const a of input.actions){
 if(a.kind==='week') {const rows=states.at(-1).rows,first=rows[0][0],last=rows.at(-1)[0],x=rows.length===1?(input.width-38)/2:5+(rows[a.index][0]-first)/(last-first)*(input.width-48);elements.c.onpointerdown({clientX:x,clientY:80});elements.c.onpointerup({clientX:x,clientY:80})}
 if(a.kind==='drag'){elements.c.onpointerdown({clientX:70,clientY:80});elements.c.onpointermove?.({clientX:70+a.dx,clientY:80});elements.c.onpointerup({clientX:70+a.dx,clientY:80})}
 if(a.kind==='popup')elements.popup.onclick();
 if(a.kind==='refresh')elements.refresh.onclick();
 if(a.kind==='timer')timers.get(next)?.();
 if(a.kind==='reply')replies[a.index]({result:JSON.stringify(a.value)});
 if(a.kind==='data'){sdk.publicData=a.publicData;sdk.chatData=a.chatData;events.publicDataChanged()}
 await Promise.resolve();states.push(inspect());
}
function text(e){return e.textContent+e.children.map(text).join(' ')}
console.log(JSON.stringify({states,requests,layoutQueries,buttons:[elements.refresh.textContent],mean:elements.mean.textContent,tvl:elements.tvl.textContent,
 statusText:elements.s.textContent,popup:text(elements.popup),popupHidden:elements.popup.hidden,labels}));

})().catch(e=>{console.error(e);process.exitCode=1});
