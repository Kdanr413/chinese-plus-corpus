from pathlib import Path
import os
from dotenv import load_dotenv

def main():
    load_dotenv(Path.cwd()/'.env', override=False)
    os.environ.setdefault('API_OPEN_BROWSER','1')
    from corpus_platform.cli import main as platform_main
    return platform_main(['serve'])

if __name__=='__main__':
    raise SystemExit(main())
