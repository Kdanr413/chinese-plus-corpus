const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../corpus_api/web/index.html'),'utf8');
for(const m of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const load=html.slice(html.indexOf('async function loadFiles(){'),html.indexOf('function previewFile(path)'));
const init=html.slice(html.indexOf('async function initializeWorkbench(){'),html.indexOf('// Start after the request timeout'));
const fields=Object.fromEntries(['fileQuery','fileStatus','uploadMessage','fileRows','filePage'].map(k=>[k,{value:'',textContent:'loading'}]));
let response={},calls=[];
const ctx={$:s=>fields[s.slice(1)],$$:()=>[],visibleFilePaths:[],esc:x=>x,api:async()=>{if(response instanceof Error)throw response;return response},console:{warn:()=>{}},loadPrompts:async()=>calls.push('prompts'),loadResourceStatus:async()=>{},platform:async()=>{},loadStructuredArtifacts:async()=>{},checkPublishedTopicRecovery:async()=>{},loadStepSettings:async()=>{throw new Error('status unavailable')},restoreLegacyJob:async()=>{throw new Error('should not run')},fail:()=>calls.push('status error')};
vm.createContext(ctx);vm.runInContext(load+'\n'+init,ctx);
(async()=>{
 response=new Error('HTTP 500');await assert.rejects(ctx.loadFiles());assert.match(fields.uploadMessage.textContent,/HTTP 500/);
 response={rows:[],summary:{active_files:3,reusable:2,rebuild:1},pagination:{filtered:0}};await ctx.initializeWorkbench();await new Promise(r=>setImmediate(r));assert.match(fields.uploadMessage.textContent,/3/);assert.ok(calls.includes('status error'));
 response=new Error('timeout');response.name='AbortError';await assert.rejects(ctx.loadFiles());assert.match(fields.uploadMessage.textContent,/重试/);
 response={rows:[],summary:{active_files:1,reusable:1,rebuild:0},pagination:{filtered:0}};await ctx.loadFiles();assert.match(fields.uploadMessage.textContent,/1/);
 console.log('PASS: status failure does not block uploads; upload failure and timeout are visible; retry succeeds; page JavaScript parses');
})().catch(e=>{console.error(e);process.exit(1)});
