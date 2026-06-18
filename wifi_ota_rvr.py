"""WiFi OTA RvR 자동화 메인 (Step 05, Windows 버전).

전체 흐름:
    초기 입력(인터랙티브)
      → 이더넷 IP 감지 & iperf3 서버 기동 (또는 원격 서버 사용)
      → RSSI 포인트 루프 (수렴 → iperf3 측정 → CSV 기록)
      → 요약 테이블 출력 → 그래프 저장
      → 안전 종료 (ATT 0 dB 복원 + 서버 종료)

스펙 문서(05)는 Linux(eth0/wlan0) 기준이나 실행 환경이 Windows이므로
인터페이스 기본값은 '이더넷'(서버) / 'Wi-Fi 2'(클라이언트)이다.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime

# Windows 콘솔 한글 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

from att6000 import ATT6000, find_att6000_port
from iperf3_runner import (get_interface_ip, run_iperf3_client,
                           start_iperf3_server, stop_iperf3_server)
from rssi_convergence import converge_to_rssi

RESULTS_DIR = "results"
CSV_HEADER = ["Timestamp", "Band_GHz", "Channel", "ATT_dB", "RSSI_dBm",
              "Throughput_DL_Mbps", "Throughput_UL_Mbps", "Status"]


# ---------- 입력/CSV 헬퍼 ----------
def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").replace("﻿", "").strip()
    return val or default


def parse_rssi_list(text: str) -> list[float]:
    return [float(t) for t in text.replace(" ", "").split(",") if t]


def ensure_csv(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(CSV_HEADER)


def append_csv(path: str, row: list) -> None:
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(row)


# ---------- 출력 ----------
def print_summary(summary, band, channel, direction, csv_path):
    print("\n" + "=" * 60)
    print(f"  WiFi OTA RvR 결과 요약  |  {band}GHz CH{channel}  |  {direction}")
    print("=" * 60)
    print(f"  {'목표RSSI':>8} {'ATT(dB)':>8} {'실측RSSI':>8} "
          f"{'DL(Mbps)':>10} {'UL(Mbps)':>10}  상태")
    for s in summary:
        rssi = f"{s['rssi']:.1f}" if isinstance(s["rssi"], (int, float)) else "N/A"
        print(f"  {s['target']:>8.0f} {s['att']:>8.2f} {rssi:>8} "
              f"{s['dl']:>10} {s['ul']:>10}  {s['status']}")
    print("=" * 60)
    print(f"  CSV 저장: {csv_path}")
    print("=" * 60)


# ---------- 메인 ----------
def main() -> int:
    print("=" * 60)
    print("  WiFi OTA RvR 자동화 (Windows)")
    print("=" * 60)

    # 초기 입력
    auto_port = find_att6000_port()
    port = ask("ATT6000 포트 (COM)", auto_port or "COM4")
    wlan = ask("WiFi 인터페이스명", "Wi-Fi 2")
    eth = ask("이더넷 인터페이스명", "이더넷")
    band = ask("Band [2.4 / 5]", "5")
    channel = ask("Channel", "36")
    direction = ask("방향 [DL / UL / Both]", "Both").upper().replace("BOTH", "Both")
    rssi_text = ask("목표 RSSI 목록(쉼표)", "-55,-65,-75,-85")
    try:
        duration = int(ask("iperf3 측정 시간(초)", "10"))
        targets = parse_rssi_list(rssi_text)
    except ValueError:
        print("[오류] 입력 형식이 잘못되었습니다 (측정시간/목표RSSI).")
        return 1
    if not targets:
        print("[오류] 목표 RSSI가 비어 있습니다.")
        return 1

    local_server = ask(
        "iperf3 서버를 이 PC에서 자동 기동? (노트북1에서 따로 돌리면 n) [y/n]", "y"
    ).lower().startswith("y")

    # 자동 설정
    eth_ip = get_interface_ip(eth)
    wlan_ip = get_interface_ip(wlan)
    print(f"  이더넷({eth}) IP : {eth_ip or '없음'}")
    print(f"  WiFi({wlan}) IP   : {wlan_ip or '없음'}")

    if local_server:
        server_ip = eth_ip or ask("  이더넷 IP 자동감지 실패 — 서버 바인딩 IP 직접 입력")
    else:
        server_ip = ask("  원격 iperf3 서버 IP (예: 노트북1 주소)", "192.168.50.5")
    if not wlan_ip:
        print("  [경고] WiFi IP 없음 — Wi-Fi 2 연결 상태를 확인하세요.")
        wlan_ip = ask("  WiFi 클라이언트 바인딩 IP 직접 입력")
    if not server_ip or not wlan_ip:
        print("[오류] 서버/클라이언트 IP가 필요합니다.")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(RESULTS_DIR, f"wifi_ota_rvr_{ts}.csv")
    ensure_csv(csv_path)

    server = att = None
    summary: list[dict] = []

    try:
        if local_server:
            print(f"\n  iperf3 서버 기동: -s -B {server_ip}")
            server = start_iperf3_server(server_ip)
        else:
            print(f"\n  원격 iperf3 서버 사용: {server_ip}")
            print("  ※ 노트북1에서 'iperf3 -s -B <IP>' 가 실행 중이어야 합니다.")

        att = ATT6000(port)
        att.open()
        att.set_attenuation(0.0)

        for target in targets:
            print(f"\n===== RSSI 포인트: {target:.0f} dBm =====")
            conv = converge_to_rssi(att, wlan, target)

            if conv["success"]:
                print(f"  [수렴 완료] ATT={conv['att_db']:.2f}dB / "
                      f"RSSI={conv['measured_rssi']:.1f}dBm ({conv['elapsed']}초)")
            else:
                print(f"  [수렴 실패: {conv.get('reason')}] "
                      f"ATT={conv['att_db']:.2f}dB / RSSI={conv['measured_rssi']}")
                if ask("  강행(g) / 스킵(s)?", "g").lower().startswith("s"):
                    append_csv(csv_path, [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), band, channel,
                        f"{conv['att_db']:.2f}", conv["measured_rssi"], 0, 0, "SKIP"])
                    summary.append({"target": target, "att": conv["att_db"],
                                    "rssi": conv["measured_rssi"], "dl": 0,
                                    "ul": 0, "status": "SKIP"})
                    continue

            res = run_iperf3_client(server_ip, wlan_ip, direction, duration=duration)
            dl, ul = res.get("DL_Mbps"), res.get("UL_Mbps")
            status = res.get("status", "OK")
            if dl is not None:
                print(f"  [iperf3 DL] {dl} Mbps")
            if ul is not None:
                print(f"  [iperf3 UL] {ul} Mbps")

            append_csv(csv_path, [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), band, channel,
                f"{conv['att_db']:.2f}", conv["measured_rssi"],
                dl if dl is not None else 0, ul if ul is not None else 0, status])
            print("  [CSV 기록 완료]")
            print("=" * 32)
            summary.append({"target": target, "att": conv["att_db"],
                            "rssi": conv["measured_rssi"], "dl": dl or 0,
                            "ul": ul or 0, "status": status})

        print_summary(summary, band, channel, direction, csv_path)

        try:
            import plot_results
            print(f"  그래프 저장: {plot_results.plot(csv_path)}")
            print("=" * 60)
        except Exception as e:
            print(f"  (그래프 생략 — matplotlib 미설치 등: {e})")

    except KeyboardInterrupt:
        print("\n\n[중단] Ctrl+C 감지 — 안전 종료 중...")
    finally:
        if att is not None:
            try:
                att.set_attenuation(0.0)
                print("  ATT6000 → 0 dB 복원")
            except Exception:
                pass
            att.close()
        if server is not None:
            stop_iperf3_server(server)
            print("  iperf3 서버 종료")

    return 0


if __name__ == "__main__":
    sys.exit(main())
