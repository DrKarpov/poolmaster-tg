// Exercise the actual delivered animation with only host/clock/Canvas boundaries replaced.
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8')),haptics=[],events={},frames=new Map();
let now=0,id=0;
const gradient={addColorStop(){}},ctx=new Proxy({}, {get:(o,k)=>o[k]||(()=>{}),set:(o,k,v)=>(o[k]=v,true)});
ctx.createLinearGradient=ctx.createRadialGradient=()=>gradient;
const attrs={},button={setAttribute:(k,v)=>attrs[k]=v},canvas={getContext:()=>ctx,getBoundingClientRect:()=>({width:320})};
const root={querySelector:s=>s==='button'?button:canvas,setAttribute:(k,v)=>attrs[k]=v};
const sdk={ready(){}};
if(input.support!=='missing')sdk.HapticFeedback={impactOccurred(style){if(input.support==='throws')throw Error('unavailable');haptics.push({style,at:now})}};
const document={hidden:false,getElementById:()=>root,addEventListener:(name,fn)=>events[name]=fn};
const context=vm.createContext({document,window:{devicePixelRatio:1,TelegramWidget:sdk},Path2D:class{},Math,Uint8Array,
 performance:{now:()=>now},crypto:{getRandomValues:a=>(a.fill(input.side||0),a)},
 matchMedia:()=>({matches:!!input.reduced}),ResizeObserver:class{observe(){}},
 requestAnimationFrame:fn=>(frames.set(++id,fn),id),cancelAnimationFrame:id=>frames.delete(id)});
vm.runInContext(input.script,context);
const samples=[];
for(const action of input.actions){
 if(action==='tap')button.onclick();
 else if(action==='hide'){document.hidden=true;events.visibilitychange()}
 else if(action==='show'){document.hidden=false;events.visibilitychange()}
 else {now=action;const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn(now))}
 samples.push(haptics.length);
}
console.log(JSON.stringify({haptics,samples,attrs,frames:frames.size}));
