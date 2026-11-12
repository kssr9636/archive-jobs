"""기록물관리 채용공고 수집기.

공공데이터포털의 '공공기관 채용정보 조회서비스'(잡알리오 데이터)에서
진행 중인 공고를 모두 받아, 기록물관리 관련 공고만 골라
docs/data/jobs.json 파일로 저장합니다.

실행 전에 환경변수 DATA_GO_KR_KEY 에 공공데이터포털 인증키를 넣어야 합니다.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

API_URL = "https://apis.data.go.kr/1051000/recruitment/list"
OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "jobs.json"
KST = timezone(timedelta(hours=9))

# 이 단어 중 하나라도 들어 있으면 기록물관리 공고로 봅니다.
KEYWORDS = [
    "기록물",
    "기록연구",
    "기록관리",
    "기록관",
    "아키비스트",
    "archivist",
]

# 제목 외에 단어를 찾아볼 항목(자격요건, 우대조건 등)
TEXT_FIELDS = [
    "recrutPbancTtl",  # 공고 제목
    "aplyQlfcCn",  # 응시 자격
    "prefCondCn",  # 우대 조건
    "prefCn",  # 우대 내용
    "ncsCdNmLst",  # 직무 분류
]


def get_key() -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY 환경변수가 비어 있습니다. 공공데이터포털 인증키를 넣어 주세요.")
    # 'Encoding' 키를 넣어도 동작하도록 한 번 풀어 줍니다.
    decoded = unquote(key)
    # 키 값은 보여주지 않고, 문제를 찾는 데 필요한 형태 정보만 보여줍니다.
    yes = lambda cond: "예" if cond else "아니오"
    has_space = any(c.isspace() for c in decoded)
    has_quote = any(c in "\"'" for c in decoded)
    print(
        f"  인증키 점검: 길이 {len(decoded)}자, 공백 포함 {yes(has_space)}, "
        f"따옴표 포함 {yes(has_quote)}, Encoding 키 여부 {yes('%' in key)}"
    )
    return "".join(decoded.split()).strip("\"'")


def fetch_all(key: str) -> list[dict]:
    """진행 중인 공고를 페이지별로 모두 받아옵니다."""
    items: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            API_URL,
            params={
                "serviceKey": key,
                "resultType": "json",
                "numOfRows": 100,
                "pageNo": page,
                "ongoingYn": "Y",
            },
            timeout=60,
        )
        try:
            data = resp.json()
        except ValueError:
            sys.exit(f"API 오류(HTTP {resp.status_code}): {resp.text[:300]}")

        if "OpenAPI_ServiceResponse" in data:  # 인증키 오류 등
            header = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            msg = f"API 오류: {header.get('returnAuthMsg')} ({header.get('errMsg')})"
            if header.get("returnReasonCode") == "30":
                msg += (
                    "\n→ 인증키가 아직 등록되지 않았습니다. 승인 직후라면 1~2시간 뒤 다시 실행해 보세요."
                    "\n→ 그래도 같으면 GitHub Secret 에 넣은 키가 정확한지(앞뒤 공백 없이) 확인해 주세요."
                )
            sys.exit(msg)
        if resp.status_code != 200:
            sys.exit(f"API 오류(HTTP {resp.status_code}): {resp.text[:300]}")
        if str(data.get("resultCode", "200")) not in ("200", "00"):
            sys.exit(f"API 오류: {data.get('resultCode')} {data.get('resultMsg')}")

        batch = data.get("result") or []
        items.extend(batch)
        total = int(data.get("totalCount") or 0)
        print(f"  {page}쪽: {len(batch)}건 (전체 {total}건)")
        if not batch or len(items) >= total:
            break
        page += 1
    return items


def is_archive_job(item: dict) -> bool:
    text = " ".join(str(item.get(f) or "") for f in TEXT_FIELDS).lower()
    return any(k.lower() in text for k in KEYWORDS)


def to_date(value) -> str | None:
    """'20261130' 같은 값을 '2026-11-30' 으로 바꿉니다."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize(item: dict) -> dict:
    sn = str(item.get("recrutPblntSn") or "")
    link = item.get("srcUrl") or (
        f"https://job.alio.go.kr/recruitview.do?idx={sn}" if sn else ""
    )
    return {
        "id": f"alio-{sn}" if sn else f"alio-{item.get('recrutPbancTtl')}",
        "source": "잡알리오",
        "org": item.get("instNm") or "",
        "title": (item.get("recrutPbancTtl") or "").strip(),
        "hire_type": item.get("hireTypeNmLst") or "",
        "recruit_type": item.get("recrutSeNm") or "",
        "region": item.get("workRgnNmLst") or "",
        "headcount": item.get("recrutNope") or "",
        "start": to_date(item.get("pbancBgngYmd")),
        "end": to_date(item.get("pbancEndYmd")),
        "url": link,
    }


def main() -> None:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    # 이전 결과를 읽어서 '처음 발견한 날'을 이어 받습니다(NEW 표시용).
    old: dict[str, dict] = {}
    if OUT_FILE.exists():
        try:
            old = {j["id"]: j for j in json.loads(OUT_FILE.read_text("utf-8")).get("jobs", [])}
        except (ValueError, KeyError):
            old = {}

    print("공공기관 채용정보를 받아오는 중...")
    raw = fetch_all(get_key())
    matched = [normalize(i) for i in raw if is_archive_job(i)]

    jobs = {}
    for job in matched:
        if job["end"] and job["end"] < today:  # 마감 지난 공고 제외
            continue
        job["first_seen"] = old.get(job["id"], {}).get("first_seen", today)
        jobs[job["id"]] = job  # 같은 공고 중복 제거

    result = sorted(jobs.values(), key=lambda j: (j["end"] or "9999-12-31", j["org"]))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {"updated_at": now.strftime("%Y-%m-%d %H:%M"), "jobs": result},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"전체 {len(raw)}건 중 기록물관리 관련 {len(result)}건 저장 완료 → {OUT_FILE}")


if __name__ == "__main__":
    main()
