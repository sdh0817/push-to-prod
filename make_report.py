"""WiFi OTA RvR 측정 결과 → 누구나 이해할 수 있는 HTML 보고서 생성.

사용법:
    python make_report.py                  # results/ 최신 CSV 자동 사용
    python make_report.py results/xxx.csv  # 특정 CSV 지정

산출물: 입력 CSV와 같은 이름의 report_*.html (그래프 PNG를 안에 포함 → 파일 하나로 공유 가능).
브라우저로 열어보고, Ctrl+P → 'PDF로 저장'으로 PDF 보고서도 만들 수 있다.
"""

from __future__ import annotations

import base64
import csv
import glob
import os
import sys
from datetime import datetime

# ---------- 데이터 로드 ----------
def latest_csv() -> str | None:
    files = glob.glob(os.path.join("results", "*.csv"))
    return max(files, key=os.path.getmtime) if files else None


def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            def num(k, d=0.0):
                try:
                    return float(r.get(k) or d)
                except ValueError:
                    return d
            rows.append({
                "ts": r.get("Timestamp", ""),
                "band": r.get("Band_GHz", ""),
                "ch": r.get("Channel", ""),
                "att": num("ATT_dB"),
                "rssi": num("RSSI_dBm"),
                "dl": num("Throughput_DL_Mbps"),
                "ul": num("Throughput_UL_Mbps"),
                "status": (r.get("Status") or "").strip(),
            })
    return rows


# ---------- 자동 분석 (쉬운 해석/주의 문구 생성) ----------
def analyze(rows: list[dict]) -> dict:
    rssis = [r["rssi"] for r in rows]
    atts = [r["att"] for r in rows]
    dls = [r["dl"] for r in rows]
    uls = [r["ul"] for r in rows]
    rssi_span = (max(rssis) - min(rssis)) if rssis else 0.0
    att_span = (max(atts) - min(atts)) if atts else 0.0
    statuses = [r["status"] for r in rows]
    n_ok = sum(s == "OK" for s in statuses)
    n_total = len(rows)

    notes = []     # 주의/이상 징후
    valid_rvr = True

    # 핵심 검증: 감쇠를 크게 줬는데 RSSI가 거의 안 변함 → RvR 구간 미형성
    max_att = max(atts) if atts else 0
    if att_span < 1.0 and max_att >= 30 and rssi_span < 5:
        valid_rvr = False
        notes.append(
            f"감쇠기를 최대치({max_att:.2f} dB) 부근에 고정했는데도 신호 세기(RSSI)가 "
            f"{rssi_span:.1f} dB밖에 변하지 않았습니다. 신호를 약하게 만드는 구간이 "
            f"형성되지 않아, ‘신호가 약해질수록 속도가 떨어진다’는 곡선을 확인할 수 없습니다.")
        notes.append(
            "원인 후보: ① 감쇠기가 실제 무선 경로에 효과적으로 삽입되지 않음(신호가 "
            "감쇠기를 우회/누설) ② 공유기와 너무 가까워 공간 결합 신호가 우세 ③ 안테나 "
            "연결/케이블 문제. → 감쇠기 연결 구성과 거리 점검 후 재측정 권장.")
    elif n_ok < n_total:
        notes.append(
            f"{n_total}개 중 {n_total - n_ok}개 포인트가 목표 RSSI에 정확히 수렴하지 "
            f"못해(WARN/SKIP/FAIL) 현재값으로 측정되었습니다. 목표 RSSI 범위가 현재 "
            "신호로 도달 가능한지 확인하세요.")

    # RvR 경향(유효할 때만): 신호 강→약으로 속도가 줄어드는지
    trend = ""
    if valid_rvr and len(rows) >= 2:
        s = sorted(rows, key=lambda x: x["rssi"], reverse=True)  # 강한 신호부터
        if s[0]["dl"] > s[-1]["dl"]:
            trend = "신호가 약해질수록 다운로드 속도가 낮아지는 정상적인 RvR 경향이 보입니다."
        else:
            trend = "신호 세기와 속도의 뚜렷한 비례 경향이 보이지 않습니다."

    return {
        "rssi_min": min(rssis) if rssis else 0, "rssi_max": max(rssis) if rssis else 0,
        "rssi_span": rssi_span, "att_min": min(atts) if atts else 0,
        "att_max": max(atts) if atts else 0, "att_span": att_span,
        "dl_min": min(dls) if dls else 0, "dl_max": max(dls) if dls else 0,
        "ul_min": min(uls) if uls else 0, "ul_max": max(uls) if uls else 0,
        "n_total": n_total, "n_ok": n_ok, "valid_rvr": valid_rvr,
        "notes": notes, "trend": trend,
    }


