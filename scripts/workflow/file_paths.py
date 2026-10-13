"""Native Windows extended paths for I/O; ordinary paths stay in metadata."""
import os
from pathlib import Path

def io_path(path):
    path=Path(path)
    if os.name!='nt':return path
    value=str(path.absolute())
    if value.startswith('\\\\?\\'):return Path(value)
    if value.startswith('\\\\'):return Path('\\\\?\\UNC\\'+value[2:])
    return Path('\\\\?\\'+value)
