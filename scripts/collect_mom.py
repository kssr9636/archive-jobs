"""어머님용 일자리 수집기 (경북 안동·예천 + 영주·문경·의성·상주).

분야
  - 사회복지      : 복지관, 요양, 돌봄, 사례관리, 아동보호, 상담 등
  - 신중년·시니어 : 신중년, 중장년, 시니어, 노인일자리 등
  - 관공서 기간제·공무직 : 시·군청, 도청 등의 기간제·공무직·근로자 채용

수집처
  1) 나라일터 (공공취업정보 API)
  2) 클린아이 잡플러스 (경북 지방공공기관)
  3) 예천군청 채용공고·구인정보, 안동시청 고시공고·구인구직 게시판

결과는 docs/data/mom_jobs.json 에 저장됩니다.
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import city_boards
from collect import KST, NARA_URL, NOT_POSTING, clean, get_key, http_get, to_date

OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "mom_jobs.json"
NARA_DAYS = 45
KEEP_DAYS = 30  # 마감일을 모르는 게시판 공고는 게시 후 며칠 동안 보여줄지

# 지역: 나라일터 지역코드 → 이름
AREA_CODES = {"47170": "안동", "47900": "예천", "47210": "영주", "47280": "문경", "47730": "의성", "47250": "상주"}
AREA_NAMES = list(AREA_CODES.values())
# '동대문'의 '문경'처럼 다른 낱말 안에 든 것은 빼고, '안동시', '예천군' 같은 온전한 이름만 봅니다.
AREA_RE = re.compile(r"(안동|영주|문경|상주)시|(예천|의성)군")

CATS = {
    "사회복지": re.compile(r"사회복지|복지사|복지관|복지센터|사례관리|아동보호|지역아동센터|상담"
                       r"|정신건강|복지플래너|맞춤형복지|노인일자리|전담인력"),
    "신중년·시니어": re.compile(r"신중년|중장년|시니어|노인일자리|어르신|은퇴|경력형|50\+|5060"),
    "관공서 기간제·공무직": re.compile(r"기간제|공무직|근로자|공공근로|일자리사업|대체인력"),
}
# 다른 자격증이 필요한 직무(요양보호사·간호·조리 등)는 사회복지사 공고가 아니면 뺍니다.
OTHER_LICENSE = re.compile(r"요양보호사|간호|조리|방사선|치과|의사|약사|노무사|변호사|수영|운전|미화|청소|신호수")
GOV_BOARD_HIRE = re.compile(r"채용|모집|구인|근로자|직원")  # 고시공고 게시판에서 채용 글만


def cats_of(title: str, gov: bool) -> list[str]:
    if OTHER_LICENSE.search(title) and "사회복지사" not in title:
        return []
    out = [name for name, rx in CATS.items() if rx.search(title)]
    if not gov:  # 민간 구인 글은 사회복지 분야만
        out = [c for c in out if c == "사회복지"]
    return out


# ------------------------------------------------------------------ 나라일터
def in_region_nara(org: str, area: str) -> str | None:
    if area in AREA_CODES:
        return AREA_CODES[area]
    m = AREA_RE.search(org)
    if m:
        return m.group(1) or m.group(2)
    tokens = org.split()
    # 경상북도청 본청(도청신도시) 공고: '경상북도' 또는 '경상북도 ○○과' 형태만 (다른 시·군 제외)
    if tokens and tokens[0] == "경상북도" and (len(tokens) == 1 or not re.search(r"(시|군)$", tokens[1])):
        return "경북도청"
    return None


def fetch_nara(key: str) -> list[dict]:
    today = datetime.now(KST)
    params = {
        "serviceKey": key, "numOfRows": 1000, "Pblanc_ty": "", "Instt_se": "",
        "Begin_de": (today - timedelta(days=NARA_DAYS)).strftime("%Y-%m-%d"),
        "End_de": today.strftime("%Y-%m-%d"),
    }
    jobs, page, seen = [], 1, 0
    while True:
        root = ET.fromstring(http_get(NARA_URL, {**params, "pageNo": page}).content)
        items = root.findall(".//item")
        seen += len(items)
        for it in items:
            g = (lambda e: lambda tag: clean(e.findtext(tag)))(it)
            region = in_region_nara(g("insttname"), g("areacode"))
            if not region:
                continue
            cats = cats_of(g("title"), gov=True)
            if not cats:
                continue
            jobs.append({
                "category": cats, "id": f"nara-{g('idx')}", "source": "나라일터",
                "org": g("insttname"), "title": g("title"), "region": region,
                "start": to_date(g("begindate")), "end": to_date(g("enddate")),
                "url": f"https://www.gojobs.go.kr/apmView.do?empmnsn={g('idx')}"
                       "&searchJobsecode=020&menuNo=401&selMenuNo=400",
            })
        if not items or seen >= int(root.findtext(".//totalCount") or 0):
            break
        page += 1
    return jobs


# ------------------------------------------------------------------ 클린아이(경북)
def fetch_cleaneye(key: str) -> list[dict]:
    resp = http_get("https://apis.data.go.kr/B551982/openApiEmployInfo/openXmlEmployInfo",
                    {"serviceKey": key, "type": "xml", "pageNo": 1, "numOfRows": 1000, "sidoCd": "007014"})
    jobs = []
    for it in ET.fromstring(resp.content).findall(".//item"):
        g = lambda tag: clean(it.findtext(tag))
        m = AREA_RE.search(g("ADDRESS") + " " + g("ENT_NAME"))
        if not m:
            continue
        region = m.group(1) or m.group(2)
        cats = cats_of(" ".join([g("ENT_TITLE"), g("ENT_RECRUIT"), g("JOB_TYPE")]), gov=True)
        if not cats:
            continue
        jobs.append({
            "category": cats, "id": f"cleaneye-{g('NO')}", "source": "클린아이 잡플러스",
            "org": g("ENT_NAME"), "title": g("ENT_TITLE"), "region": region,
            "start": to_date(g("PUB_DATE")), "end": to_date(g("PUB_END_DATE")), "url": g("URL"),
        })
    return jobs


# ------------------------------------------------------------------ 시·군청 게시판
BOARDS = [
    {"name": "예천군청 채용공고", "region": "예천", "gov": True,
     "list": "https://www.ycg.kr/open.content/ko/administrative/news/recruitment/?p={page}"},
    {"name": "예천군청 구인정보", "region": "예천", "gov": False,
     "list": "https://www.ycg.kr/open.content/ko/participate/recruit/?p={page}"},
    {"name": "안동시청 고시공고", "region": "안동", "gov": True, "hire_only": True,
     "list": "https://www.andong.go.kr/portal/saeol/gosi/list.do?mId=0401020100&page={page}"},
    {"name": "안동시청 구인구직", "region": "안동", "gov": False,
     "list": "https://www.andong.go.kr/portal/bbs/list.do?ptIdx=69&mId=0602020000&page={page}",
     "id_re": r'data-action="/portal/bbs/view\.do\?bIdx=(\d+)',
     "view": "https://www.andong.go.kr/portal/bbs/view.do?bIdx={id}&ptIdx=69&mId=0602020000"},
]


def fetch_boards(key: str) -> list[dict]:
    jobs = []
    for b in BOARDS:
        try:
            rows = city_boards.fetch_board(b)
        except Exception as e:  # noqa: BLE001
            print(f"  {b['name']}: 건너뜀({str(e)[:60]})")
            continue
        n = 0
        for r in rows:
            if b.get("hire_only") and not GOV_BOARD_HIRE.search(r["title"]):
                continue
            cats = cats_of(r["title"], gov=b["gov"])
            if not cats:
                continue
            n += 1
            jobs.append({
                "category": cats, "id": "board-" + r["url"].split("://", 1)[-1], "source": b["name"],
                "org": b["name"].split()[0], "title": r["title"], "region": b["region"],
                "start": r["posted"], "end": r["end"], "url": r["url"], "sticky": True,
            })
        print(f"  {b['name']}: {len(rows)}건 중 {n}건")
    return jobs


# ------------------------------------------------------------------ 실행
def main() -> None:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    keep_from = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    key = get_key()

    old = {}
    if OUT_FILE.exists():
        try:
            old = {j["id"]: j for j in json.loads(OUT_FILE.read_text("utf-8")).get("jobs", [])}
        except (ValueError, KeyError):
            old = {}

    found, sources = [], []
    for name, fetch in [("나라일터", fetch_nara), ("클린아이 잡플러스", fetch_cleaneye), ("시·군청 게시판", fetch_boards)]:
        print(f"[{name}] 받아오는 중...")
        try:
            got = fetch(key)
        except Exception as e:  # noqa: BLE001
            print(f"  건너뜀(오류): {str(e)[:100]}")
            continue
        print(f"  → {len(got)}건")
        found.extend(got)
        sources.append(name)

    ids = {j["id"] for j in found}
    found += [j for j in old.values() if j.get("sticky") and j["id"] not in ids]

    jobs, titles = {}, set()
    for j in found:
        if not j["end"]:  # 제목에 '(11.12.마감)', '~11/20' 같은 마감일이 있으면 씁니다
            m = re.search(r"(?:~\s*)?(\d{1,2})[./](\d{1,2})\.?\s*(?:\([^)]*\))?\s*(?:마감|까지)", j["title"])
            if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
                year = now.year + (1 if int(m.group(1)) < now.month - 6 else 0)
                j["end"] = f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        if j["end"] and j["end"] < today:
            continue
        if not j["end"] and (j.get("start") or "") < keep_from:
            continue
        if any(w in j["title"] for w in NOT_POSTING):
            continue
        same = "".join(j["title"].split())
        if same in titles:
            continue
        titles.add(same)
        j["first_seen"] = old.get(j["id"], {}).get("first_seen", today)
        jobs[j["id"]] = j

    result = sorted(jobs.values(), key=lambda j: (j["end"] or "9999-12-31", j["org"]))
    OUT_FILE.write_text(json.dumps({"updated_at": now.strftime("%Y-%m-%d %H:%M"), "sources": sources,
                                    "jobs": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"총 {len(result)}건 저장 완료 → {OUT_FILE}")


if __name__ == "__main__":
    main()
