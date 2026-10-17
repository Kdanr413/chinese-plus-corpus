const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../corpus_api/web/index.html'),'utf8');
for(const m of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
assert.ok(html.indexOf('id="uploadSourceCategory"')>html.indexOf('<b>基础语料文件</b>'));
assert.ok(!html.includes('id="uploadBookTitle"'));
const fields={uploadSourceCategory:{value:''},corpusFiles:{files:[],value:'files',disabled:false},uploadCorpusBatch:{disabled:false}};
let calls=[],errors=[],failUpload=false;
const ctx={$:s=>fields[s.slice(1)],setUploadStatus:()=>{},overall:()=>{},loadFiles:async()=>{},loadResourceStatus:async()=>{},fail:e=>errors.push(e.message),api:async(url)=>{calls.push(url);if(failUpload)throw new Error('network failed');return {ok:true,message:'ok'}}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('async function upload(input,category){'),html.indexOf("$('#reuseApprovedReference').onclick")),ctx);
vm.runInContext(html.slice(html.indexOf("$('#corpusFiles').onchange="),html.indexOf("$$('[data-category]').forEach")),ctx);
const file=name=>({name,arrayBuffer:async()=>new ArrayBuffer(0)});
(async()=>{
 fields.corpusFiles.files=[file('甲.txt'),file('乙.txt')];fields.corpusFiles.onchange({target:fields.corpusFiles});assert.equal(calls.length,0);
 await fields.uploadCorpusBatch.onclick();assert.equal(calls.length,0);assert.match(errors.pop(),/先选择.*来源/);assert.equal(fields.uploadCorpusBatch.disabled,false);
 fields.uploadSourceCategory.value='教材';await fields.uploadCorpusBatch.onclick();assert.equal(calls.length,2);
 for(const call of calls){const params=new URL(call,'http://local').searchParams;assert.equal(params.get('source_category'),'教材');assert.equal(params.get('book_title'),'');}
 assert.equal(fields.uploadSourceCategory.value,'');assert.equal(fields.corpusFiles.disabled,false);
 fields.uploadSourceCategory.value='新闻';failUpload=true;await fields.uploadCorpusBatch.onclick();assert.equal(fields.uploadSourceCategory.value,'新闻');assert.equal(fields.corpusFiles.disabled,false);assert.equal(fields.uploadCorpusBatch.disabled,false);
 console.log('PASS: per-batch source required; file selection does not auto-upload; textbooks send independent filenames; successful batches reset category; failures remain retryable');
})().catch(e=>{console.error(e);process.exit(1)});
