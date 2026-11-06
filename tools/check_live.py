import re
import urllib.request

base = "https://elizabethm-rawr.github.io/TrecRulebook/"
html = urllib.request.urlopen(base, timeout=30).read().decode("utf-8")
print("html_bytes", len(html))
print("title", re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip())
print("description", re.search(r'<meta name="description" content="(.*?)"', html).group(1)[:180])
print("og_image", re.search(r'<meta property="og:image" content="(.*?)"', html).group(1))
for path in ("sitemap.xml", "robots.txt", "social-card.png", "search.png", "compare.png"):
    response = urllib.request.urlopen(base + path, timeout=30)
    print(path, response.status, response.headers.get("content-type"), response.headers.get("content-length"))
