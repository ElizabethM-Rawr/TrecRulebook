from html.parser import HTMLParser
from pathlib import Path
import json
import re

path = Path(__file__).resolve().parent.parent / 'docs' / 'index.html'
html = path.read_text(encoding='utf-8')

class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.imgs = []
        self.metas = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'id' in a: self.ids.append(a['id'])
        if tag == 'a' and 'href' in a: self.links.append(a['href'])
        if tag == 'img': self.imgs.append((a.get('src'), a.get('alt')))
        if tag == 'meta': self.metas.append(a)

p = P(); p.feed(html)
print('title', re.search(r'<title>(.*?)</title>', html, re.S).group(1))
print('ids', len(p.ids), 'unique', len(set(p.ids)))
print('duplicate ids', sorted({i for i in p.ids if p.ids.count(i) > 1}))
print('images', p.imgs)
for src, alt in p.imgs:
    if src and not src.startswith('http'):
        print('  local image exists:', (path.parent/src).exists(), src)
print('local links')
for href in sorted(set(p.links)):
    if href.startswith(('#','http','mailto:')): continue
    print(href, (path.parent/href).exists())
print('metas')
for m in p.metas:
    if m.get('name') or m.get('property'):
        print(' ', m.get('name') or m.get('property'), '=', (m.get('content') or '')[:80])
print('json-ld blocks')
for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
    obj = json.loads(block)
    print(' ', obj['@type'], 'OK')
print('word count approx', len(re.sub(r'<[^>]+>', ' ', html).split()))
