"""통근 가능한 시·구청 홈페이지의 채용·고시공고 게시판을 읽습니다.

사이트마다 게시판 모양이 달라서, 각 사이트의
  - 목록 주소 (page 자리에 쪽 번호)
  - 제목 링크에서 상세 주소를 만드는 규칙
만 따로 적어 두고, 읽는 방법은 하나로 같이 씁니다.
"""

import html
import re
import time
from urllib.parse import urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (archive-jobs; personal job search)"}
PAGES = 3  # 게시판마다 몇 쪽까지 볼지

# name, region, 목록 주소, (onclick/javascript 안의 번호 → 상세 주소) 규칙
BOARDS = [
    {
        "name": "고양시청", "region": "경기 고양",
        "list": "https://www.goyang.go.kr/www/user/bbs/BD_selectBbsList.do?q_bbsCode=1082&q_currPage={page}",
        "id_re": r"fnView\('\d+','(\d+)'",
        "view": "https://www.goyang.go.kr/www/user/bbs/BD_selectBbs.do?q_bbsCode=1082&q_bbscttSn={id}",
    },
    {
        "name": "파주시청", "region": "경기 파주",
        "list": "https://www.paju.go.kr/user/board/BD_board.list.do?bbsCd=1022&q_ctgCd=4064&q_currPage={page}",
        "id_re": r"jsView\('\d+',\s*'(\d+)'",
        "view": "https://www.paju.go.kr/user/board/BD_board.view.do?seq={id}&bbsCd=1022&q_ctgCd=4064",
    },
    {
        "name": "김포시청", "region": "경기 김포",
        "list": "https://www.gimpo.go.kr/portal/selectBbsNttList.do?bbsNo=288&key=1007&pageIndex={page}",
    },
    {
        "name": "종로구청", "region": "서울 종로구",
        "list": "https://www.jongno.go.kr/portal/bbs/selectBoardList.do?bbsId=BBSMSTR_000000000026"
                "&menuId=400510&menuNo=400510&pageIndex={page}",
        "id_re": r"viewMove\('(\d+)'\)",
        "view": "https://www.jongno.go.kr/portal/bbs/selectBoardArticle.do?bbsId=BBSMSTR_000000000026"
                "&menuId=400510&menuNo=400510&nttId={id}",
    },
    {
        "name": "강서구청", "region": "서울 강서구",
        "list": "https://www.gangseo.seoul.kr/gs040302?curPage={page}",
    },
    {
        "name": "마포구청", "region": "서울 마포구",  # 고시공고(채용 공고 포함)
        "list": "https://www.mapo.go.kr/site/main/nPortal/list?cp={page}",
    },
    {
        "name": "서대문구청", "region": "서울 서대문구",  # 고시공고(채용 공고 포함)
        "list": "https://www.sdm.go.kr/news/notice/notice.do?cp={page}",
        "id_re": r"goView\('(\d+)'\)",
        "view": "https://www.sdm.go.kr/news/notice/notice.do?mode=view&sdmBoardSeq={id}",
    },
    {
        "name": "법원도서관", "region": "경기 고양",  # 공지사항 (관심 기관)
        "list": "https://library.scourt.go.kr/openspace/parti/notice/list?pageIndex={page}",
        "id_re": r'data-seq-no="(\d+)"',
        "view": "https://library.scourt.go.kr/openspace/parti/notice/detail?seqNo={id}",
        # 기록물·사서 단어가 없어도, 이 단어가 들어간 글은 모두 보여줍니다.
        "watch": ["채용", "근로자", "공무직", "임기제", "직원 모집", "인력 모집"],
    },
    {
        "name": "고양시 도서관센터", "region": "경기 고양",  # 모집공고 (관심 기관, 자원봉사 모집은 제외)
        "list": "https://www.goyanglib.or.kr/center/menu/130332/bbs/20005/bbsPostList.do?currentPageNo={page}",
        "id_re": r"fnDetail\('(\d+)'\)",
        "view": "https://www.goyanglib.or.kr/center/menu/130332/bbs/20005/bbsPostDetail.do?postIdx={id}",
        "watch": ["채용", "근로자", "공무직", "임기제", "기간제", "사서 모집", "직원 모집"],
    },
]

DATE_RE = re.compile(r"(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def _get(url: str) -> str:
    last = None
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=UA, timeout=30)
            raw = resp.content
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp949", "ignore")  # 서대문구 등 옛 한글 인코딩
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"연결 실패: {last}")


def _text(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_list(page_html: str, board: dict, base_url: str) -> list[dict]:
    rows = []
    for row in re.findall(r"<tr\b.*?</tr>", page_html, re.S | re.I):
        m = re.search(r"<a\b([^>]*)>(.*?)</a>", row, re.S | re.I)
        if not m:
            continue
        attrs, title = m.group(1), _text(m.group(2))
        title = re.sub(r"\s*(N|새글|NEW)$", "", title).strip()
        if len(title) < 6:
            continue

        url = None
        if board.get("id_re"):
            idm = re.search(board["id_re"], attrs)
            if idm:
                url = board["view"].format(id=idm.group(1))
        if not url:
            href = re.search(r'href="([^"]+)"', attrs)
            if href and not href.group(1).startswith(("#", "javascript")):
                url = urljoin(base_url, html.unescape(href.group(1)))
        if not url:
            continue

        dates = sorted({f"{y}-{int(mo):02d}-{int(d):02d}" for y, mo, d in DATE_RE.findall(_text(row))})
        rows.append({
            "title": title,
            "url": url,
            "posted": dates[0] if dates else None,
            # 날짜가 둘 이상이면 늦은 날짜를 마감일로 봅니다(접수마감일·게시종료일 칸)
            "end": dates[-1] if len(dates) >= 2 and dates[-1] > dates[0] else None,
        })
    return rows


def fetch_board(board: dict) -> list[dict]:
    found, seen = [], set()
    for page in range(1, PAGES + 1):
        url = board["list"].format(page=page)
        rows = parse_list(_get(url), board, url)
        new = [r for r in rows if r["url"] not in seen]
        if not new:  # 쪽 번호가 안 먹히면 같은 목록이 반복되므로 멈춥니다
            break
        for r in new:
            seen.add(r["url"])
            found.append(r)
        time.sleep(1)  # 구청 서버에 부담을 주지 않도록 천천히
    return found
