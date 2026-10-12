"""Build one complete reusable source release without touching live data."""
from pathlib import Path
import argparse, hashlib, json, zipfile
from source_snapshot import ROOT, program, put, verify, copy_file

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    # Do not permit an output anywhere in protected user/runtime data.
    if output == ROOT or output.is_relative_to(ROOT/'data') or output.is_relative_to(ROOT/'portal_template'):
        parser.error('Output must not be the project root, data or template directory')
    if output.exists(): parser.error('Output already exists; choose a new directory')
    output.mkdir(parents=True)
    source=output/'corpus-source'
    program(source)
    program(source/'portal_template')
    if (ROOT/'data/README.md').exists(): copy_file(ROOT/'data/README.md',source/'data/README.md')
    for base in [source,source/'portal_template']:
        changes=base/'corpus_topic_workflow/configs/dynamic_topic_taxonomy_changes.json'
        if changes.exists():
            put(changes,json.dumps({'schema_version':'3.0','taxonomy_file':'dynamic_topic_taxonomy.json','current_taxonomy_version':1,'changes':[]},ensure_ascii=False,indent=2)+'\n')
        gitignore=base/'.gitignore'
        text=gitignore.read_text(encoding='utf-8').replace('/vendor/','').replace('/portal_template/','')
        # Keep normative empty input interfaces tracked; project input data stays local.
        text='\n'.join(line for line in text.splitlines() if not line.startswith(('/inputs/', '!/inputs/')))
        text+='\n/runtime/\n*.zip\n'
        put(gitignore,text)
    count=verify(source)
    archive=output/'corpus-source.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for path in sorted(source.rglob('*')):
            if path.is_file():z.write(path,path.relative_to(source).as_posix())
    with zipfile.ZipFile(archive) as z:
        bad=z.testzip()
        if bad:raise ValueError('ZIP CRC failed: '+bad)
    with archive.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    put(output/'SHA256SUMS.txt',digest+'  '+archive.name+'\n')
    manifest={'format':3,'mode':'unified-source','archive':archive.name,'bytes':archive.stat().st_size,'sha256':digest,'source_files_checked':count,'private_findings':0,'raw_data_included':False,'user_accounts_included':False,'runtime_included':False}
    put(output/'release-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    put(output/'README.md','# 完整源码交付\n\n个人与服务器均从 GitHub 拉取相同完整源码。corpus-source.zip 是当前源码副本，解压后见 README.md 安装。未附带 Python 环境、模型、PostgreSQL、个人语料、账号或密钥。SHA256SUMS.txt 校验下载完整性。\n')
    print(json.dumps(manifest,ensure_ascii=False))

if __name__=='__main__':main()
