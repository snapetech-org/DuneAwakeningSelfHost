"""Upgrade pip's actual pkg_resources subset from the fixed installed distribution.

Keep pip's namespace isolation and upstream jaraco adapter, and update its SBOM
only after replacing code. No vulnerability suppression is used.
"""
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import pathlib
import re
import shutil
import pip

assert pip.__version__ == '26.3'
assert importlib.metadata.version('setuptools') == '80.10.2'
source = pathlib.Path(importlib.util.find_spec('pkg_resources').origin).parent
vendor = pathlib.Path(pip.__file__).parent / '_vendor'
assert source.is_dir() and not source.is_symlink()
assert (vendor / 'pkg_resources').is_dir()
assert not (vendor / 'setuptools').exists()
text = (source / '__init__.py').read_text()
# The upstream vendoring process strips setuptools-only path/bootstrap state.
text, count = re.subn(r"(?m)^sys\.path\.extend.*\n# workaround for #4476\nsys\.modules\.pop\('backports', None\)\n", '', text)
assert count == 1
text = text.replace('import packaging.markers', 'from pip._vendor import packaging\nimport packaging.markers', 1)
for name in ('markers', 'requirements', 'specifiers', 'utils', 'version'):
    old = 'import packaging.' + name
    assert text.count(old) == 1
    text = text.replace(old, 'import pip._vendor.packaging.' + name)
old = 'from jaraco.text import drop_comment, join_continuation, yield_lines'
assert text.count(old) == 1
text = text.replace(old, 'from pip._internal.utils._jaraco_text import drop_comment, join_continuation, yield_lines')
old = 'from platformdirs import user_cache_dir as _user_cache_dir'
assert text.count(old) == 1
text = text.replace(old, 'from pip._vendor.platformdirs import user_cache_dir as _user_cache_dir')
# Match pip's upstream removal of the deprecated API warning.
tree = ast.parse(text)
warns = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == 'warnings' and n.value.func.attr == 'warn']
assert len(warns) == 1
lines = text.splitlines(keepends=True)
n = warns[0]
del lines[n.lineno-1:n.end_lineno]
text = ''.join(lines)
ast.parse(text)
licenses = {p.name: p.read_bytes() for p in (vendor / 'pkg_resources').glob('*LICENSE*')}
assert licenses
shutil.rmtree(vendor / 'pkg_resources')
shutil.copytree(source, vendor / 'pkg_resources', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
(vendor / 'pkg_resources/__init__.py').write_text(text)
# Preserve pip's distributed license for the same upstream project.
for name, payload in licenses.items():
    (vendor / 'pkg_resources' / name).write_bytes(payload)
inventory = vendor / 'vendor.txt'
data = inventory.read_text()
data, count = re.subn(r'(?m)^setuptools==70\.3\.0$', 'setuptools==80.10.2', data)
assert count == 1
inventory.write_text(data)
bom = vendor / 'bom.cdx.json'
data = json.loads(bom.read_text())
rows = [x for x in data['components'] if x['name'] == 'setuptools']
assert len(rows) == 1 and rows[0]['version'] == '70.3.0'
row = rows[0]
# Hashes describe the original wheel, not this replaced subset: remove stale hashes.
row.pop('hashes', None)
for key in ('bom-ref', 'purl'):
    if key in row:
        row[key] = row[key].replace('70.3.0', '80.10.2')
row['version'] = '80.10.2'
data = json.loads(json.dumps(data).replace('pkg:pypi/setuptools@70.3.0', 'pkg:pypi/setuptools@80.10.2'))
bom.write_text(json.dumps(data, indent=2) + '\n')
print(json.dumps({'pip': pip.__version__, 'vendoredSetuptoolsSubset': '80.10.2', 'codeSha256': hashlib.sha256(text.encode()).hexdigest()}))
