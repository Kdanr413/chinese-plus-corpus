const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../corpus_portal/web/index.html'),'utf8');
for(const m of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const first=html.indexOf('function renderNetworkSettings(data){'),last=html.indexOf("$('#openNetworkSettings').onclick",first);
const formStart=html.indexOf("$('#networkSettingsForm').onsubmit="),formEnd=html.indexOf("$('#createShare').onclick=",formStart);
const fields={},calls=[];const active={api_host:'127.0.0.1',api_port:8000,public_url:'http://127.0.0.1:8000'};let failSave=false;
const context={canManageNetwork:false,$:id=>fields[id]??={value:'',disabled:false,showModal(){this.open=true}},api:async(url,options={})=>{calls.push({url,options});if(url==='/network-info')return {lan_addresses:['192.168.1.20']};if(options.method==='POST'){if(failSave)throw new Error('save unavailable');return {active,saved:JSON.parse(options.body),restart_required:true,message:'saved; restart later'}}return {active,saved:active,restart_required:false}}};
vm.createContext(context);vm.runInContext(html.slice(first,last)+'\n'+html.slice(formStart,formEnd),context);
(async()=>{
 await context.openNetworkSettings();assert.equal(calls.length,0);
 context.canManageNetwork=true;await context.openNetworkSettings();assert.ok(fields['#networkSettings'].open);assert.equal(fields['#networkHost'].value,'127.0.0.1');
 fields['#networkHost'].value='0.0.0.0';fields['#networkPort'].value='9000';fields['#networkPublicUrl'].value='http://192.168.1.20:9000';
 await fields['#networkSettingsForm'].onsubmit({preventDefault(){}});
 const saved=calls.find(c=>c.options.method==='POST');assert.deepEqual(Object.keys(JSON.parse(saved.options.body)).sort(),['api_host','api_port','public_url']);assert.equal(active.api_host,'127.0.0.1');assert.match(fields['#activeNetworkSettings'].textContent,/127.0.0.1/);assert.match(fields['#networkSaveStatus'].textContent,/restart later/);assert.equal(fields['#saveNetworkSettings'].disabled,false);
 failSave=true;await fields['#networkSettingsForm'].onsubmit({preventDefault(){}});assert.match(fields['#networkSaveStatus'].textContent,/保存失败/);assert.equal(fields['#saveNetworkSettings'].disabled,false);
 assert.ok(calls.every(c=>['/network-info','/network-settings'].includes(c.url)));
 console.log('PASS: admin-only form, current/saved values, three-field save, no automatic restart, visible errors and retry');
})().catch(e=>{console.error(e);process.exit(1)});
