"""Build private-data-free, current-source share packages without overwriting."""
from pathlib import Path
import argparse,hashlib,json,re,shutil,subprocess,zipfile
ROOT=Path(__file__).resolve().parents[2]
MODULES=['corpus_api','corpus_delivery','corpus_pipeline','corpus_platform','corpus_portal','corpus_recognition','corpus_registry','corpus_releases','corpus_search','corpus_topic_workflow','scripts','vendor','configs','db','tests','docs','deploy','参考文件中心']
EXCLUDED={'__pycache__','.cache','.pytest_cache','.git','runtime','填写示例'}
EXTENSIONS={'.py','.md','.json','.csv','.txt','.html','.css','.js','.sql','.ps1','.cmd','.yml','.yaml','.svg','.j2','.toml',''}
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
 text=re.sub(r'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+','<USER_HOME>',text,flags=re.I)
 for marker in MARKERS:text=text.replace(marker,'[REDACTED]')
 if SECRET.search(text):raise ValueError('Potential credential in distribution source')
 return text
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
   if source.suffix.lower() not in EXTENSIONS or source.name.startswith('.env') or source.name=='dynamic_topic_taxonomy.json':continue
   copy_file(source,target/relative)
 for name in ['README.md','DEVELOPMENT_STATUS.md','requirements.txt','environment.yml','.env.example','.gitignore','.gitattributes','THIRD_PARTY_NOTICES.md','run.ps1','启动语料库平台.cmd']:copy_file(ROOT/name,target/name)
 for source in (ROOT/'inputs').rglob('*'):
  if not source.is_file() or source.suffix not in {'.csv','.txt','.jsonl','.md'}:continue
  text=source.read_text(encoding='utf-8-sig')
  if source.suffix=='.csv':text=text.splitlines()[0]+'\n' if text.strip() else ''
  elif source.suffix=='.jsonl':text=''
  elif source.suffix=='.txt':text='的\n了\n和\n与\n' if source.name=='stop_terms.txt' else ''
  put(target/source.relative_to(ROOT),sanitize(text))
 for relative in ['data/raw/manual','data/raw/web','data/releases','data/workspaces','logs']:put(target/relative/'.gitkeep','')
 put(target/'corpus_topic_workflow/configs/dynamic_topic_taxonomy.json',json.dumps(dict(schema_version='3.0',taxonomy_name='用户话题体系',taxonomy_version=1,updated_at='',path_separator=' > ',max_levels=3,topics=[]),ensure_ascii=False,indent=2))
 p=target/'configs/pipeline.json';config=json.loads(p.read_text(encoding='utf-8'));config['workflow']['corpus_version']='0.1.0-draft';config['project']['name']='我的语料库';put(p,json.dumps(config,ensure_ascii=False,indent=2))
 p=target/'corpus_pipeline/service.py';put(p,'\n'.join(line for line in p.read_text(encoding='utf-8').splitlines() if '"term method PDF":' not in line)+'\n')
