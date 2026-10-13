const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../corpus_api/web/topic-workflow.html'),'utf8');
for(const m of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const source=name=>html.split(/\r?\n/).find(line=>line.trim().startsWith('async function '+name+'('));
async function check(){
 const fields={startModel:{disabled:false},modelId:{value:'model_A'},apiKey:{value:'private'},modelBatchProgress:{}};
 const job={job_id:'existing',kind:'model_review',model_id:'model_A',round_id:'R0002',status:'running'};
 let submissions=0,polls=0,recoveries=0;const rendered=[];
 const ctx={state:{jobTimers:{}},$:id=>fields[id],currentRound:()=>({round_id:'R0002'}),modelSettings:()=>({}),api:async()=>{submissions++;return job},renderJobProgress:j=>{rendered.push(j);ctx.state.lastModelJob=j},showStep:()=>{},pollJob:()=>{polls++},setGlobal:()=>{},load:async()=>{throw new Error('refresh failed')},recoverModelJob:async()=>{recoveries++;return job}};
 vm.createContext(ctx);vm.runInContext(source('startModel'),ctx);
 await ctx.startModel();await Promise.resolve();
 assert.equal(submissions,1);assert.equal(polls,1);assert.ok(rendered.every(x=>x!==null));
 fields.startModel.disabled=false;fields.apiKey.value='private';ctx.api=async()=>{submissions++;throw new Error('response timeout')};
 await ctx.startModel();assert.equal(submissions,2);assert.equal(recoveries,1);
 ctx.state.lastModelJob=job;ctx.api=async()=>{throw new Error('query timeout')};let retry=0;ctx.setTimeout=()=>{retry++;return 1};ctx.clearTimeout=()=>{};
 vm.runInContext(source('pollJob'),ctx);await ctx.pollJob('existing',null);
 assert.equal(retry,1);assert.ok(rendered.every(x=>x!==null));
 console.log('PASS: failed refresh preserves accepted task, response timeout recovers without resubmission, failed polling retains progress and retries');
}
check().catch(error=>{console.error(error);process.exit(1)});