# ---------- HTML 생성 ----------
_STATUS_COLOR = {"OK": "#1a7f37", "WARN": "#bf8700", "SKIP": "#6e7781",
                 "FAIL": "#cf222e", "NO_RESULT": "#cf222e"}
_STATUS_KR = {"OK": "정상", "WARN": "주의(강행측정)", "SKIP": "건너뜀",
              "FAIL": "실패", "NO_RESULT": "결과없음"}


def _img_tag(png_path: str) -> str:
    if not png_path or not os.path.exists(png_path):
        return "<p style='color:#888'>(그래프 이미지 없음)</p>"
    with open(png_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f'<img src="data:image/png;base64,{b64}" alt="RvR 그래프">'


def build_html(csv_path: str) -> str:
    rows = load_rows(csv_path)
    if not rows:
        raise SystemExit(f"[오류] CSV에 데이터가 없습니다: {csv_path}")
    a = analyze(rows)
    png_path = os.path.splitext(csv_path)[0] + ".png"
    meta = rows[0]
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 결과 표 행
    tr = ""
    for i, r in enumerate(rows, 1):
        color = _STATUS_COLOR.get(r["status"], "#6e7781")
        label = _STATUS_KR.get(r["status"], r["status"])
        tr += f"""<tr>
          <td>{i}</td><td>{r['rssi']:.1f}</td><td>{r['att']:.2f}</td>
          <td>{r['dl']:.1f}</td><td>{r['ul']:.1f}</td>
          <td><span class="badge" style="background:{color}">{label}</span></td></tr>"""

    # 주의 박스
    notes_html = ""
    if a["notes"]:
        items = "".join(f"<li>{n}</li>" for n in a["notes"])
        box_cls = "warn" if not a["valid_rvr"] else "info"
        title = "⚠️ 이번 측정 데이터 해석 시 주의" if not a["valid_rvr"] else "ℹ️ 참고"
        notes_html = f'<div class="note {box_cls}"><h3>{title}</h3><ul>{items}</ul></div>'

    verdict = ("이번 데이터는 신호 약화 구간이 만들어지지 않아 RvR(신호 대비 속도) 곡선 "
               "판정에는 부적합합니다. 측정 구성 점검 후 재측정이 필요합니다."
               if not a["valid_rvr"]
               else (a["trend"] or "측정이 정상 완료되었습니다."))

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WiFi OTA RvR 측정 보고서</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Malgun Gothic","맑은 고딕",sans-serif; color:#1f2328;
         max-width: 960px; margin: 0 auto; padding: 32px 24px; line-height:1.6;
         background:#fff; }}
  h1 {{ font-size: 26px; margin:0 0 4px; }}
  h2 {{ font-size: 19px; border-bottom:2px solid #d0d7de; padding-bottom:6px;
        margin-top:34px; }}
  h3 {{ font-size: 15px; margin:0 0 8px; }}
  .sub {{ color:#656d76; font-size:13px; margin-bottom:20px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }}
  .card {{ flex:1 1 150px; background:#f6f8fa; border:1px solid #d0d7de;
           border-radius:10px; padding:14px 16px; }}
  .card .k {{ font-size:12px; color:#656d76; }}
  .card .v {{ font-size:22px; font-weight:700; margin-top:2px; }}
  .card .u {{ font-size:12px; color:#656d76; }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }}
  th,td {{ border:1px solid #d0d7de; padding:8px 10px; text-align:center; }}
  th {{ background:#f6f8fa; }}
  .badge {{ color:#fff; padding:2px 9px; border-radius:20px; font-size:12px;
            font-weight:600; }}
  .note {{ border-radius:10px; padding:14px 18px; margin:16px 0; }}
  .note.warn {{ background:#fff8c5; border:1px solid #d4a72c; }}
  .note.info {{ background:#ddf4ff; border:1px solid #54aeff; }}
  .note ul {{ margin:6px 0 0; padding-left:20px; }}
  .verdict {{ background:#f6f8fa; border-left:4px solid #0969da; padding:14px 18px;
              border-radius:4px; font-size:15px; margin:14px 0; }}
  img {{ max-width:100%; border:1px solid #d0d7de; border-radius:8px; }}
  .glossary dt {{ font-weight:700; margin-top:8px; }}
  .glossary dd {{ margin:0 0 0 0; color:#424a53; }}
  footer {{ margin-top:40px; color:#8c959f; font-size:12px;
            border-top:1px solid #d0d7de; padding-top:12px; }}
  @media print {{ body {{ padding:0; }} .note.warn {{ -webkit-print-color-adjust:exact; }} }}
</style></head><body>

<h1>WiFi 무선 성능 측정 보고서</h1>
<div class="sub">OTA(실제 무선) 환경 · {meta['band']} GHz · 채널 {meta['ch']} ·
  생성 {gen_time} · 원본 데이터: {os.path.basename(csv_path)}</div>

<h2>1. 이 측정이 무엇인가요?</h2>
<p>WiFi 신호를 일부러 단계별로 약하게 만들면서, 신호 세기에 따라 실제 통신 속도가
어떻게 변하는지를 자동으로 측정한 결과입니다. 흔히 <b>RvR(Rate vs Range,
신호 세기 대비 속도)</b> 시험이라고 부르며, "신호가 멀어지거나 약해질 때 속도가
얼마나 버텨주는가"를 보는 것이 목적입니다.</p>
<dl class="glossary">
  <dt>RSSI (신호 세기)</dt><dd>받는 무선 신호의 세기. 단위는 dBm이고, <b>0에 가까울수록(예: -20) 강하고, 숫자가 작아질수록(예: -85) 약합니다.</b></dd>
  <dt>처리량 (속도)</dt><dd>실제로 데이터가 오간 속도. 단위는 Mbps이며 클수록 빠릅니다. (DL=다운로드, UL=업로드)</dd>
  <dt>감쇠량 (ATT)</dt><dd>신호를 인위적으로 약하게 만든 양(dB). 기계(감쇠기)로 자동 조절합니다. 클수록 신호를 더 많이 줄인 것입니다.</dd>
</dl>

<h2>2. 한눈에 보는 요약</h2>
<div class="cards">
  <div class="card"><div class="k">측정 포인트</div><div class="v">{a['n_total']}개</div><div class="u">정상 수렴 {a['n_ok']}개</div></div>
  <div class="card"><div class="k">신호 세기 범위</div><div class="v">{a['rssi_max']:.0f} ~ {a['rssi_min']:.0f}</div><div class="u">dBm (변화폭 {a['rssi_span']:.1f} dB)</div></div>
  <div class="card"><div class="k">다운로드 속도</div><div class="v">{a['dl_min']:.0f} ~ {a['dl_max']:.0f}</div><div class="u">Mbps</div></div>
  <div class="card"><div class="k">업로드 속도</div><div class="v">{a['ul_min']:.0f} ~ {a['ul_max']:.0f}</div><div class="u">Mbps</div></div>
</div>
<div class="verdict"><b>결론:</b> {verdict}</div>
{notes_html}

<h2>3. 측정 결과 표</h2>
<table>
  <thead><tr><th>#</th><th>신호 세기<br>(RSSI, dBm)</th><th>감쇠량<br>(dB)</th>
    <th>다운로드<br>(Mbps)</th><th>업로드<br>(Mbps)</th><th>상태</th></tr></thead>
  <tbody>{tr}</tbody>
</table>
<p class="sub">상태 '정상'=목표 신호에 수렴 후 측정 / '주의'=목표 미수렴, 현재 신호로 강행 측정 /
'실패'=측정 불가.</p>

<h2>4. 그래프 — 신호 세기 vs 속도</h2>
{_img_tag(png_path)}
<p class="sub">가로축은 신호 세기(오른쪽일수록 약함), 세로축은 속도(Mbps).
정상적인 RvR이라면 왼쪽(강한 신호)에서 오른쪽(약한 신호)으로 갈수록 속도가 완만히 떨어집니다.</p>

<h2>5. 다음 단계 제안</h2>
<ul>
  {"".join(f"<li>{n}</li>" for n in a['notes']) if a['notes'] else "<li>측정이 정상 완료되었습니다. 동일 조건으로 재현성(반복 측정)을 확인하면 신뢰도가 높아집니다.</li>"}
  <li>감쇠기 연결과 안테나 경로를 점검한 뒤, 목표 RSSI를 현재 도달 가능한 범위로 조정해 재측정하면 깔끔한 RvR 곡선을 얻을 수 있습니다.</li>
</ul>

<footer>WiFi OTA RvR 자동화 시스템 · 자동 생성 보고서 · 측정 시각 {meta['ts']}</footer>
</body></html>"""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else latest_csv()
    if not path or not os.path.exists(path):
        raise SystemExit("CSV를 찾을 수 없습니다. results/ 에 결과가 있는지 확인하세요.")
    html = build_html(path)
    out = os.path.join(os.path.dirname(path),
                       "report_" + os.path.splitext(os.path.basename(path))[0] + ".html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"입력 CSV   : {path}")
    print(f"보고서 생성: {out}")
    print("  → 브라우저로 열고, Ctrl+P > 'PDF로 저장' 하면 PDF 보고서가 됩니다.")


if __name__ == "__main__":
    main()
