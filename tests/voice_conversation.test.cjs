const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve();};
function harness(decide, tts=false){
 const nodes=new Map(), intervals=[], submitted=[], decisions=[], audios=[];
 let processor, transcript='', busy=false, cancelled=0;
 const element=()=>({textContent:'',style:{},dataset:{},classList:{toggle(){}},setAttribute(){},addEventListener(type,fn){this[type]=fn;}});
 const doc={createElement:element,querySelector:()=>({prepend(){}}),getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);}};
 class Context{
  constructor(){this.sampleRate=16000;this.destination={};}
  async resume(){} async close(){}
  createScriptProcessor(){processor={connect(){},disconnect(){}};return processor;}
  createMediaStreamSource(){return {connect(){},disconnect(){}};}
  createGain(){return {gain:{},connect(){},disconnect(){}};}
 }
 const sandbox={document:doc,navigator:{mediaDevices:{async getUserMedia(){return {getTracks:()=>[{stop(){}}]};}}},AudioContext:Context,
  Audio:class { constructor(){this.plays=0;this.pauses=0;audios.push(this);} async play(){this.plays++;} pause(){this.pauses++;} removeAttribute(){} load(){} },
  AbortController,Float32Array,ArrayBuffer,DataView,performance:{now:()=>0},setInterval(fn){intervals.push(fn);return fn;},clearInterval(){},setTimeout,URL:{createObjectURL:()=>"blob:test",revokeObjectURL(){}},addEventListener(){},
  async fetch(url,opts){
   if(url.endsWith('/status'))return {ok:true,json:async()=>({enabled:true,stt_available:true,tts_available:tts})};
   if(url.endsWith('/synthesize'))return {ok:true,blob:async()=>({})};
   if(url.endsWith('/transcribe'))return {ok:true,json:async()=>({text:transcript})};
   if(url.endsWith('/decide')){const payload=JSON.parse(opts.body);decisions.push(payload);return {ok:true,json:async()=>await decide(payload)};}
   throw Error(url);
  }};
 vm.runInNewContext(fs.readFileSync('web_chat/voice.js','utf8'),sandbox);
 const ui=sandbox.VoiceUI;
 ui.setup({getContextId:()=> 'ctx',getDraft:()=> 'typed draft',isStreaming:()=>busy,getAssistantText:()=> 'prior answer',sendVoiceMessage:async text=>{submitted.push(text);busy=true;},interruptAgent:()=>{cancelled++;},ensureConversation:async()=>{}});
 function frame(level){processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(1600).fill(level)}});}
 return {ui,submitted,decisions,audios,get cancelled(){return cancelled;},setBusy(v){busy=v;},
  async start(){nodes.get('voice-mic-btn').click();await flush();},
  onset(){frame(.1);frame(.1);},
  async utterance(text){transcript=text;frame(.1);frame(.1);for(let i=0;i<8;i++)frame(0);await flush();},
  async tick(){intervals.forEach(fn=>fn());await flush();},
 };
}
test('wait accumulates thought; only semantic respond dispatches it',async()=>{
 const h=harness(({text})=>({action:text.includes('файл')?'respond':'wait'}));await h.start();
 await h.utterance('Я хотел бы');await h.tick();assert.equal(h.submitted.length,0);
 await h.utterance('прочитать файл');await h.tick();assert.deepEqual(h.submitted,['Я хотел бы прочитать файл']);h.ui.cleanup();
});
test('semantic interrupt cancels once; replacement waits for agent terminal event',async()=>{
 const h=harness(()=>({action:'interrupt',replacement:true}));await h.start();h.setBusy(true);
 await h.utterance('Нет, объясни другое');await h.tick();assert.equal(h.cancelled,1);assert.equal(h.submitted.length,0);
 h.setBusy(false);await h.tick();assert.deepEqual(h.submitted,['Нет, объясни другое']);h.ui.cleanup();
});
test('pure stop never creates a new agent run',async()=>{
 const h=harness(()=>({action:'interrupt',replacement:false}));await h.start();h.setBusy(true);
 await h.utterance('Стоп');h.setBusy(false);await h.tick();assert.equal(h.cancelled,1);assert.equal(h.submitted.length,0);h.ui.cleanup();
});
test('new speech invalidates a late semantic respond',async()=>{
 let resolve;const h=harness(()=>new Promise(r=>resolve=r));await h.start();
 await h.utterance('Сделай');h.onset();resolve({action:'respond'});await flush();await h.tick();assert.equal(h.submitted.length,0);h.ui.cleanup();
});

test('acoustic onset pauses speech; semantic ignore resumes the same audio',async()=>{
 const h=harness(()=>({action:'ignore'}),true);await h.start();
 const speaking=h.ui.speak('Текущий ответ','ctx');await flush();
 assert.equal(h.audios.length,1);assert.equal(h.audios[0].plays,1);
 await h.utterance('Ага');await h.tick();
 assert.equal(h.audios[0].pauses,1);assert.equal(h.audios[0].plays,2);
 assert.equal(h.submitted.length,0);h.ui.cleanup();await speaking;
});
