const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const lines = fs.readFileSync(path.join(root,'corpus_api/web/index.html'),'utf8').split(/\r?\n/);
const functions=['stageUsesModel','modelConfig','modes','requiredModelTests','recognitionStepConfig','stepSettingsPayload','toggleModels','runWorkflow'].map(name=>lines.find(x=>x.startsWith('function '+name+'(')||x.startsWith('async function '+name+'('))).join('\n');
for(const match of fs.readFileSync(path.join(root,'corpus_api/web/index.html'),'utf8').matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
async function check(cleaning, external, cross, invalid=false, untested=false) {
 const fields={machineEngine:{value:external?'models':'rules'},cleaningLLM:{checked:cleaning},cleaningModel:{value:invalid?'':'clean'},termModelChoices:{},crossModelChoices:{},machineValidation:{checked:true},minValidators:{value:1},strategy:{value:'majority'},crossValidation:{checked:true},crossModelEnabled:{checked:cross},crossStrategy:{value:'weighted'},crossMinValidators:{value:1},runButton:{disabled:false}};
 const calls=[];let saved,error;
 const bank=['term','cross','clean','unused'].map(id=>({id,model:'model-'+id,url:'https://api.example.org/v1',key:'dummy-'+id,weight:1}));
 const context={models:bank,stepSelections:{cleaning:['clean'],term:['term'],cross:['cross']},tests:{cleaning:cleaning&&!untested,term:external,cross},$:selector=>fields[selector.slice(1)],updateStepStatus:()=>{},persistStepSettings:async()=>{},stage:()=>{},overall:()=>{},req:x=>x,waitLegacy:async()=>{},api:async(url,data)=>{calls.push({url,data});return url==='/api/workflow/prepare-current'?{profile:{},round:{round_id:'sample-round'}}:{}},rememberPendingTopicContinuation:x=>{saved=x},watchTopicPublication:()=>{},openTopicWorkflowRound:()=>{},clearPendingTopicContinuation:()=>{},fail:x=>{error=x}};
 vm.createContext(context);vm.runInContext(functions,context);vm.runInContext('toggleModels()',context);
 assert.equal(fields.cleaningModel.disabled,!cleaning);assert.equal(fields.termModelChoices.hidden,!external);assert.equal(fields.crossModelChoices.hidden,!cross);
 await vm.runInContext('runWorkflow()',context);
 if(invalid||untested){assert.ok(error);assert.equal(calls.length,0);return;}
 assert.equal(error,undefined);assert.equal(calls[0].data.model.cleaning_enabled,cleaning);if(cleaning)assert.equal(calls[0].data.model.name,'model-clean');
 assert.equal(saved.use_external_model,external);assert.equal(saved.validator_models.length,external?1:0);if(external)assert.equal(saved.validator_models[0].id,'term');
 assert.equal(saved.cross_use_external_model,cross);assert.equal(saved.cross_validator_models.length,cross?1:0);if(cross)assert.equal(saved.cross_validator_models[0].id,'cross');
 assert.equal(saved.cross_strategy,'weighted');
 const settings=vm.runInContext('stepSettingsPayload()',context);assert.equal(settings.steps.cross.mode,cross?'models':'rules');assert.equal(settings.steps.cleaning.model_ids[0],'clean');
}
(async()=>{for(const c of [false,true])for(const e of [false,true])for(const x of [false,true])await check(c,e,x);await check(true,false,false,true);await check(true,false,false,false,true);console.log('PASS: 8 independent step-model combinations, explicit non-first cleaning model, separate cross models, invalid choice and untested configuration blocked');})().catch(x=>{console.error(x);process.exit(1)});
