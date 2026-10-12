const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const lines = fs.readFileSync(path.join(root,'corpus_api/web/index.html'),'utf8').split(/\r?\n/);
const configLine=lines.find(x=>x.startsWith('function modelConfig('));
const toggle=lines.find(x=>x.startsWith('function toggleModels(')).split("$('#machineEngine').onchange")[0];
const workflow=lines.find(x=>x.startsWith('async function runWorkflow('));
async function check(cleaning, external, invalid=false) {
 const fields={machineEngine:{value:external?'models':'rules'},cleaningLLM:{checked:cleaning},modelBox:{hidden:true},machineValidation:{checked:true},minValidators:{value:1},strategy:{value:'majority'},crossValidation:{checked:false},runButton:{disabled:false}};
 const calls=[];let saved,error;
 const context={models:invalid?[]:[{id:'m1',model:'test-model',url:'https://api.example.org/v1',key:'dummy',weight:1}],tests:{cleaning:cleaning,term:external},$:selector=>fields[selector.slice(1)],stage:()=>{},overall:()=>{},req:x=>x,waitLegacy:async()=>{},api:async(url,data)=>{calls.push({url,data});return url==='/api/workflow/prepare-current'?{profile:{},round:{round_id:'sample-round'}}:{}},rememberPendingTopicContinuation:x=>{saved=x},watchTopicPublication:()=>{},openTopicWorkflowRound:()=>{},clearPendingTopicContinuation:()=>{},fail:x=>{error=x}};
 vm.createContext(context);vm.runInContext(configLine+'\n'+toggle+'\n'+workflow,context);
 vm.runInContext('toggleModels()',context);assert.equal(fields.modelBox.hidden,!cleaning&&!external);
 await vm.runInContext('runWorkflow()',context);
 if(invalid){assert.ok(error);assert.equal(calls.length,0);return;}
 assert.equal(error,undefined);assert.equal(calls[0].data.model.cleaning_enabled,cleaning);assert.equal(calls[0].data.model.enabled,cleaning);assert.equal(saved.use_external_model,external);assert.equal(saved.validator_models.length,external?1:0);
}
(async()=>{for(const c of [false,true])for(const e of [false,true])await check(c,e);await check(true,false,true);console.log('PASS: four independent cleaning/term combinations, model panel visibility, prompt gating and missing-model rejection');})().catch(x=>{console.error(x);process.exit(1)});
