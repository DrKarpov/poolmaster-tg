"""Offline native sandbox/validator probe; host layout answers are controlled.

Uses separate engine processes for the untrusted widget and trusted validator.
This exercises the shipped bundles and numeric boundaries, not the Qt painter.
"""
import json
import os
from pathlib import Path
import re


SDK = Path(os.environ.get('POOLMASTER_WIDGET_SDK_DIR',
                          str(Path(__file__).parent/'optional-widget-sdk')))


def widget_program(html, public='{}', private='{}', *, kind, width=320, dpr=2):
    script = re.search(r'<script>(.*?)</script>', html, re.S).group(1)
    config = dict(features=['webAnimations'], maxTimers=64, policy='strict', visible=True,
                  viewport=dict(innerWidth=width, innerHeight=320, dpr=dpr),
                  theme='dark', storage={}, publicData=public, chatData=private,
                  canQuery=True, backgroundQuery=True, isCopy=False, chatType='private')
    host = r'''
      const __probeBatches=[],__probeLogs=[],__probeBridge=[],__probeAnims=[];
      let __probeNow=0;
      globalThis.__hostBinary=()=>null;
      globalThis.__host=(kind,arg)=>{
        if(kind==='ops'){__probeBatches.push(arg);return}
        if(kind==='anim'){__probeAnims.push(arg);return}
        if(kind==='now')return __probeNow;
        if(kind==='log'||kind==='diag'){__probeLogs.push([kind,arg]);return}
        if(kind==='bridge'){__probeBridge.push(arg);return}
        if(kind==='random')return JSON.stringify(Array.from({length:+arg},()=>1));
        if(kind==='query'){
          let q=JSON.parse(arg),w=__PROBE_WIDTH__;
          if(q.q==='rect'||q.q==='rects'){
            let r={x:0,y:0,width:w,height:268,top:0,left:0,right:w,bottom:268};
            return JSON.stringify(q.q==='rect'?r:[r]);
          }
          if(q.q==='box')return JSON.stringify({offsetWidth:w,clientWidth:w,offsetHeight:268,clientHeight:268});
          if(q.q==='matchMedia')return '{"matches":false}';
          if(q.q==='measureText')return JSON.stringify({width:String(q.text).length*6});
          if(q.q==='cs')return q.prop?'""':'{"display":"block"}';
          return 'null';
        }
      };
    '''.replace('__PROBE_WIDTH__', str(width))
    boot = '__twDispatch("boot",' + json.dumps(json.dumps(dict(html=html, config=config))) + ''');
      const __probeScript=JSON.parse(__twDispatch('nextScript',''));
    '''
    after = '''
      __twDispatch('endTick','');
      __twDispatch('scriptDone',JSON.stringify([__probeScript.id,true]));
      __twDispatch('loaded','');__twDispatch('endTick','');
      const __probeCanvas=__probeBatches.flatMap(b=>JSON.parse(b)).find(o=>o.op==='el'&&o.tag==='canvas')?.id;
      if(__probeCanvas)__twDispatch('resize',JSON.stringify([{id:__probeCanvas,width:__PROBE_WIDTH__,height:268}]));
      __twDispatch('endTick','');
    '''.replace('__PROBE_WIDTH__', str(width))
    if kind == 'coin':
        actions = '''
          document.querySelector('button').onclick();
          for(const t of [16,160,320,480,640,800,940,980,1040,1120]){
            __probeNow=t;__twDispatch('frame',String(t));__twDispatch('endTick','');
          }
          const __probeFacts={side:document.getElementById('jetton-coin-concept').getAttribute('data-side'),
            busy:document.querySelector('button').getAttribute('aria-busy')};
        '''
    elif kind == 'farming':
        actions = '''
          const __probeC=document.getElementById('c');
          __probeC.onpointerdown({clientX:140,clientY:100});
          __probeC.onpointerup({clientX:140,clientY:100});
          __twDispatch('endTick','');
          const __probeFacts={mean:document.getElementById('mean').textContent,
            popup:document.getElementById('popup').textContent};
        '''
    else:
        actions = '''
          const __probeFacts={info:document.getElementById('i').textContent,
            price:document.getElementById('p').textContent};
        '''
    if kind in ('price', 'farming'):
        actions += '''
          (document.getElementById('refresh')||document.querySelector('nav').lastElementChild).onclick();
          __twDispatch('endTick','');
          for(const t of [200,400,600,800]){
            __probeNow=t;__twDispatch('frame',String(t));__twDispatch('endTick','');
          }
        '''
    return '\n'.join([host, (SDK/'sandbox-bundle.js').read_text(), boot, script, after,
                      actions, "JSON.stringify({facts:__probeFacts,batches:__probeBatches,anims:__probeAnims,logs:__probeLogs,bridge:__probeBridge})"])


def validator_program(batches, anims=()):
    # Batch text stays quoted until the validator itself parses it in its own realm.
    return '\n'.join([
        "const __probeDiags=[];globalThis.__diag=(...args)=>__probeDiags.push(args);",
        (SDK/'validator-bundle.js').read_text(),
        "__twValidator.create('dark');__twValidator.setPolicy('strict');",
        'const __probeInput='+json.dumps(batches)+';',
        "const __probeOutput=__probeInput.map(b=>__twValidator.processOps(b));",
        'const __probeAnimInput='+json.dumps(anims)+';',
        "const __probeAnimOutput=__probeAnimInput.map(b=>__twValidator.validateAnim(b));",
        "JSON.stringify({batches:__probeOutput,anims:__probeAnimOutput,diags:__probeDiags,stopped:__twValidator.stopped()})",
    ])
