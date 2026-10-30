"""Explicit allowlist: never package credentials, installed runtimes or user caches."""
from pathlib import Path
import zipfile
ROOT = Path(__file__).resolve().parents[1]
paths = [ROOT/name for name in ('README.md','requirements.txt','.gitignore','.gitattributes',
    'install.cmd','start.cmd','disconnect-keep-desktop.cmd','assistant/settings.example.json',
    'assistant/prompt.example.txt')]
paths += list((ROOT/'assistant').glob('*.py'))
paths += list((ROOT/'assistant'/'vendor').glob('*.py'))
paths += [ROOT/'assistant'/'vendor'/name for name in ('NOTICE.txt','LICENSE-wechatauto.txt')]
paths += list((ROOT/'tests').glob('test_*.py'))
paths += [ROOT/'tools'/'build_release.py']
destination = ROOT/'dist'/'wechat-deepseek-assistant-source.zip'
destination.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
    for path in sorted(set(paths)):
        z.write(path,'wechat-deepseek-assistant/'+path.relative_to(ROOT).as_posix())
with zipfile.ZipFile(destination) as z:
    assert z.testzip() is None
print(destination)
