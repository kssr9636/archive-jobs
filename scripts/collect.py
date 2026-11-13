"""기록물관리 채용공고 수집기.

공공데이터포털 API에서 채용공고를 받아, 기록물관리 관련 공고만 골라
docs/data/jobs.json 파일로 저장합니다.

수집처
  1) 한국지역정보개발원_채용정보 조회 서비스 (클린아이 잡플러스, 지방공공기관)
  2) 공공기관 채용정보 조회서비스 (잡알리오, 공기업·준정부기관)
  3) 인사혁신처_공공취업정보 조회 서비스 (나라일터, 국가·지자체·교육청)
  4) 통근 가능한 시·구청 홈페이지 채용·고시공고 게시판 (scripts/city_boards.py)
     → 2)~4)는 활용신청을 하지 않았거나 오류가 나면 건너뜁니다.

실행 전에 환경변수 DATA_GO_KR_KEY 에 공공데이터포털 인증키를 넣어야 합니다.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "jobs.json"
KST = timezone(timedelta(hours=9))
MUSEUM = "박물관·역사관·문학관"

# 분야별로 이 단어 중 하나라도 들어 있으면 해당 분야 공고로 봅니다.
# (띄어쓰기를 지우고 비교하므로 '기록물 관리'도 찾습니다.)
CATEGORIES = {
    "기록물": ["기록물", "기록연구", "기록관리", "기록관", "아키비스트", "archivist"],
    "사서·도서관": ["도서관", "정사서", "준사서", "사서교사", "사서보조", "사서직"],
    MUSEUM: ["역사관", "박물관", "기념관", "사료관", "문서관", "문학관", "아카이브",
             "학예", "소장품", "자료정리"],
}
# 박물관·역사관·문학관 분야에서, 제목에 이런 단어가 있으면 (자료 업무가 아니므로) 뺍니다.
MUSEUM_EXCLUDE = re.compile(r"시설|청소|미화|환경관리|경비|방호|안내|주차|조리|운전|해설")
# '사서'는 '신체검사서', '조사서' 같은 단어 안에도 들어 있으므로,
# 앞에 다른 한글이 붙지 않은 '사서'만 사서 공고로 봅니다.
LIBRARIAN_RE = re.compile(r"(?<![가-힣])사서(?!함)")
# 이 기관의 공고는 단어와 상관없이 모두 보여줍니다(관심 기관).
WATCH_ORGS = ["법원도서관", "국회도서관", "국립중앙도서관", "고양시도서관센터", "고양도서관센터"]


# ---------------------------------------------------------------- 공통 도구
def get_key() -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY 환경변수가 비어 있습니다. 공공데이터포털 인증키를 넣어 주세요.")
    # 'Encoding' 키를 넣어도 동작하도록 한 번 풀고, 공백·따옴표를 지웁니다.
    return "".join(unquote(key).split()).strip("\"'")


def http_get(url: str, params: dict) -> requests.Response:
    """공공데이터포털은 가끔 연결이 끊기므로 몇 번 다시 시도합니다."""
    last_error = None
    for attempt in range(5):
        try:
            return requests.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            last_error = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"연결 실패: {last_error}")


def is_archive_job(*texts) -> list[str]:
    """해당하는 분야 이름 목록을 돌려줍니다. 하나도 없으면 빈 목록(=관련 없음)."""
    text = "".join("".join(str(t or "").split()) for t in texts).lower()
    cats = [cat for cat, words in CATEGORIES.items() if any(w.lower() in text for w in words)]
    if "사서·도서관" not in cats and any(LIBRARIAN_RE.search(str(t or "")) for t in texts):
        cats.append("사서·도서관")
    return cats


def watched(*texts) -> list[str]:
    """기관명(또는 제목)에 관심 기관 이름이 있으면 ['관심기관']."""
    text = "".join("".join(str(t or "").split()) for t in texts)
    return ["관심기관"] if any(w in text for w in WATCH_ORGS) else []


def to_date(value) -> str | None:
    """'20261130', '2026-11-30' 같은 값을 '2026-11-30' 으로 바꿉니다."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def clean(value) -> str:
    value = (value or "").strip()
    return "" if value in ("-", "null", "None") else value


# ------------------------------------------------- 1) 클린아이 잡플러스(지방공공기관)
CLEANEYE_URL = "https://apis.data.go.kr/B551982/openApiEmployInfo/openXmlEmployInfo"
# 시·도 코드 (007005 광주는 전남광주통합특별시 007013 으로 합쳐졌습니다)
SIDO_CODES = [f"0070{n:02d}" for n in range(1, 18)]


