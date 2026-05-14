from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from html import unescape
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import feedparser
import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import DBSCAN


# =========================
# Paths / Config
# =========================
ROOT = Path.home() / "workspace" / "news-briefing"
CFG_PATH = ROOT / "configs" / "briefing_config.json"
FEEDS_CSV = ROOT / "configs" / "feeds.csv"
DB_PATH = ROOT / "db" / "news.db"
OUT_PATH = ROOT / "data" / "processed" / "morning_briefing.md"

assert CFG_PATH.exists(), f"Missing config: {CFG_PATH}"
assert FEEDS_CSV.exists(), f"Missing feeds.csv: {FEEDS_CSV}"

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))

HOURS = int(cfg["hours"])
TOP_N_POL = int(cfg["top_n_politics"])
TOP_N_OTH = int(cfg["top_n_other"])
EPS_MAP = cfg["eps"]
SIM_MIN = float(cfg["sim_min"])
LOW_SIM = float(cfg["low_sim"])


# =========================
# RSS -> DB
# =========================
def normalize_url(u: str | None) -> str | None:
    if not isinstance(u, str) or not u.strip():
        return None
    p = urlparse(u.strip())
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))  # drop query/fragment


def make_item_id(feed_url: str | None, guid: str | None, link: str | None, title: str | None, published: str | None) -> str:
    base = guid or normalize_url(link) or f"{title}|{published}|{feed_url}"
    base = (base or "").strip()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def fetch_feed(feed_name: str, feed_url: str, limit: int = 50) -> pd.DataFrame:
    d = feedparser.parse(feed_url)
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows = []
    for e in d.entries[:limit]:
        link = getattr(e, "link", None)
        guid = getattr(e, "id", None) or getattr(e, "guid", None)
        title = getattr(e, "title", None)
        published = getattr(e, "published", None)
        summary = getattr(e, "summary", None)

        link_norm = normalize_url(link)
        item_id = make_item_id(feed_url, guid, link_norm, title, published)

        rows.append({
            "item_id": item_id,
            "feed_name": feed_name,
            "feed_url": feed_url,
            "title": title,
            "link": link,
            "link_norm": link_norm,
            "published": published,
            "summary": summary,
            "guid": guid,
            "fetched_at": fetched_at,
        })
    return pd.DataFrame(rows)


def ensure_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS items (
        item_id TEXT PRIMARY KEY,
        feed_name TEXT,
        feed_url TEXT,
        title TEXT,
        link TEXT,
        link_norm TEXT,
        published TEXT,
        summary TEXT,
        guid TEXT,
        fetched_at TEXT
    )
    """)
    conn.commit()


def ingest_all_feeds(limit: int = 50) -> dict:
    feeds_df = pd.read_csv(FEEDS_CSV)
    conn = sqlite3.connect(DB_PATH)
    ensure_db(conn)
    cur = conn.cursor()

    before = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_fetched = 0
    total_inserted = 0

    for _, r in feeds_df.iterrows():
        fn = str(r["feed_name"])
        url = str(r["feed_url"])

        df = fetch_feed(fn, url, limit=limit)
        total_fetched += len(df)
        if len(df) == 0:
            continue

        recs = df[[
            "item_id","feed_name","feed_url","title","link","link_norm","published","summary","guid","fetched_at"
        ]].to_records(index=False)

        cur.executemany("""
        INSERT OR IGNORE INTO items
        (item_id, feed_name, feed_url, title, link, link_norm, published, summary, guid, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, list(recs))
        conn.commit()

    after = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_inserted = after - before

    conn.close()
    return {
        "feeds": int(len(feeds_df)),
        "db_before": int(before),
        "db_after": int(after),
        "fetched": int(total_fetched),
        "inserted": int(total_inserted),
    }


# =========================
# Briefing generation
# =========================
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(x):
    if not isinstance(x, str):
        return ""
    x = unescape(x)
    x = TAG_RE.sub(" ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def make_display_title(row: pd.Series) -> str:
    t = row.get("title_clean", "")
    if isinstance(t, str) and t.strip():
        return t.strip()
    t2 = clean_text(row.get("title", ""))
    if isinstance(t2, str) and t2.strip():
        return t2.strip()
    s = row.get("summary_clean", "")
    if isinstance(s, str) and s.strip():
        s = s.strip()
        return (s[:80] + "…") if len(s) > 80 else s
    return "(제목 없음)"


def cluster_df(dfx: pd.DataFrame, eps: float, min_samples: int = 2, min_df: int = 2):
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=min_df)
    X = vec.fit_transform(dfx["text"])
    labels = DBSCAN(eps=float(eps), min_samples=min_samples, metric="cosine").fit_predict(X)
    dfo = dfx.copy()
    dfo["cluster"] = labels
    return dfo, X


