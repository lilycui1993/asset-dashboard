#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双九宫格资产监测面板 —— GitHub Actions 自治构建脚本
数据源：公开 API（akshare / 东方财富 push2his / stooq），不依赖银河/星耀私有接口。
容错：每个标的独立 try/except，失败则沿用缓存（cache.json，跨运行保存）/ 种子值，
      保证页面永不空。部分估值分位在公开源不可得时沿用近期缓存。
"""
import json, os, sys, io, traceback, datetime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ---------- 静态配置：18 个标的的数据源 ----------
CONFIG = {
    # 全球大类资产
    "沪深300":       {"src": "cn",    "secid": "1.000300", "mtype": "PE-TTM",  "pe": "沪深300"},
    "孟买SENSEX":    {"src": "stooq", "sym": "bse.sn",     "mtype": "PE-TTM",  "pe": None},
    "中债10年期国债": {"src": "stooq", "sym": "cn10y",      "mtype": "收益率",   "pe": None},
    "恒生科技指数":   {"src": "stooq", "sym": "hstech.hk",  "mtype": "PE-TTM",  "pe": None},
    "可转债指数":     {"src": "cn",    "secid": "1.000832", "mtype": "价格",    "pe": None},
    "美债10年期国债": {"src": "stooq", "sym": "us10y",      "mtype": "收益率",   "pe": None},
    "纳斯达克100":    {"src": "stooq", "sym": "ndx.us",     "mtype": "PE-TTM",  "pe": None},
    "COMEX黄金":      {"src": "stooq", "sym": "xauusd",     "mtype": "价格",    "pe": None},
    "布伦特原油":     {"src": "stooq", "sym": "cbz.f",      "mtype": "价格",    "pe": None},
    # A股行业
    "中证食品饮料":   {"src": "cn",    "secid": "0.399396", "mtype": "PE-TTM",  "pe": "中证食品饮料"},
    "中证创新药":     {"src": "cn",    "secid": "1.931152", "mtype": "PE-TTM",  "pe": "中证创新药"},
    "中证半导体":     {"src": "cn",    "secid": "1.931865", "mtype": "PE-TTM",  "pe": "中证半导体"},
    "中证全指证券":   {"src": "cn",    "secid": "0.399975", "mtype": "PE-TTM",  "pe": "中证全指证券"},
    "中证银行":       {"src": "cn",    "secid": "0.399986", "mtype": "PE-TTM",  "pe": "中证银行"},
    "中证传媒":       {"src": "cn",    "secid": "0.399971", "mtype": "PE-TTM",  "pe": "中证传媒"},
    "中证有色金属":   {"src": "cn",    "secid": "1.930708", "mtype": "PE-TTM",  "pe": "中证有色金属"},
    "中证电力设备":   {"src": "cn",    "secid": "1.931560", "mtype": "PE-TTM",  "pe": "中证电力设备"},
    "中证军工":       {"src": "cn",    "secid": "0.399967", "mtype": "PE-TTM",  "pe": "中证军工"},
}


def log(*a):
    print("[build]", *a, flush=True)


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log("WARN", getattr(fn, "__name__", "?"), "->", repr(e))
        return None


# ---------- 行情获取 ----------
def em_kline(secid):
    """东方财富日线，返回 [(date, close), ...]"""
    import requests
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1&fields2=f51,f53&klt=101&fqt=0&end=20500101&lmt=260")
    j = requests.get(url, headers=UA, timeout=25).json()
    kl = (j.get("data") or {}).get("klines") or []
    rows = []
    for s in kl:
        p = s.split(",")
        rows.append((p[0], float(p[2])))
    return rows


def stooq_series(symbol):
    """stooq 日线 CSV，返回 [(date, close), ...]"""
    import requests, csv
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    txt = requests.get(url, headers=UA, timeout=25).text
    rows = []
    for ln in txt.strip().split("\n")[1:]:
        if not ln.strip():
            continue
        parts = ln.split(",")
        try:
            rows.append((parts[0], float(parts[4])))
        except (IndexError, ValueError):
            continue
    return rows


def series_to_metrics(rows):
    """从 [(date,close)] 计算 最新价/昨收涨跌%/年初至今%"""
    if not rows or len(rows) < 2:
        return None
    last_d, last = rows[-1]
    prev_d, prev = rows[-2]
    prev_day = round((last / prev - 1) * 100, 2) if prev else None
    # 年初至今：找 2026-01-01 之后第一行
    jan1 = None
    for d, c in rows:
        if d >= "2026-01-01":
            jan1 = c
            break
    if jan1 is None:
        jan1 = rows[0][1]
    ytd = round((last / jan1 - 1) * 100, 2) if jan1 else None
    return {"metric": round(last, 2), "prev_day": prev_day, "ytd": ytd}


def fetch_price(name, cfg):
    if cfg["src"] == "cn":
        rows = safe(em_kline, cfg["secid"])
    else:
        rows = safe(stooq_series, cfg["sym"])
    if not rows:
        return None
    return series_to_metrics(rows)


# ---------- PE / 分位获取（最佳努力） ----------
def fetch_pe_live(pe_name):
    """返回 (current_pe, percentile_10y) 或 (None, None)"""
    try:
        import akshare as ak
        # 1) 韭圈儿长期 PE
        try:
            df = ak.stock_index_pe_lg(symbol=pe_name)
            pe_col = [c for c in df.columns if "市盈" in c or c.lower() == "pe"]
            if pe_col and "date" in [c.lower() for c in df.columns]:
                df = df.copy()
                df.columns = [c.lower() if c.lower() == "date" else c for c in df.columns]
                df = df.sort_values("date")
                vals = df[pe_col[0]].dropna().astype(float).tolist()
                if vals:
                    cur = vals[-1]
                    window = [v for v in vals if v is not None][-2520:]  # ~10y 交易日
                    pct = round(sum(1 for v in window if v <= cur) / len(window) * 100, 2)
                    return cur, pct
        except Exception as e:
            log("pe_lg failed for", pe_name, e)
        # 2) CSIndex 历史（含 PE）
        try:
            code = pe_name_to_csindex(pe_name)
            if code:
                df = ak.stock_index_hist_csindex(symbol=code, period="daily")
                pe_col = [c for c in df.columns if c.lower() == "pe"]
                if pe_col:
                    df = df.dropna(subset=pe_col)
                    vals = df[pe_col[0]].astype(float).tolist()
                    if vals:
                        cur = vals[-1]
                        window = vals[-2520:]
                        pct = round(sum(1 for v in window if v <= cur) / len(window) * 100, 2)
                        return cur, pct
        except Exception as e:
            log("csindex pe failed for", pe_name, e)
    except Exception as e:
        log("akshare import failed:", e)
    return None, None


def pe_name_to_csindex(name):
    m = {
        "沪深300": "000300", "中证银行": "399986", "中证军工": "399967",
        "中证全指证券": "399975", "中证半导体": "931865", "中证传媒": "399971",
        "中证食品饮料": "399396", "中证创新药": "931152", "中证有色金属": "930708",
        "中证电力设备": "931560",
    }
    return m.get(name)


def valuate(p):
    if p is None:
        return None
    if p < 20:
        return "低估"
    if p < 50:
        return "偏低"
    if p < 80:
        return "中性"
    return "高估"


# ---------- 市场资讯（规则化自动生成） ----------
def build_news(global_assets, a_share):
    def sgn(x):
        return "涨" if (x or 0) >= 0 else "跌"
    def pct(x):
        return f"{abs(x):.2f}%"
    items = []
    hs = next((a for a in global_assets if a["name"] == "沪深300"), None)
    if hs:
        items.append({
            "title": f"A股收评：沪深300{sgn(hs['prev_day'])}{pct(hs['prev_day'])}，年初至今{pct(hs['ytd'])}",
            "summary": f"沪深300 最新 PE-TTM {hs['metric']:.1f}，近10年分位 {hs['percentile']:.1f}%（{hs['valuation']}）。"
                       f"数据由公开行情接口自动抓取，与银河终端可能存在偏差。",
            "impact": "宽基指数估值与涨跌由公开数据源自动更新"
        })
    # 行业涨跌榜
    movers = sorted(a_share, key=lambda a: (a["prev_day"] or 0))
    if movers:
        top_up = movers[-1]
        top_dn = movers[0]
        items.append({
            "title": f"行业涨跌：{top_up['name']}领涨{sgn(top_up['prev_day'])}{pct(top_up['prev_day'])}，{top_dn['name']}领跌{sgn(top_dn['prev_day'])}{pct(top_dn['prev_day'])}",
            "summary": f"申万/中证行业指数中，{top_up['name']}（PE {top_up['metric']:.1f}，分位{top_up['percentile']:.1f}%）"
                       f"表现最强，{top_dn['name']}（PE {top_dn['metric']:.1f}，分位{top_dn['percentile']:.1f}%）表现最弱。",
            "impact": "行业轮动由公开行情自动计算"
        })
    hk = next((a for a in global_assets if a["name"] == "恒生科技指数"), None)
    if hk:
        items.append({
            "title": f"港股：恒生科技指数{sgn(hk['prev_day'])}{pct(hk['prev_day'])}，年初至今{pct(hk['ytd'])}",
            "summary": f"恒生科技指数最新 {hk['metric']:.0f}，昨日涨跌幅 {hk['prev_day']:.2f}%。",
            "impact": "港股科技板块波动由公开源更新"
        })
    us = next((a for a in global_assets if a["name"] == "纳斯达克100"), None)
    if us:
        items.append({
            "title": f"美股：纳斯达克100{sgn(us['prev_day'])}{pct(us['prev_day'])}，年初至今{pct(us['ytd'])}",
            "summary": f"纳斯达克100 最新 {us['metric']:.0f}，年初至今 {us['ytd']:.2f}%。",
            "impact": "美股科技由公开源自动更新"
        })
    gold = next((a for a in global_assets if a["name"] == "COMEX黄金"), None)
    if gold:
        items.append({
            "title": f"商品：COMEX黄金{sgn(gold['prev_day'])}{pct(gold['prev_day'])}报{gold['metric']:.0f}，年初至今{pct(gold['ytd'])}",
            "summary": f"黄金最新 {gold['metric']:.0f}，昨日 {gold['prev_day']:.2f}%。",
            "impact": "贵金属由公开源更新"
        })
    oil = next((a for a in global_assets if a["name"] == "布伦特原油"), None)
    if oil:
        items.append({
            "title": f"商品：布伦特原油{sgn(oil['prev_day'])}{pct(oil['prev_day'])}报{oil['metric']:.1f}，年初至今{pct(oil['ytd'])}",
            "summary": f"布伦特原油最新 {oil['metric']:.1f}，昨日 {oil['prev_day']:.2f}%。",
            "impact": "原油由公开源更新"
        })
    ub = next((a for a in global_assets if a["name"] == "美债10年期国债"), None)
    cb = next((a for a in global_assets if a["name"] == "中债10年期国债"), None)
    if ub or cb:
        parts = []
        if ub: parts.append(f"美债10Y {ub['metric']:.2f}%")
        if cb: parts.append(f"中债10Y {cb['metric']:.2f}%")
        items.append({
            "title": "利率：" + "，".join(parts),
            "summary": "中美10年期国债收益率为公开源自动抓取（stooq）。",
            "impact": "利率中枢由公开源更新"
        })
    items.append({
        "title": "数据说明：本页由 GitHub Actions 每日自动构建",
        "summary": "行情来自 akshare / 东方财富 / stooq 等公开接口；PE 估值分位在公开源不可得时沿用近期缓存。"
                   "与星耀数智（银河证券）驱动的 CloudStudio 版本可能存在偏差，仅供参考。",
        "impact": "自动化说明"
    })
    while len(items) < 8:
        items.append(items[-1])
    return items[:8]


# ---------- 主流程 ----------
def main():
    base = os.path.dirname(os.path.abspath(__file__))
    corr = json.load(open(os.path.join(base, "corr.json"), encoding="utf-8"))
    seed = json.load(open(os.path.join(base, "seed_cache.json"), encoding="utf-8"))

    # 缓存：优先 cache.json（跨运行 artifact），否则种子
    cache_path = os.path.join(base, "cache.json")
    if os.path.exists(cache_path):
        cache = json.load(open(cache_path, encoding="utf-8"))
        log("loaded cache.json")
    else:
        cache = seed
        log("no cache.json, using seed")

    today = datetime.date.today()
    # 周末/非交易日也要跑（保证页面可访问），但数据可能滞后——这里只在交易日更新日期
    cache_by_name = {}
    for grp in ("global_assets", "a_share_industries"):
        for a in cache.get(grp, []):
            cache_by_name[a["name"]] = a

    fetched = 0
    for name, cfg in CONFIG.items():
        old = cache_by_name.get(name, {})
        price = safe(fetch_price, name, cfg)
        pe, pe_pct = (None, None)
        if cfg.get("pe"):
            pe, pe_pct = fetch_pe_live(cfg["pe"]) or (None, None)
        rec = dict(old)
        rec["name"] = name
        rec["metric_type"] = cfg["mtype"]
        if price:
            rec["metric"] = price["metric"]
            rec["prev_day"] = price["prev_day"]
            rec["ytd"] = price["ytd"]
            fetched += 1
        if pe is not None:
            rec["metric"] = round(pe, 2)
        if pe_pct is not None:
            rec["percentile"] = pe_pct
            rec["valuation"] = valuate(pe_pct)
        # 兜底：若字段缺失则保留旧值
        for k in ("metric", "percentile", "valuation", "prev_day", "ytd"):
            if rec.get(k) is None and old.get(k) is not None:
                rec[k] = old[k]
        cache_by_name[name] = rec
        log(f"{name}: price={'Y' if price else 'N'} pe={pe} pct={pe_pct}")

    new_global = [cache_by_name[n] for n in CONFIG if n in [a["name"] for a in seed["global_assets"]]]
    new_a = [cache_by_name[n] for n in CONFIG if n in [a["name"] for a in seed["a_share_industries"]]]

    news = build_news(new_global, new_a)

    out = {
        "update_date": today.strftime("%Y-%m-%d"),
        "generated_at": today.strftime("%Y-%m-%d") + " 09:05:00 (GitHub Actions 自动构建)",
        "data_source": "公开API(akshare/东方财富/stooq)，部分估值分位沿用缓存",
        "global_assets": new_global,
        "a_share_industries": new_a,
        "news": news,
    }
    out.update(corr)

    # 写回缓存供下次/artifact 使用
    json.dump({k: out[k] for k in ("global_assets", "a_share_industries", "news", "update_date", "generated_at")},
              open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 渲染
    tpl = open(os.path.join(base, "template.html"), encoding="utf-8").read()
    dumped = json.dumps(out, ensure_ascii=False)
    json.dump(out, open(os.path.join(base, "embedded_data.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    html = tpl.replace("__EMBEDDED_DATA__", dumped)
    with open(os.path.join(base, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    log(f"index.html written, fetched_price={fetched}/{len(CONFIG)}, update_date={out['update_date']}")


if __name__ == "__main__":
    main()
