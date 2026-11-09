// Execute the shipped script, not a reimplementation of its decoder/controller.
// DOM/canvas are boundary doubles; the official QuickJS harness verifies profile.
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8')), handlers = {}, requests = [];
const timers=new Map(), frames=new Map(); let nextTimer=0,nextFrame=0,resized,writes=0,draws=0,now=0;
const replies=[];const RealDate=Date;class Clock extends RealDate {static now(){return now}}
const element = () => ({children: [], textContent: '', disabled: false,
    appendChild(child) { this.children.push(child); },
    getBoundingClientRect() { return {width: input.width ?? 300, left: 0, top:0}; },
    get clientWidth() {return input.width ?? 300}, setPointerCapture() {}});
const elements = Object.fromEntries(['c','i','s','f','p'].map(k => [k,element()]));
const labels=[],fills=[],strokes=[];let clip=null,rectangle=null,canvasWidth=300;const clips=[];
Object.defineProperty(elements.c,'width',{get(){return canvasWidth},set(v){canvasWidth=v;draws++;labels.length=fills.length=strokes.length=0;clip=rectangle=null}});
elements.c.getContext = () => new Proxy({}, {get: (o,k) => ({
    fillText:(...args)=>labels.push(args),
    fillRect:(...args)=>{fills.push(args)},
    save:()=>clips.push(clip),restore:()=>clip=clips.pop(),rect:(...args)=>rectangle=args,
    clip:()=>clip=rectangle,stroke:()=>strokes.push(clip),
})[k] || o[k] || (() => {}), set: (o,k,v) => (o[k]=v,true)});
const sdk = {publicData: input.publicData, chatData: input.chatData,
    onEvent(name,fn) { handlers[name] = fn; }, ready() {},
    request(data) { requests.push(data);return new Promise((resolve,reject)=>{replies.push({resolve,reject});if(!data.startsWith('p1:history:'))resolve({result:JSON.stringify({status:'pending',revision:Number(data.split(':').at(-1))+1})})}); }};
const context = vm.createContext({window:{TelegramWidget:sdk,devicePixelRatio:1},
    document:{getElementById:id=>elements[id],createElement:element,hidden:false,addEventListener(name,fn){handlers[name]=fn}},Date:Clock,
    localStorage:{getItem:()=>input.storedView?JSON.stringify(input.storedView):null,setItem(){writes++}},
    requestAnimationFrame:fn=>{frames.set(++nextFrame,fn);return nextFrame;},cancelAnimationFrame:id=>frames.delete(id),
    setTimeout:(fn,delay=0)=>{timers.set(++nextTimer,{fn,at:now+delay});return nextTimer;},clearTimeout:id=>timers.delete(id),
    ResizeObserver:class {constructor(fn){resized=fn}observe(){}},console});
vm.runInContext(input.script,context);
const inspect = () => vm.runInContext('({revision:A.r, candles:D.length, timeframe:A.f, rows:D, right:R, visible:n})',context);
const states = [inspect()];
const metrics=()=>({writes,draws,frames:frames.size,status:elements.s.className,disabled:elements.f.children.map(b=>b.disabled)});
const measurements=[metrics()];
(async()=>{
for (const action of input.actions || []) {
    if (action.kind === 'data') {
        sdk.publicData=action.publicData;
        sdk.chatData=action.chatData;
        handlers.publicDataChanged();
    } else if (action.kind === 'resize') {input.width=action.width;resized();}
    else if (action.kind === 'tap') elements.f.children[action.index].onclick();
    else if (action.kind === 'timer') timers.get(action.id)?.fn();
    else if (action.kind === 'reply') replies[action.index||0].resolve({result:JSON.stringify(action.data)});
    else if (action.kind === 'reject') replies[action.index||0].reject({code:action.code});
    else if (action.kind === 'visibility') {context.document.hidden=action.hidden;handlers.visibilitychange?.();}
    else if (action.kind === 'tick') {now+=action.ms;for(let count=0;count<100;count++){const due=[...timers].find(([id,t])=>t.at<=now);if(!due)break;timers.delete(due[0]);due[1].fn();await Promise.resolve();}}
    else if (action.kind === 'down') elements.c.onpointerdown({clientX:100,clientY:action.y||100,pointerId:1});
    else if (action.kind === 'move') elements.c.onpointermove({clientX:100+action.dx,clientY:action.y||100});
    else if (action.kind === 'up') elements.c.onpointerup({clientX:100+action.dx,clientY:action.y||100});
    else if (action.kind === 'animation') {const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn());}
    else if (action.kind === 'drag') {
        elements.c.onpointerdown({clientX:100,clientY:action.y||100,pointerId:1});
        elements.c.onpointermove({clientX:100+action.dx,clientY:action.y||100});
        elements.c.onpointerup({clientX:100+action.dx,clientY:action.y||100});
    }
    await Promise.resolve();await Promise.resolve();states.push(inspect());measurements.push(metrics());
}
console.log(JSON.stringify({states,measurements,requests,info:elements.i.textContent,status:elements.s.className,statusText:elements.s.textContent,
    price:elements.p.textContent,labels,fills,strokes,disabled:elements.f.children.map(b=>b.disabled)}));

})().catch(e=>{console.error(e);process.exitCode=1});