def fetch_cleaneye(key: str) -> list[dict]:
    jobs = []
    for code in SIDO_CODES:
        resp = http_get(
            CLEANEYE_URL,
            {"serviceKey": key, "type": "xml", "pageNo": 1, "numOfRows": 1000, "sidoCd": code},
        )
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        result_code = root.findtext(".//resultCode")
        if result_code == "3":  # 해당 지역에 공고 없음
            continue
        if result_code != "0":
            msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or resp.text[:200]
            raise RuntimeError(f"API 오류({result_code}): {msg}")

        items = root.findall(".//item")
        print(f"  시도코드 {code}: {len(items)}건")
        for it in items:
            g = lambda tag: clean(it.findtext(tag))
            cats = watched(g("ENT_NAME")) + is_archive_job(
                g("ENT_TITLE"), g("ENT_RECRUIT"), g("POSITION"), g("DUTY_DETAIL"),
                g("ENT_LICENSE1"), g("ENT_LICENSE2"), g("ENT_LICENSE3"), g("ENT_LICENSE4"),
                g("EXHIBIT"), g("JOB_SEEK_ETC"),
            )
            if not cats:
                continue
            jobs.append({
                "category": cats,
                "id": f"cleaneye-{g('NO')}",
                "source": "클린아이 잡플러스",
                "org": g("ENT_NAME"),
                "title": g("ENT_TITLE"),
                "hire_type": g("JOB_TYPE"),
                "status": g("STATUS"),
                "region": g("SIDO_CD"),
                "headcount": g("EMPLOY_NUM"),
                "start": to_date(g("PUB_DATE")),
                "end": to_date(g("PUB_END_DATE")),
                "url": g("URL"),
            })
    return jobs


# ------------------------------------------------- 2) 잡알리오(공기업·준정부기관)
ALIO_URL = "https://apis.data.go.kr/1051000/recruitment/list"


def fetch_alio(key: str) -> list[dict]:
    jobs, page, seen = [], 1, 0
    while True:
        resp = http_get(
            ALIO_URL,
            {"serviceKey": key, "resultType": "json", "numOfRows": 100, "pageNo": page, "ongoingYn": "Y"},
        )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if "OpenAPI_ServiceResponse" in data:
            h = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            if h.get("returnReasonCode") == "30":
                raise PermissionError("이 서비스는 아직 활용신청이 안 되어 있습니다")
            raise RuntimeError(f"API 오류: {h.get('returnAuthMsg')} ({h.get('errMsg')})")

        batch = data.get("result") or []
        seen += len(batch)
        for it in batch:
            cats = watched(it.get("instNm")) + is_archive_job(*(it.get(f) for f in (
                "recrutPbancTtl", "aplyQlfcCn", "prefCondCn", "prefCn", "ncsCdNmLst")))
            if not cats:
                continue
            sn = str(it.get("recrutPblntSn") or "")
            jobs.append({
                "category": cats,
                "id": f"alio-{sn}",
                "source": "잡알리오",
                "org": it.get("instNm") or "",
                "title": (it.get("recrutPbancTtl") or "").strip(),
                "hire_type": it.get("hireTypeNmLst") or "",
                "status": "모집중",
                "region": it.get("workRgnNmLst") or "",
                "headcount": it.get("recrutNope") or "",
                "start": to_date(it.get("pbancBgngYmd")),
                "end": to_date(it.get("pbancEndYmd")),
                "url": it.get("srcUrl") or (f"https://job.alio.go.kr/recruitview.do?idx={sn}" if sn else ""),
            })
        if not batch or seen >= int(data.get("totalCount") or 0):
            break
        page += 1
    return jobs


# ------------------------------------------------- 3) 나라일터(국가·지자체·교육청 등)
NARA_URL = "https://apis.data.go.kr/1760000/PblJobService/getList"
NARA_PAGE = 1000
NARA_DAYS = 45  # 최근 며칠 동안 등록된 공고까지 볼지
NARA_ORG_TYPE = {"g01": "국가기관", "g02": "지방자치단체", "g03": "공공기관", "g04": "교육청"}
AREA = {
    "11": "서울", "26": "부산", "27": "대구", "28": "인천", "29": "광주", "30": "대전",
    "31": "울산", "36": "세종", "41": "경기", "42": "강원", "51": "강원", "43": "충북",
    "44": "충남", "45": "전북", "52": "전북", "46": "전남", "47": "경북", "48": "경남", "50": "제주",
}


