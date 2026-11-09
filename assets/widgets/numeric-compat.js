// Shared bootstrap for the locale-dependent Telegram Desktop JSON defect.
// The sandbox captures stringify before our script runs. A realm-local array
// hook therefore protects Canvas command arrays even through that reference.
// The separate trusted validator receives regular JSON numeric tokens.
(()=>{
  const parse=JSON.parse;
  if(parse('[1,1]')[0]===1||!JSON.rawJSON)return;
  // QuickJS retains the exact numeric token in the reviver source context.
  JSON.parse=(text,reviver)=>parse(text,function(key,value,context){
    if(typeof value==='number')value=Number(context.source);
    return typeof reviver==='function'?reviver.call(this,key,value,context):value;
  });
  function safe(value){
    if(Number.isFinite(value)){
      // Shortest round-trippable digits, no decimal point, no adjacent numeric
      // tokens for strtod to merge: 1.65 -> 165e-2; zero -> 0e0.
      let [mantissa,exponent]=value.toExponential().split('e');
      let [integer,fraction='']=mantissa.split('.');
      return JSON.rawJSON(integer+fraction+'e'+(exponent-fraction.length));
    }
    // Animations contain plain records inside their array batches. Protect their
    // numeric properties too. Raw JSON objects have a null prototype, and Date
    // and other built-ins retain their native serialization.
    return value?.constructor===Object?
      Object.fromEntries(Object.entries(value).map(([key,v])=>[key,safe(v)])):value;
  }
  Object.defineProperty(Array.prototype,'toJSON',{
    configurable:true,writable:true,
    value(){return this.map(safe)}
  });
})();