LAUNCH=r'''[CmdletBinding()]
param([ValidateSet('builder','search','check','topics','init-db')][string]$Mode='builder',[string]$BindHost='127.0.0.1',[int]$Port=8001)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime=Join-Path $PSScriptRoot 'runtime';$python=Join-Path $runtime 'python.exe'
$env:PYTHONNOUSERSITE='1';$env:PYTHONUTF8='1';$env:CORPUS_PROJECT_ROOT=$PSScriptRoot
if (Test-Path Env:PYTHONHOME) { Remove-Item Env:PYTHONHOME }
if (Test-Path Env:PYTHONPATH) { Remove-Item Env:PYTHONPATH }
if (-not (Test-Path -LiteralPath $python)) { Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'runtime-win64.zip') -DestinationPath $runtime }
$env:PATH="$runtime;$runtime\Library\bin;$runtime\DLLs;$runtime\Scripts;"+$env:PATH
if (-not (Test-Path -LiteralPath (Join-Path $runtime '.ready'))) {
 & $python (Join-Path $runtime 'Scripts\conda-unpack-script.py')
 if ($LASTEXITCODE -ne 0) { throw 'Runtime relocation failed' }
 New-Item -ItemType File -Path (Join-Path $runtime '.ready') | Out-Null
}
if ($Mode -eq 'builder') {
 if (-not (Test-Path -LiteralPath '.env')) { throw 'Copy .env.example to .env and configure PostgreSQL first; see README.md.' }
 & $python -m corpus_platform serve --setup
} elseif ($Mode -eq 'search') {
 & $python -m corpus_delivery.search --releases data/releases --host $BindHost --port $Port --open-browser
} elseif ($Mode -eq 'check') {
 & $python -m corpus_delivery.catalog data/releases
} elseif ($Mode -eq 'init-db') {
 & $python -m corpus_registry init-db
} else {
 & $python -m pip install -r requirements-topics.txt
 if ($LASTEXITCODE -ne 0) { throw 'Topic dependencies install failed' }
 & $python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"
}
if ($LASTEXITCODE -ne 0) { throw "Operation failed: $LASTEXITCODE" }
'''
def cmd(mode):return '@echo off\nchcp 65001 >nul\ncd /d "%~dp0"\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" -Mode '+mode+'\npause\n'
BUILDER='''# 当前构建发行包（Windows x64）

本包整合账号登录、独立项目、六步构建、版本发布与只读分享，不含原作者语料、账号、密钥和历史成果。

## 首次运行

1. 安装并启动 PostgreSQL，创建自己的数据库和用户，复制 .env.example 为 .env，填写 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD。
2. 双击 install-topics.cmd 安装 BERTopic 等依赖并下载模型，需要联网。本包附带核心 Python 与 BCC，未附完整话题环境及模型；首次安装可能较大。
3. 双击 start-builder.cmd；首次解包运行环境后在窗口创建账号，密码至少 3 位。浏览器打开 http://127.0.0.1:8000/。使用期间不要关闭服务窗口。
4. 登录并创建项目，上传语料、按六步流程构建，第 6 步完成索引发布后生成分享链接。

主流程和文件位置见 PLATFORM_GUIDE.md 与 corpus_portal/README.md。话题审核需要使用者自行配置接口；真实句法步骤依赖 configs/pipeline.json 中的句法服务，部署者需确认可用。测试捷径或合成句法不能代替正式研究成果。

## 文件保存与维护

项目位于 data/portal/projects/<项目编号>/；其中 data/raw/manual 保存上传原件，data/workspaces 保存加工及轮次，data/releases 保存正式版本和索引。账号和授权位于自己的 PostgreSQL。备份需包含项目、数据库和本机 .env，这些都不可公开打包。

首次解包后，在本包根目录执行 runtime/python.exe -m corpus_platform portal add-user user02 新增账号，portal reset-password 重置密码。管理员检查：runtime/python.exe -m corpus_platform doctor。回归方法见 tests/README.md。

移到新目录时推荐重新解包发行 ZIP，再迁移自己的数据、数据库和配置。环境重定位在首次运行时执行。

## 分享与部署

127.0.0.1 仅本机可用；其他电脑访问须按 deploy/portal/README.md 配置网络入口和 HTTPS，并设置 PORTAL_PUBLIC_URL。接收者通过有效分享链接只读检索，服务、数据库和指定索引持续可用时可继续使用。

独立只读服务用 start-search.cmd，但需先导入完整已建索引 Release 到 data/releases。导出见外层 02-corpus-data/README.md。第三方许可证见 THIRD_PARTY_NOTICES.md。新版本机 ZIP 不代表 GitHub 原发行文件已更新。
'''
DATA='''# 正式版本导出容器

本包只保留空 releases/；历史语料已按所有者要求删除，不含可直接检索的成果。

构建后在主程序根目录执行：

```powershell
python -m corpus_delivery.export data/portal/projects/PROJECT_DIRECTORY/data/releases/chinese-plus/VERSION deliveries/2026-10-09/02-corpus-data/releases
```

PROJECT_DIRECTORY 是无连字符的项目编号，VERSION 是实际版本。导出完整 manifest.json、corpus、index、metadata、statistics，保留 GB18030 与特殊标记；工具校验前后资产，拒绝覆盖同名目标。

将 releases/<语料库ID>/<版本>/ 完整复制到 03-search-service/data/releases/ 后校验并启动。不得包含 .env、账号记录、私人原文或模型密钥。仅在线分享时，优先用第 6 步授权链接，无需传数据包。

填入新成果后需重新生成 ZIP 和校验值，原空容器 ZIP 不会自动更新。
'''
SEARCH='''# 独立只读检索服务（Windows x64）

本包只有检索所需组件，没有构建工作台、历史语料或账号数据库。无需 PostgreSQL 和模型依赖。

1. 将完整已建索引的 releases/<语料库ID>/<版本>/ 放入本包 data/releases/，保持原编码与结构。
2. 双击 verify-data.cmd 校验；没有成果时提示待导入。
3. 双击 start-search.cmd，首次解包核心环境，打开 http://127.0.0.1:8001/search，使用期间保持服务窗口运行。

每次查询使用独立短进程调用 BCC，避免不同索引在同进程混读，增加一次启动耗时。

其他电脑访问时，在本包 PowerShell 中运行：

```powershell
.\\launch.ps1 -Mode search -BindHost 0.0.0.0 -Port 8001
```

使用主机的实际 IP 或域名访问，按网络环境配置防火墙和反向代理，公网建议 HTTPS。对方需要服务网址，单个 HTML 无法独立检索。此服务允许能访问网址的人读取已导入成果；需要授权、过期或撤销控制时使用主平台分享链接。

服务和索引持续可用时可继续检索；不开放上传、构建或整库下载。许可证见 THIRD_PARTY_NOTICES.md。本机新版文件尚未同步到 GitHub 原发行。
'''
def verify(root):
 count=0
 for path in root.rglob('*'):
  if not path.is_file() or path.name=='runtime-win64.zip':continue
  relative=path.relative_to(root)
  if path.name=='.env' or any(x in {'.git','__pycache__','runtime'} for x in relative.parts):raise ValueError('Private/generated file included')
  text=path.read_text(encoding='utf-8-sig',errors='ignore')
  if SECRET.search(text) or re.search(r'[A-Za-z]:[\\/]+Users[\\/]+Administrator',text,re.I) or any(x in text for x in MARKERS):raise ValueError('Private content included: '+str(relative))
  count+=1
 return count
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--runtime',type=Path,required=True);a=parser.parse_args();out=a.output.resolve()
 if out.exists():parser.error('Output already exists; choose a new directory')
 if not a.runtime.is_file():parser.error('Missing existing portable core runtime')
 out.mkdir(parents=True)
 builder=out/'01-builder';program(builder);program(builder/'portal_template')
 (builder/'README.md').rename(builder/'PLATFORM_GUIDE.md');put(builder/'README.md',BUILDER);put(builder/'launch.ps1',LAUNCH)
 put(builder/'requirements-topics.txt','bertopic>=0.17,<0.18\nsentence-transformers>=3,<4\numap-learn>=0.5.7,<0.6\nhdbscan>=0.8.40,<0.9\nscikit-learn>=1.5,<2\n')
 for name,mode in [('start-builder.cmd','builder'),('启动语料库平台.cmd','builder'),('install-topics.cmd','topics'),('init-database.cmd','init-db'),('start-search.cmd','search'),('verify-data.cmd','check')]:put(builder/name,cmd(mode))
 shutil.copy2(a.runtime,builder/'runtime-win64.zip')
 data=out/'02-corpus-data';put(data/'releases/.gitkeep','');put(data/'README.md',DATA)
 search=out/'03-search-service';search.mkdir()
 for name in ['corpus_search','corpus_releases','corpus_registry']:
  for source in (ROOT/name).rglob('*'):
   if source.is_file() and source.suffix=='.py' and '__pycache__' not in source.parts:copy_file(source,search/source.relative_to(ROOT))
 for name in ['corpus_delivery/__init__.py','corpus_delivery/catalog.py','corpus_delivery/search.py','corpus_delivery/export.py','corpus_api/__init__.py','corpus_api/service.py','corpus_api/models.py','corpus_api/web/search.html','corpus_api/web/ui-polish.css','corpus_portal/__init__.py','corpus_portal/workspaces.py','corpus_portal/isolated_search.py','THIRD_PARTY_NOTICES.md']:copy_file(ROOT/name,search/name)
 put(search/'requirements.txt','fastapi==0.141.1\nuvicorn==0.52.4\nhttpx==0.28.1\npydantic>=2.12,<3\npsycopg[binary]>=3.2,<4\npython-dotenv>=1,<2\nLangSC==2.0.7\n')
 put(search/'corpus_api/__init__.py','"""Read-only service models; no build application in this package."""\n')
 prefix=LAUNCH.split("if ($Mode -eq 'builder')")[0].replace("[ValidateSet('builder','search','check','topics','init-db')]","[ValidateSet('search','check')]").replace("$Mode='builder'","$Mode='search'")
 search_launch=prefix+"if ($Mode -eq 'search') {\n & $python -m corpus_delivery.search --releases data/releases --host $BindHost --port $Port --open-browser\n} else {\n & $python -m corpus_delivery.catalog data/releases\n}\nif ($LASTEXITCODE -ne 0) { throw \"Operation failed: $LASTEXITCODE\" }\n"
 put(search/'README.md',SEARCH);put(search/'launch.ps1',search_launch);put(search/'start-search.cmd',cmd('search'));put(search/'verify-data.cmd',cmd('check'));put(search/'data/releases/.gitkeep','');shutil.copy2(a.runtime,search/'runtime-win64.zip')
 manifest=dict(format=2,history_removed=True,accounts_included=False,corpus_included=False,archives=[])
 for directory,name in [(builder,'corpus-builder-win64-2026-10-09.zip'),(data,'corpus-data-empty-2026-10-09.zip'),(search,'corpus-search-win64-2026-10-09.zip')]:
  count=verify(directory);put(directory/'privacy-report.json',json.dumps(dict(files_checked=count,findings=0,runtime='Previously privacy-checked core runtime reused'),indent=2))
  archive=out/name
  with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
   for file in sorted(directory.rglob('*')):
    if file.is_file():z.write(file,file.relative_to(directory).as_posix(),compress_type=zipfile.ZIP_STORED if file.name=='runtime-win64.zip' else zipfile.ZIP_DEFLATED)
  with archive.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
  manifest['archives'].append(dict(name=name,bytes=archive.stat().st_size,sha256=digest))
 put(out/'delivery-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2));put(out/'SHA256SUMS.txt',''.join(x['sha256']+'  '+x['name']+'\n' for x in manifest['archives']))
 put(out/'README.md','# 当前共享交付\n\n01-builder 用于本地多用户构建；02-corpus-data 是空成果导出容器；03-search-service 用于独立只读检索。历史语料已按要求移除，各部分有 README。目录与 ZIP 同步，完整性见 SHA256SUMS.txt。核心环境仅支持 Windows x64，话题环境、模型及 PostgreSQL 需自行准备。本次未更新 GitHub 旧发行文件。\n')
 print(json.dumps(dict(built=True,archives=manifest['archives']),ensure_ascii=False))
if __name__=='__main__':main()