NARA_DETAIL_URL = "https://apis.data.go.kr/1760000/PblJobService"
# 한 번 상세 확인한 공고 결과를 기억해 두는 파일 (다음 실행 때 다시 조회하지 않음)
NARA_CACHE = Path(__file__).resolve().parent.parent / "data" / "nara_detail_cache_v4.json"
NARA_WORKERS = 6  # 상세 조회를 동시에 몇 개씩 할지


def nara_detail(key: str, idx: str) -> dict:
    """공고 본문(getItem)과 채용 직급(getItemPosition)을 받아 기록물 관련인지 확인합니다."""
    texts = {}
    for func in ("getItemPosition", "getItem"):
        resp = http_get(f"{NARA_DETAIL_URL}/{func}", {"serviceKey": key, "idx": idx, "numOfRows": 100})
        root = ET.fromstring(resp.content)
        if root.findtext(".//resultCode") not in ("00", "0"):
            raise RuntimeError(root.findtext(".//resultMsg"))
        if func == "getItemPosition":
            texts["positions"] = [clean(n.text) for n in root.iter("name") if clean(n.text)]
        else:
            texts["contents"] = clean(root.findtext(".//contents"))

    hit_pos = [p for p in texts["positions"] if is_archive_job(p)]
    if hit_pos:
        return {"hit": True, "note": "직급: " + ", ".join(hit_pos)}
    if is_archive_job(texts["contents"]):
        line = next((ln.strip(" ○·-\u3000") for ln in texts["contents"].splitlines() if is_archive_job(ln)), "")
        return {"hit": True, "note": ("본문: " + line)[:80]}
    return {"hit": False}


