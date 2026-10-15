const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../corpus_api/web/topic-workflow.html'),'utf8');
const extract=name=>html.split(/\r?\n/).find(line=>line.trim().startsWith('function '+name+'('));
class Element{constructor(){this.children=[];this.innerHTML='';}replaceChildren(...xs){this.children=xs}append(...xs){this.children.push(...xs)}querySelector(){return this.actions??=new Element()}}
const nodes={modelList:new Element(),modelExportList:new Element()};
const round={round_id:'R0004',models:[{model_id:'model_B',status:'completed_with_errors',completed:944,failed:3}]};
const job={job_id:'done',kind:'model_review',round_id:'R0004',model_id:'model_B',status:'completed_with_errors',successful:944,failed:3,total:947,created_at:'2026-10-15'};
let displayed;
const ctx={state:{data:{jobs:[job]},tasks:[],jobTimers:{}},currentRound:()=>round,$:id=>nodes[id],document:{createElement:()=>new Element()},renderJobProgress:j=>{displayed=j}};
vm.createContext(ctx);vm.runInContext(extract('latestModelJob')+'\n'+extract('renderModels'),ctx);
assert.equal(ctx.latestModelJob('R0004','model_B').job_id,'done');assert.equal(ctx.latestModelJob('R0004','model_B',true),null);
ctx.renderModels();assert.equal(nodes.modelList.children.length,1);assert.equal(nodes.modelExportList.children.length,1);assert.equal(displayed.job_id,'done');
const actions=nodes.modelExportList.children[0].actions.children;assert.equal(actions[0].textContent,'查看逐聚类判断');assert.match(actions[1].href,/R0004\/models\/model_B\/export\?format=csv/);assert.match(actions[2].href,/format=jsonl/);
ctx.state.data.jobs.unshift({...job,job_id:'other',round_id:'R0002',created_at:'2026-10-16'});assert.equal(ctx.latestModelJob('R0004','model_B').job_id,'done');
console.log('PASS: completed model results render with view and CSV/JSONL links; active-only filter and round isolation work');
