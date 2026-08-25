import json
h = open("index.html", encoding="utf-8").read()
marker = "const EMBEDDED_DATA = "
s = h.index(marker) + len(marker)
# find opening brace
bo = h.index("{", s)
depth = 0
inst = False
esc = False
j = bo
while j < len(h):
    c = h[j]
    if inst:
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            inst = False
    else:
        if c == '"':
            inst = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
    j += 1
obj = h[bo:j + 1]
d2 = json.loads(obj)
print("obj tail:", repr(obj[-30:]))
print("keys:", list(d2.keys()))
print("global_assets:", len(d2["global_assets"]), "a_share:", len(d2["a_share_industries"]), "news:", len(d2["news"]))
print("sample 沪深300:", d2["global_assets"][0]["metric"], d2["global_assets"][0]["percentile"], d2["global_assets"][0]["valuation"])
print("data_source:", d2.get("data_source"))
print("first news:", d2["news"][0]["title"])
print("HTML EMBEDDED_DATA VALID")