def fetch_nara(key: str) -> list[dict]:
    today = datetime.now(KST)
    today_s = today.strftime("%Y%m%d")
    params = {
        "serviceKey": key,
        "numOfRows": NARA_PAGE,
        "Pblanc_ty": "",
        "Instt_se": "",
        "Begin_de": (today - timedelta(days=NARA_DAYS)).strftime("%Y-%m-%d"),  # 등록일 범위
        "End_de": today.strftime("%Y-%m-%d"),
    }

    def to_job(g, note=""):
        area = g("areacode")
        return {
            "category": watched(g("insttname"), g("title")) + is_archive_job(g("title"), note),
            "id": f"nara-{g('idx')}",
            "source": "나라일터",
            "org": g("insttname"),
            "title": g("title"),
            "hire_type": NARA_ORG_TYPE.get(g("type02"), ""),
            "status": "",
            "region": AREA.get(area[:2], "") if area and area != "00000" else "전국",
            "headcount": "",
            "start": to_date(g("begindate")),
            "end": to_date(g("enddate")),
            "note": note,
            "url": f"https://www.gojobs.go.kr/apmView.do?empmnsn={g('idx')}"
                   "&searchJobsecode=020&menuNo=401&selMenuNo=400",
        }

    jobs, unclear, page, seen = [], [], 1, 0
    while True:
        resp = http_get(NARA_URL, {**params, "pageNo": page})
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            if "SERVICE_KEY_IS_NOT_REGISTERED" in resp.text:
                raise PermissionError("이 서비스는 아직 활용신청이 안 되어 있습니다")
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if root.findtext(".//resultCode") not in ("00", "0"):
            raise RuntimeError(f"API 오류: {root.findtext('.//resultMsg')}")

        items = root.findall(".//item")
        seen += len(items)
        total = int(root.findtext(".//totalCount") or 0)
        print(f"  {page}쪽: {len(items)}건 (전체 {total}건)")
        for it in items:
            g = (lambda e: lambda tag: clean(e.findtext(tag)))(it)
            if is_archive_job(g("title")) or watched(g("insttname"), g("title")):
                jobs.append(to_job(g))
            elif (g("enddate") or "0") >= today_s and not any(w in g("title") for w in NOT_POSTING):
                unclear.append(g)  # 제목만으로는 알 수 없는, 아직 접수 중인 공고
        if not items or seen >= total:
            break
        page += 1

    # 제목에 단어가 없는 공고는 상세 직급·본문까지 확인합니다.
    try:
        cache = json.loads(NARA_CACHE.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    todo = [g for g in unclear if g("idx") not in cache]
    print(f"  상세 확인: 접수 중 {len(unclear)}건 중 새로 확인할 공고 {len(todo)}건")

    from concurrent.futures import ThreadPoolExecutor

    def work(g):
        try:
            return g("idx"), nara_detail(key, g("idx"))
        except Exception as e:  # noqa: BLE001  다음 실행 때 다시 시도
            return g("idx"), {"error": str(e)[:100]}

    errors = 0
    with ThreadPoolExecutor(NARA_WORKERS) as pool:
        for n, (idx, res) in enumerate(pool.map(work, todo), 1):
            if "error" in res:
                errors += 1
                continue
            cache[idx] = {**res, "end": next(g("enddate") for g in todo if g("idx") == idx)}
            if n % 100 == 0:
                print(f"    {n}/{len(todo)}건 확인")
    if errors:
        print(f"  상세 확인 실패 {errors}건 (다음 실행 때 다시 확인)")

    for g in unclear:
        res = cache.get(g("idx"))
        if res and res.get("hit"):
            note = res.get("note", "")
            # 본문에서 찾은 경우, '도서관'이 시설 이름으로만 나온 것은 빼고
            # '사서'·'기록물' 같은 직무 단어가 있을 때만 보여줍니다.
            if note.startswith("본문:") and not (
                    is_archive_job(note).count("기록물") or LIBRARIAN_RE.search(note)
                    or re.search(r"학예|소장품|자료정리|아카이브|사료", note)):
                continue
            jobs.append(to_job(g, note))

    # 마감 지난 공고는 기억 파일에서 지웁니다.
    cache = {k: v for k, v in cache.items() if (v.get("end") or "0") >= today_s}
    NARA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    NARA_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    return jobs


# ------------------------------------------------- 4) 통근 가능한 시·구청 홈페이지
CITY_KEEP_DAYS = 30  # 마감일을 모르는 공고는 게시일로부터 며칠 동안 보여줄지


def fetch_city(key: str) -> list[dict]:
    import city_boards

    jobs, ok = [], 0
    for board in city_boards.BOARDS:
        try:
            rows = city_boards.fetch_board(board)
        except Exception as e:  # noqa: BLE001  한 곳이 안 돼도 나머지는 계속
            print(f"  {board['name']}: 건너뜀({str(e)[:80]})")
            continue
        ok += 1
        hits = []
        for r in rows:
            if board.get("watch"):  # 관심 기관 게시판: 채용 관련 글이면 모두
                cats = ["관심기관"] if any(w in r["title"] for w in board["watch"]) else []
            else:
                cats = watched(r["title"]) + is_archive_job(r["title"])
            if cats:
                hits.append((r, cats))
        print(f"  {board['name']}: {len(rows)}건 중 {len(hits)}건")
        for r, cats in hits:
            jobs.append({
                "category": cats,
                "id": "city-" + r["url"].split("://", 1)[-1],
                "source": board["name"],
                "org": board["name"],
                "title": r["title"],
                "hire_type": "",
                "status": "",
                "region": board["region"],
                "headcount": "",
                "start": r["posted"],
                "end": r["end"],
                "url": r["url"],
                "sticky": True,  # 게시판에서 밀려나도 마감 때까지 기억
            })
    if not ok:
        raise RuntimeError("시·구청 홈페이지에 하나도 접속하지 못했습니다")
    return jobs


# ------------------------------------------------- 5) 아트모아(예술분야 채용, 민간 문화재단 등)
ARTMORE_URL = "https://www.artmore.kr/sub/recruit/search_list.do?page={page}"
ARTMORE_PAGES = 15  # 한 쪽에 10건, 하루 약 10건 올라옴 → 약 2주 치
FOUNDATION = "문화재단"
# 재단 공고 중 자료·기획 업무가 아닌 것은 뺍니다.
FOUNDATION_EXCLUDE = re.compile(r"안내원|어텐던트|매표|청소|미화|경비|주차|조리|운전|시설관리|임원|이사\b|비상임")


def fetch_artmore(key: str) -> list[dict]:
    import city_boards
    import html as htmllib

    jobs, seen = [], set()
    for page in range(1, ARTMORE_PAGES + 1):
        page_html = city_boards._get(ARTMORE_URL.format(page=page))
        rows = re.findall(r"<tr>.*?</tr>", page_html, re.S)
        found = 0
        for tr in rows:
            m_id = re.search(r"search_view\.do\?rec_idx=(\d+)", tr)
            m_co = re.search(r'jobs_cname">\s*([^<]+)', tr)
            m_ti = re.search(r'class="jobs_title">(.*?)</a>', tr, re.S)
            if not (m_id and m_co and m_ti) or m_id.group(1) in seen:
                continue
            seen.add(m_id.group(1))
            found += 1
            raw_title = city_boards._text(m_ti.group(1))
            if raw_title.startswith("마감"):
                continue
            title = re.sub(r"^(진행중|상시)\s*", "", raw_title)
            org = htmllib.unescape(m_co.group(1)).strip()
            place = re.search(r'jobs_wkplace">([^<]*)', tr)
            reg = re.search(r"(20\d\d-\d\d-\d\d) 등록", tr)
            end = re.search(r"(20\d\d-\d\d-\d\d) 마감", tr)
            emp = re.search(r'jobs_regular">([^<]*)', tr)

            cats = watched(org, title) + is_archive_job(title)
            if "재단" in org and not FOUNDATION_EXCLUDE.search(title):
                cats.append(FOUNDATION)
            if not cats:
                continue
            jobs.append({
                "category": cats,
                "id": f"artmore-{m_id.group(1)}",
                "source": "아트모아",
                "org": org,
                "title": title,
                "hire_type": (emp.group(1).strip() if emp else ""),
                "status": "",
                "region": " ".join((place.group(1).split() if place else [])[:2]),
                "headcount": "",
                "start": reg.group(1) if reg else None,
                "end": end.group(1) if end else None,
                "url": f"https://www.artmore.kr/sub/recruit/search_view.do?rec_idx={m_id.group(1)}",
                "sticky": True,
            })
        if not found:
            break
        time.sleep(1)
    return jobs


SOURCES = [
    ("클린아이 잡플러스", fetch_cleaneye, True),  # True = 꼭 성공해야 함
    ("잡알리오", fetch_alio, False),  # False = 실패해도 건너뜀
    ("나라일터", fetch_nara, False),
    ("시·구청 홈페이지", fetch_city, False),
    ("아트모아", fetch_artmore, False),
]

# 채용공고가 아닌 안내글(합격자 발표 등)은 뺍니다.
NOT_POSTING = ["합격자", "결과발표", "결과 발표", "채용후보자", "면접시험", "서류전형 결과"]


# ---------------------------------------------------------------- 실행
def main() -> None:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    key = get_key()

    # 이전 결과를 읽어서 '처음 발견한 날'을 이어 받습니다(NEW 표시용).
    old: dict[str, dict] = {}
    if OUT_FILE.exists():
        try:
            old = {j["id"]: j for j in json.loads(OUT_FILE.read_text("utf-8")).get("jobs", [])}
        except (ValueError, KeyError):
            old = {}

    found, sources = [], []
    for name, fetch, required in SOURCES:
        print(f"[{name}] 받아오는 중...")
        try:
            jobs = fetch(key)
        except PermissionError as e:
            print(f"  건너뜀: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            if required:
                sys.exit(f"  [{name}] 실패: {e}")
            print(f"  건너뜀(오류): {e}")
            continue
        print(f"  → 기록물관리 관련 {len(jobs)}건")
        found.extend(jobs)
        sources.append(name)

    # 시·구청 게시판에서 밀려난 공고도 마감 전까지는 계속 보여줍니다.
    keep_from = (now - timedelta(days=CITY_KEEP_DAYS)).strftime("%Y-%m-%d")
    found_ids = {j["id"] for j in found}
    for j in old.values():
        if j.get("sticky") and j["id"] not in found_ids and (j.get("end") or (j.get("start") or "") >= keep_from):
            found.append(j)

    jobs, titles = {}, set()
    for job in found:
        if not job["end"] and job.get("sticky") and (job.get("start") or "") < keep_from:
            continue  # 마감일을 모르는 시·구청 공고는 게시 후 30일까지만
        if job["end"] and job["end"] < today:  # 마감 지난 공고 제외
            continue
        if any(w in job["title"] for w in NOT_POSTING):  # 합격자 발표 등 제외
            continue
        job.setdefault("category", is_archive_job(job["title"], job.get("note")) or ["기록물"])
        if MUSEUM in job["category"] and MUSEUM_EXCLUDE.search(job["title"]):
            job["category"] = [c for c in job["category"] if c != MUSEUM]
            if not job["category"]:
                continue  # 박물관 시설관리·청소·안내 등은 제외
        same = "".join(job["title"].split())  # 여러 사이트에 같은 제목으로 올라온 공고는 하나만
        if same in titles:
            continue
        titles.add(same)
        job["first_seen"] = old.get(job["id"], {}).get("first_seen", today)
        job.setdefault("category", is_archive_job(job["title"], job.get("note")) or ["기록물"])
        jobs[job["id"]] = job

    result = sorted(jobs.values(), key=lambda j: (j["end"] or "9999-12-31", j["org"]))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {"updated_at": now.strftime("%Y-%m-%d %H:%M"), "sources": sources, "jobs": result},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"총 {len(result)}건 저장 완료 → {OUT_FILE}")


if __name__ == "__main__":
    main()
