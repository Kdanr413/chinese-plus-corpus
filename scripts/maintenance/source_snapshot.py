"""Create a sanitized source snapshot; never copy or change live user data."""
from pathlib import Path
import argparse,hashlib,json,re,shutil,subprocess,zipfile
ROOT=Path(__file__).resolve().parents[2]
MODULES=['corpus_api','corpus_delivery','corpus_pipeline','corpus_platform','corpus_portal','corpus_recognition','corpus_registry','corpus_releases','corpus_search','corpus_topic_workflow','scripts','vendor','configs','db','tests','docs','deploy','参考文件中心']
EXCLUDED={'__pycache__','.cache','.pytest_cache','.git','runtime','填写示例'}
EXTENSIONS={'.py','.md','.json','.csv','.txt','.html','.css','.js','.cjs','.sql','.ps1','.cmd','.yml','.yaml','.svg','.j2','.toml',''}
SECRET=re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b)')
MARKERS=[]
for field in ['user.name','user.email']:
 value=subprocess.run(['git','config',field],cwd=ROOT,capture_output=True,text=True).stdout.strip()
 if len(value)>3:MARKERS.append(value)
remote=subprocess.run(['git','remote','get-url','origin'],cwd=ROOT,capture_output=True,text=True).stdout.strip()
match=re.search(r'github\.com[:/]([^/]+)/',remote)
if match:MARKERS.append(match.group(1))
def put(path,text):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
def sanitize(text):
 # The user explicitly publishes this repository URL; preserve it while scrubbing personal Git metadata.
 repository_url='https://github.com/Kdanr413/chinese-plus-corpus'
 text=text.replace(repository_url,'https://github.com/Kdanr413/chinese-plus-corpus')
 text=re.sub(r'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+','<USER_HOME>',text,flags=re.I)
 for marker in MARKERS:text=text.replace(marker,'[REDACTED]')
 if SECRET.search(text):raise ValueError('Potential credential in distribution source')
 return text.replace('https://github.com/Kdanr413/chinese-plus-corpus',repository_url)
def scrub(value):
 if isinstance(value,dict):return {k:('' if k.lower() in {'api_key','password','access_token','refresh_token','client_secret'} else scrub(v)) for k,v in value.items()}
 if isinstance(value,list):return [scrub(v) for v in value]
 return value
def copy_file(source,target):
 if source.relative_to(ROOT).parts[0]=='vendor':
  target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target);return
 text=source.read_text(encoding='utf-8-sig')
 if source.suffix=='.json':text=json.dumps(scrub(json.loads(text)),ensure_ascii=False,indent=2)+'\n'
 if source.name in {'source_rights.csv','coverage_targets.csv'}:text=text.splitlines()[0]+'\n'
 put(target,sanitize(text))
def program(target):
 target.mkdir(parents=True,exist_ok=False)
 for name in MODULES:
  for source in (ROOT/name).rglob('*'):
   relative=source.relative_to(ROOT)
   if not source.is_file() or any(x in EXCLUDED for x in relative.parts):continue
   if (source.suffix.lower() not in EXTENSIONS and source.name!='Caddyfile.example') or source.name.startswith('.env') or source.name in {'dynamic_topic_taxonomy.json','reference_taxonomy.json','step_models.json','step_models.json.tmp'}:continue
   copy_file(source,target/relative)
 for name in ['README.md','DEVELOPMENT_STATUS.md','requirements.txt','requirements-public.txt','environment.yml','.env.example','.gitignore','.gitattributes','THIRD_PARTY_NOTICES.md','run.ps1','启动语料库平台.cmd']:copy_file(ROOT/name,target/name)
 for source in (ROOT/'inputs').rglob('*'):
  if not source.is_file() or source.suffix not in {'.csv','.txt','.jsonl','.md'}:continue
  text=source.read_text(encoding='utf-8-sig')
  if source.suffix=='.csv':text=text.splitlines()[0]+'\n' if text.strip() else ''
  elif source.suffix=='.jsonl':text=''
  elif source.suffix=='.txt':text='的\n了\n和\n与\n' if source.name=='stop_terms.txt' else ''
  put(target/source.relative_to(ROOT),sanitize(text))
 for relative in ['data/raw/manual','data/raw/web','data/releases','data/workspaces','logs']:put(target/relative/'.gitkeep','')
 put(target/'corpus_topic_workflow/configs/dynamic_topic_taxonomy.json',json.dumps(dict(schema_version='3.0',taxonomy_name='用户话题体系',taxonomy_version=1,updated_at='',path_separator=' > ',max_levels=3,topics=[]),ensure_ascii=False,indent=2))
 p=target/'configs/pipeline.json';config=json.loads(p.read_text(encoding='utf-8'));config['workflow']['corpus_version']='0.1.0-draft';config['project']['name']='我的语料库';config.setdefault('topics',{})['reference_taxonomy']='configs/topics/reference_taxonomy.json';put(p,json.dumps(config,ensure_ascii=False,indent=2));put(target/'configs/topics/reference_taxonomy.json','{}\n')
 p=target/'corpus_pipeline/service.py';put(p,'\n'.join(line for line in p.read_text(encoding='utf-8').splitlines() if '"term method PDF":' not in line)+'\n')
def verify(root):
 count=0
 for path in root.rglob('*'):
  if not path.is_file() or path.name=='runtime-win64.zip':continue
  relative=path.relative_to(root)
  if path.name=='.env' or any(x in {'.git','__pycache__','runtime'} for x in relative.parts):raise ValueError('Private/generated file included')
  text=path.read_text(encoding='utf-8-sig',errors='ignore')
  if SECRET.search(text) or re.search(r'[A-Za-z]:[\\/]+Users[\\/]+Administrator',text,re.I) or any(x in text.replace('https://github.com/Kdanr413/chinese-plus-corpus','') for x in MARKERS):raise ValueError('Private content included: '+str(relative))
  count+=1
 return count
