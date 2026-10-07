import os, re, sys, csv, pdfplumber
P = sys.argv[1] if len(sys.argv) > 1 else "caper.pdf"
OUT = os.environ.get("CAPER_STORES_CSV", "caper_stores.csv")

def clean(c): return (c or "").replace("\n"," ").strip()
STREET=r"\b(st|ave|rd|blvd|dr|ln|way|pike|hwy|rte|route|turnpike|mall|pkwy|pl|ct|broadway|terrace|highway|road|street|avenue|drive|lane|circle|cir|square|sq)\b"
def find_addr(cells):
    z=[c for c in cells if re.match(r"\s*\d",c) and re.search(r"\b\d{5}\b",c)]
    if z: return max(z,key=len)
    s=[c for c in cells if re.match(r"\s*\d+\s+\w",c) and re.search(STREET,c,re.I)]
    return max(s,key=len) if s else ""
IDPAT=re.compile(r"(prod|qvs|kroger|sobeys|geisslers|mckeevers|bowman|davis|wakefern|gfh|schnucks|aldi|sprouts|weis|gelson)\s*-",re.I)
def find_name(cells,addr):
    for c in cells:
        if not c or c==addr or not re.search(r"[A-Za-z]",c): continue
        if IDPAT.search(c): continue
        if re.search(r"\d{3}[\)\-]\s*\d{3}",c) or "@" in c: continue
        if re.match(r"america/|gmt|mountain time|pacific|central|eastern",c,re.I): continue
        if re.fullmatch(r"WF\s*\d+",c,re.I): continue
        if c.lower() in ("shoprite","the fresh grocer","m3","m1","m2","m3l","tbd","n/a","yes","no"): continue
        return c
    return ""
def find_id(cells):
    for c in cells:
        m=re.search(r"(prod|qvs|kroger|sobeys|geisslers|mckeevers|bowman\w*|davis|wakefern|gfh|schnucks|aldi|sprouts|weis|gelson\w*)\s*-\s*[\w-]*\d+",c,re.I)
        if m: return re.sub(r"\s*-\s*","-",m.group(0))
    return ""
def num_from(name):
    m=re.search(r"(\d{2,})\s*$",name.strip())
    return m.group(1) if m else ""

rows=[]; seen=set()
with pdfplumber.open(P) as pdf:
    for pi,pg in enumerate(pdf.pages):
        for t in (pg.extract_tables() or []):
            for r in t:
                cells=[clean(x) for x in r]
                if not any(cells): continue
                addr=find_addr(cells)
                if not addr: continue                 # address is the join key — require it
                name=find_name(cells,addr)
                sid=find_id(cells)
                num=num_from(name) or (re.search(r"(\d+)$",sid).group(1) if re.search(r"(\d+)$",sid) else "")
                key=(name.lower(),addr.lower())
                if key in seen: continue
                seen.add(key)
                rows.append([name,addr,num,sid,pi+1])
with open(OUT,"w",newline="") as f:
    w=csv.writer(f); w.writerow(["store_name","address","store_number","store_id","pdf_page"])
    w.writerows(rows)
print("rows with address:",len(rows))
withzip=sum(1 for r in rows if re.search(r"\b\d{5}\b",r[1]))
print("…of which have a ZIP:",withzip)
print("…with a store name:",sum(1 for r in rows if r[0]))
print("\nSAMPLE:")
for r in rows[:18]: print(" |".join(str(x) for x in r[:4]))