def rep_index_by_centroid(dfo: pd.DataFrame, X):
    idx_to_pos = {idx: pos for pos, idx in enumerate(dfo.index)}
    rows = []
    core = dfo[dfo["cluster"] != -1]
    for c, g in core.groupby("cluster"):
        idxs = g.index.tolist()
        Xc = X[[idx_to_pos[i] for i in idxs], :]
        centroid = np.asarray(Xc.mean(axis=0)).ravel()
        centroid = centroid / (np.linalg.norm(centroid) + 1e-12)
        sims = Xc.dot(centroid)
        rep_idx = idxs[int(np.argmax(sims))]

        top3 = g["display_title"].head(3).tolist()
        rows.append({
            "cluster": int(c),
            "size": int(len(g)),
            "sources": int(g["feed_name"].nunique()),
            "representative_title": dfo.loc[rep_idx, "display_title"],
            "representative_link": dfo.loc[rep_idx, "link"],
            "top3_titles": " / ".join(top3),
        })
    return (pd.DataFrame(rows)
            .sort_values(["size","sources","cluster"], ascending=[False, False, True])
            .reset_index(drop=True))


def rep_politics_fill(pol_df: pd.DataFrame, X, sim_min: float):
    buckets = ["conservative","progressive","centrist"]
    idx_to_pos = {idx: pos for pos, idx in enumerate(pol_df.index)}
    bucket_candidates_global = {b: pol_df[pol_df["politics_bucket"] == b].index.tolist() for b in buckets}

    used = {b:set() for b in buckets}
    rows = []
    core = pol_df[pol_df["cluster"] != -1]

    for c, g in core.groupby("cluster"):
        idxs = g.index.tolist()
        Xc = X[[idx_to_pos[i] for i in idxs], :]
        centroid = np.asarray(Xc.mean(axis=0)).ravel()
        centroid = centroid / (np.linalg.norm(centroid) + 1e-12)

        row = {"cluster": int(c), "size": int(len(g)), "sources": int(g["feed_name"].nunique())}
        filled = 0

        for b in buckets:
            # 1) cluster 내부 우선
            cand_in = [i for i in g[g["politics_bucket"] == b].index.tolist() if i not in used[b]]
            cand = cand_in if cand_in else [i for i in bucket_candidates_global[b] if i not in used[b]]

            if not cand:
                row[f"{b}_title"] = ""
                row[f"{b}_link"] = ""
                row[f"{b}_sim"] = 0.0
                continue

            Xb = X[[idx_to_pos[i] for i in cand], :]
            sims = Xb.dot(centroid)
            best_pos = int(np.argmax(sims))
            best_sim = float(sims[best_pos])
            best_idx = cand[best_pos]

            if best_sim < sim_min:
                row[f"{b}_title"] = ""
                row[f"{b}_link"] = ""
                row[f"{b}_sim"] = best_sim
            else:
                row[f"{b}_title"] = pol_df.loc[best_idx, "display_title"]
                row[f"{b}_link"]  = pol_df.loc[best_idx, "link"] or ""
                row[f"{b}_sim"]   = best_sim
                used[b].add(best_idx)
                filled += 1

        row["bucket_filled"] = filled
        rows.append(row)

    return (pd.DataFrame(rows)
            .sort_values(["bucket_filled","size","sources","cluster"], ascending=[False, False, False, True])
            .reset_index(drop=True))


