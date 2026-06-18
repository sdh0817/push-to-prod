"""WiFi OTA RvR 결과 CSV → RSSI vs 처리량 그래프(PNG) 생성.

사용법:
    python plot_results.py                  # results/ 최신 CSV 자동 사용
    python plot_results.py results/xxx.csv  # 특정 CSV 지정

X축: 실측 RSSI(dBm, 강한 신호가 왼쪽), Y축: 처리량(Mbps). DL/UL 두 곡선.
PNG는 입력 CSV와 같은 경로/이름(.png)으로 저장된다.
"""

from __future__ import annotations

import csv
import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")  # 화면 없이 파일로 저장
import matplotlib.pyplot as plt

for _f in ("Malgun Gothic", "맑은 고딕", "Gulim"):  # Windows 한글 폰트
    try:
        matplotlib.rcParams["font.family"] = _f
        break
    except Exception:
        continue
matplotlib.rcParams["axes.unicode_minus"] = False  # 음수 부호 깨짐 방지


def latest_csv() -> str | None:
    files = glob.glob(os.path.join("results", "*.csv"))
    return max(files, key=os.path.getmtime) if files else None


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rssi = float(r["RSSI_dBm"])
            except (ValueError, TypeError, KeyError):
                continue

            def num(key):
                try:
                    return float(r.get(key) or 0)
                except ValueError:
                    return 0.0

            rows.append({"rssi": rssi,
                         "dl": num("Throughput_DL_Mbps"),
                         "ul": num("Throughput_UL_Mbps"),
                         "status": r.get("Status", "")})
    rows.sort(key=lambda x: x["rssi"])
    return rows


def plot(path: str) -> str:
    rows = load_csv(path)
    if not rows:
        raise SystemExit(f"[오류] CSV에 유효한 데이터가 없습니다: {path}")

    rssi = [r["rssi"] for r in rows]
    dl = [r["dl"] for r in rows]
    ul = [r["ul"] for r in rows]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(rssi, dl, "o-", color="#1f77b4", linewidth=2, markersize=8,
            label="DL (다운로드)")
    if any(u > 0 for u in ul):
        ax.plot(rssi, ul, "s--", color="#d62728", linewidth=2, markersize=7,
                label="UL (업로드)")
    for x, y in zip(rssi, dl):
        ax.annotate(f"{y:g}", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=9, color="#1f77b4")

    ax.set_xlabel("RSSI (dBm)  —  오른쪽일수록 약한 신호", fontsize=11)
    ax.set_ylabel("처리량 (Mbps)", fontsize=11)
    ax.set_title("WiFi OTA RvR — RSSI vs 처리량", fontsize=13, fontweight="bold")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.set_ylim(bottom=0)       # 0부터 — 작은 변화 과장 방지(정직한 스케일)
    ax.invert_xaxis()           # 강한 신호(큰 값)를 왼쪽으로 → RvR 곡선 직관화
    ax.legend(fontsize=10)
    fig.tight_layout()

    out = os.path.splitext(path)[0] + ".png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else latest_csv()
    if not path or not os.path.exists(path):
        raise SystemExit("CSV를 찾을 수 없습니다. results/ 에 결과가 있는지 확인하세요.")
    print(f"입력 CSV   : {path}")
    print(f"그래프 저장: {plot(path)}")
