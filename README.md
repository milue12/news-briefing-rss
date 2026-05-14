# news-briefing-rss

## 프로젝트 소개
이 저장소는 **RSS 기반 뉴스 수집 → 이슈(토픽) 클러스터링 → 아침 브리핑 생성**을 자동화한 프로젝트입니다.  
정치 분야는 **보수/진보/중도**(3관점)로 대표 기사를 매칭하여 같은 이슈를 서로 다른 관점에서 빠르게 비교할 수 있도록 구성했습니다.

- 대상 분야: **정치 / 경제 / 사회 / 세계**
- 결과물: `data/processed/morning_briefing.md` (Markdown 브리핑 파일)
- 자동화: WSL/Linux 환경에서 **cron**으로 매일 아침 실행 가능

---

## 목표
- 하루 시작에 주요 언론사 RSS를 기반으로 **당일 정세를 빠르게 파악**
- 동일 이슈를 언론사별로 묶고(클러스터링), 정치 분야는 **3관점 비교**로 편향을 줄인 요약 소비 지원

---

## 주요 기능
### 1) RSS 수집 및 중복 제거
- 언론사/섹션별 RSS를 수집하여 기사 아이템을 DB에 누적 저장
- `guid` 또는 `link` 기반으로 중복을 제거하기 위해 `item_id(sha256)`를 생성하여 관리

### 2) 이슈 클러스터링 (Topic Clustering)
- 기사 제목+요약을 하나의 텍스트로 결합
- 한국어 형태소 분석 없이도 안정적으로 동작하도록 **char n-gram TF-IDF**를 사용
- **DBSCAN(cosine)**으로 “같은 사건/이슈”를 자동으로 묶음

### 3) 정치 3관점 비교(보수/진보/중도)
- `feeds.csv`에 정의된 `politics_bucket`을 이용해 정치 기사에 관점 라벨 부여
- 클러스터 내부에 특정 관점 기사가 없으면 전체 정치 기사 풀에서 **centroid 유사도 기반으로 보강 매칭**
- 유사도가 낮은 경우 `(유사도 낮음)`으로 표시하여 오매칭 가능성을 사용자에게 노출

### 4) 아침 브리핑 파일 생성
- 정치: 이슈별 **보수/진보/중도** 대표 기사(제목+링크) 출력
- 경제/사회/세계: 이슈별 대표 기사 + 참고(상위 3개 제목)

---

## 프로젝트 구조
권장 구조(웹 업로드 기준):

```

news-briefing-rss/
notebooks/
news_briefing_pipeline.ipynb
src/
run_daily.py
configs/
briefing_config.json
feeds.sample.csv
README.md
requirements.txt

````

런타임(실행 후 생성, GitHub에는 업로드 X):
- `db/news.db`
- `data/processed/morning_briefing.md`
- `logs/cron.log`

---

## 주요 파일 설명
- `notebooks/news_briefing_pipeline.ipynb` : 실험/개발용 노트북(수집→클러스터링→브리핑 생성 흐름 확인)
- `src/run_daily.py` : 운영용 원샷 스크립트(**RSS 수집 → SQLite 적재 → 브리핑 생성**)
- `configs/briefing_config.json` : 브리핑 파라미터(시간 범위, eps, sim_min 등)
- `configs/feeds.sample.csv` : RSS 목록 샘플(사용 시 `feeds.csv`로 복사하여 편집)

---

## 설치 및 실행 (pip)
### 1) 패키지 설치
```bash
pip install -r requirements.txt
````

### 2) RSS 목록 준비

샘플을 복사해서 실제 파일로 만들고, 필요하면 언론사/섹션/관점 라벨을 수정합니다.

```bash
cp configs/feeds.sample.csv configs/feeds.csv
```

### 3) 실행

```bash
python src/run_daily.py
```

실행 후 생성물:

* `data/processed/morning_briefing.md`

---

## 설정 파일 가이드

### A) `configs/briefing_config.json`

* `hours`: 최근 N시간 기사만 브리핑 대상
* `top_n_politics`: 정치 이슈 상위 N개 출력
* `top_n_other`: 경제/사회/세계 이슈 상위 N개 출력
* `eps`: DBSCAN 클러스터링 강도(작을수록 더 엄격하게 묶임)
* `sim_min`: 정치 3관점 보강 매칭 시 최소 유사도 임계값

  * 높이면 정확도↑ / 3관점 채움↓
  * 낮추면 3관점 채움↑ / “유사도 낮음” 증가 가능
* `low_sim`: 이 값 미만이면 출력에 `(유사도 낮음)` 표시

### B) `configs/feeds.sample.csv`

권장 컬럼:

* `publisher, section, feed_name, feed_url, politics_bucket`

`politics_bucket`은 정치 섹션에서만 의미가 있으며 값은 아래 중 하나를 권장합니다:

* `conservative` / `progressive` / `centrist`

---

## 자동 실행 (WSL/Linux cron)

### 1) python 절대경로 확인

```bash
which python
```

### 2) cron 등록(매일 08:00)

```bash
crontab -e
```

예시:

```cron
0 8 * * * cd /home/<USER>/workspace/news-briefing-rss && /home/<USER>/miniconda3/envs/news-briefing/bin/python /home/<USER>/workspace/news-briefing-rss/src/run_daily.py >> /home/<USER>/workspace/news-briefing-rss/logs/cron.log 2>&1
```
