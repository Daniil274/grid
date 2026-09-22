const {test} = require('node:test');
const assert = require('node:assert/strict');
const {encodeWav, resample, speechText, splitIntoChunks, Generation} = require('../web_chat/voice.js');
test('production encoder preserves PCM amplitude, mono header and clipping', () => {
 const data=encodeWav(new Float32Array([-1,-0.5,0,0.5,1,2]));
 const view=new DataView(data);
 assert.equal(Buffer.from(data).toString('ascii',0,4),'RIFF');
 assert.equal(view.getUint32(4,true),data.byteLength-8);
 assert.equal(view.getUint16(22,true),1);
 assert.equal(view.getUint32(24,true),16000);
 assert.equal(view.getUint16(34,true),16);
 assert.deepEqual(Array.from({length:6},(_,i)=>view.getInt16(44+2*i,true)),[-32768,-16384,0,16384,32767,32767]);
});
test('48k and 44.1k input preserve one second duration at 16k',()=>{
 for(const rate of [48000,44100]) {
  const samples=new Float32Array(rate).fill(.25);
  assert.equal(encodeWav(samples,rate).byteLength,32044);
  assert.ok(Math.abs(resample(samples,rate)[100]-.25)<1e-5);
 }
});
test('silence remains in recorded timeline',()=>{
 const samples=new Float32Array(48000);samples.fill(.5,24000);
 const view=new DataView(encodeWav(samples,48000));
 assert.equal(view.getInt16(44+7999*2,true),0);
 assert.equal(view.getInt16(44+8000*2,true),16384);
});
test('long unpunctuated words and sentences are bounded without dropped text',()=>{
 for(const text of ['x'.repeat(2001),Array(1000).fill('word').join(' ')]) {
  const chunks=splitIntoChunks(text,500);
  assert.ok(chunks.every(x=>x.length<=500 && x.length>0));
  assert.equal(chunks.join('').replaceAll(' ',''),text.replaceAll(' ',''));
 }
});
test('speech removes code, links and internal context marker',()=>{
 const clean=speechText('Hello **world** [label](https://example.com) ```js code``` Context ID: ctx-123');
 assert.equal(clean,'Hello world label');
});
test('generation invalidates late completion when a new capture or cleanup occurs',()=>{
 const token=new Generation();const old=token.next();assert.ok(token.is(old));token.next();assert.ok(!token.is(old));
});
const {TurnDetector} = require('../web_chat/voice.js');
test('acoustic detector ignores silence and clicks, preserves pauses within speech', () => {
 const d = new TurnDetector(16000);
 const silence = new Float32Array(1600), voice = new Float32Array(1600).fill(.1);
 for(let i=0;i<20;i++) assert.equal(d.push(silence),null);
 assert.equal(d.push(voice),null);
 assert.equal(d.push(silence),null);
 assert.equal(d.push(voice),null);
 assert.equal(d.push(voice).type,'start');
 for(let i=0;i<6;i++) assert.equal(d.push(silence),null);
 assert.equal(d.push(voice),null);
 for(let i=0;i<7;i++) assert.equal(d.push(silence),null);
 const end=d.push(silence);
 assert.equal(end.type,'end');
 assert.ok(end.samples.length>16000);
 assert.equal(d.active,false);
});
test('speech during playback is detected without waiting for an end button', () => {
 const d=new TurnDetector(48000), voice=new Float32Array(4800).fill(.08);
 assert.equal(d.push(voice,true),null);
 assert.equal(d.push(voice,true).type,'start');
});