def generate_briefing() -> dict:
    feeds_df = pd.read_csv(FEEDS_CSV)
    feed_to_section = dict(zip(feeds_df["feed_name"], feeds_df["section"]))
    feed_to_bucket  = dict(zip(feeds_df["feed_name"], feeds_df["politics_bucket"].fillna("")))

    since = (datetime.now(timezone.utc) - timedelta(hours=HOURS)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
    SELECT item_id, feed_name, title, summary, link, fetched_at
    FROM items
    WHERE fetched_at >= ?
    """, conn, params=[since])
    conn.close()

    df["section"] = df["feed_name"].map(feed_to_section)
    df["politics_bucket"] = df["feed_name"].map(feed_to_bucket).fillna("")

    df["title_clean"] = df["title"].map(clean_text)
    df["summary_clean"] = df["summary"].map(clean_text)
    df["text"] = (df["title_clean"] + " " + df["summary_clean"]).str.strip()
    df = df[df["text"].str.len() >= 15].copy()
    df["display_title"] = df.apply(make_display_title, axis=1)

    bucket_kr = {"conservative":"보수", "progressive":"진보", "centrist":"중도"}
    buckets = ["conservative","progressive","centrist"]

    brief = []
    brief.append("# 아침 브리핑")
    brief.append(f"- 생성 시각(로컬): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    brief.append(f"- 범위: 최근 {HOURS}시간 (fetched_at >= {since})")
    brief.append(f"- 총 기사 수(클린 후): {len(df)}")
    brief.append(f"- 설정: eps(politics)={EPS_MAP['politics']}, sim_min={SIM_MIN}, low_sim={LOW_SIM}")
    brief.append("")

    # Politics
    pol = df[df["section"] == "politics"].copy()
    brief.append("## 정치 (보수/진보/중도 비교)")
    if len(pol) == 0:
        brief.append("- (데이터 없음)\n")
        dist = {}
    else:
        pol2, Xp = cluster_df(pol, eps=EPS_MAP["politics"])
        labels = pol2["cluster"].to_numpy()
        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = int((labels == -1).sum())
        brief.append(f"- 클러스터: {n_clusters}, 노이즈: {n_noise}")
        brief.append(f"- 3관점 보강: centroid 유사도 매칭(sim_min={SIM_MIN}, low_sim<{LOW_SIM})\n")

        cmp = rep_politics_fill(pol2, Xp, sim_min=SIM_MIN)
        dist = cmp["bucket_filled"].value_counts().sort_index().to_dict()
        brief.append(f"- 관점 채움 분포(bucket_filled=1/2/3): {dist}\n")

        for i, row in enumerate(cmp.head(TOP_N_POL).itertuples(index=False), start=1):
            brief.append(f"### 정치 이슈 {i} (기사 {row.size} / 매체 {row.sources} / 관점 {row.bucket_filled}종)")
            for b in buckets:
                title = getattr(row, f"{b}_title")
                link  = getattr(row, f"{b}_link")
                sim   = float(getattr(row, f"{b}_sim"))
                if isinstance(title, str) and title.strip():
                    flag = " (유사도 낮음)" if sim < LOW_SIM else ""
                    brief.append(f"- [{bucket_kr[b]}]{flag} {title} — {link}")
                else:
                    brief.append(f"- [{bucket_kr[b]}] (해당 관점 기사 없음)")
            brief.append("")

    # Others
    for sec in ["economy", "society", "international"]:
        dsec = df[df["section"] == sec].copy()
        title_kr = {"economy":"경제", "society":"사회", "international":"세계"}[sec]
        brief.append(f"## {title_kr}")
        if len(dsec) == 0:
            brief.append("- (데이터 없음)\n")
            continue

        dsec2, Xs = cluster_df(dsec, eps=EPS_MAP[sec])
        labels = dsec2["cluster"].to_numpy()
        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = int((labels == -1).sum())
        brief.append(f"- 클러스터: {n_clusters}, 노이즈: {n_noise}\n")

        summ = rep_index_by_centroid(dsec2, Xs)
        for j, r in enumerate(summ.head(TOP_N_OTH).itertuples(index=False), start=1):
            brief.append(f"### {title_kr} 이슈 {j} (기사 {r.size} / 매체 {r.sources})")
            brief.append(f"- 대표: {r.representative_title} — {r.representative_link}")
            brief.append(f"- 참고(상위 3개 제목): {r.top3_titles}")
            brief.append("")
        brief.append("")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(brief), encoding="utf-8")

    return {"since": since, "rows": int(len(df)), "politics_bucket_filled_dist": dist, "out": str(OUT_PATH)}


def main():
    # 1) ingest feeds
    ingest = ingest_all_feeds(limit=50)
    print("[INGEST]", ingest)

    # 2) generate briefing
    info = generate_briefing()
    print("[BRIEFING]", info)


if __name__ == "__main__":
    main()
