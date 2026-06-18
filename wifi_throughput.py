#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFi 쓰루풋 자동 측정 스크립트 (반자동)

가변감쇠기로 WiFi 신호를 단계별로 약하게 만들면서, 각 단계에서 iperf3로
쓰루풋(업로드/다운로드)을 측정하고 RSSI/링크레이트와 함께 기록·그래프화한다.

- 감쇠량은 사람이 다이얼로 수동 조정 → 스크립트는 Enter 입력 대기 방식.
- OS(Windows/Linux)를 자동 감지하여 무선 정보 조회 명령을 달리한다.
- 측정은 iperf3 -J(JSON) 출력으로 안정적으로 파싱한다.

실행 예:
    python wifi_throughput.py --server 192.168.0.1
    python wifi_throughput.py --server 192.168.0.1 --iface wlan0 --duration 10
"""

import argparse
import csv
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# OS 감지
# ---------------------------------------------------------------------------

def detect_os():
    """'windows' 또는 'linux' 반환."""
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s.startswith("linux"):
        return "linux"
    # macOS 등은 미지원
    return s


OS = detect_os()


# ---------------------------------------------------------------------------
# 공용 유틸
# ---------------------------------------------------------------------------

def run_cmd(cmd, timeout=15):
    """명령 실행 후 (returncode, stdout, stderr) 반환. 실패해도 예외 없이 처리."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Windows 콘솔 인코딩 문제 완화
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except FileNotFoundError:
        return -2, "", "NOT_FOUND"


def first_float(text):
    """문자열에서 첫 번째 실수 추출, 없으면 None."""
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


# ---------------------------------------------------------------------------
# 무선 인터페이스 / 링크 정보 조회
# ---------------------------------------------------------------------------

def get_wireless_interface_linux():
    """`iw dev`로 무선 인터페이스명 자동 탐지. 첫 번째 인터페이스 반환."""
    rc, out, _ = run_cmd(["iw", "dev"])
    if rc != 0:
        return None
    ifaces = re.findall(r"Interface\s+(\S+)", out)
    return ifaces[0] if ifaces else None


def get_wireless_interface_windows():
    """`netsh wlan show interfaces`로 무선 인터페이스명 탐지."""
    rc, out, _ = run_cmd(["netsh", "wlan", "show", "interfaces"])
    if rc != 0:
        return None
    # 'Name'/'이름' 라벨이 있는 첫 줄에서 값 추출
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        k = key.strip().lower()
        if k in ("name", "이름"):
            return val.strip()
    return None


def get_wireless_interface():
    if OS == "linux":
        return get_wireless_interface_linux()
    if OS == "windows":
        return get_wireless_interface_windows()
    return None


def _percent_to_dbm(percent):
    """Windows netsh는 신호를 %로 준다. MS 통상 근사식으로 dBm 환산.
    dBm = (percent / 2) - 100  (대략 0%→-100dBm, 100%→-50dBm)
    """
    if percent is None:
        return None
    return round((percent / 2.0) - 100.0, 1)


def get_wifi_info_linux(iface):
    """iw dev <iface> link 파싱 → {connected, ssid, rssi_dbm, link_rate_mbps}."""
    info = {"connected": False, "ssid": None, "rssi_dbm": None, "link_rate_mbps": None}
    rc, out, _ = run_cmd(["iw", "dev", iface, "link"])
    if rc != 0:
        return info
    if "Not connected" in out or out.strip() == "":
        return info
    info["connected"] = True

    m = re.search(r"SSID:\s*(.+)", out)
    if m:
        info["ssid"] = m.group(1).strip()

    m = re.search(r"signal:\s*(-?\d+)", out)
    if m:
        info["rssi_dbm"] = float(m.group(1))

    # rx/tx bitrate 중 더 낮은(또는 rx) 값을 링크레이트로 사용
    rates = []
    for m in re.finditer(r"(?:rx|tx) bitrate:\s*([\d.]+)\s*MBit/s", out):
        rates.append(float(m.group(1)))
    if rates:
        info["link_rate_mbps"] = max(rates)
    return info


def get_wifi_info_windows(iface=None):
    """netsh wlan show interfaces 파싱 → {connected, ssid, rssi_dbm, link_rate_mbps}.
    한글/영문 Windows 라벨 모두 대응. iface 지정 시 해당 인터페이스 블록만 사용.
    """
    info = {"connected": False, "ssid": None, "rssi_dbm": None, "link_rate_mbps": None}
    rc, out, _ = run_cmd(["netsh", "wlan", "show", "interfaces"])
    if rc != 0:
        return info

    # 인터페이스가 여러 개면 'Name/이름' 기준으로 블록 분리
    blocks = re.split(r"\n\s*\n", out)
    target = None
    for b in blocks:
        if iface is None:
            target = out  # 단일 처리
            break
        if re.search(r"(?:Name|이름)\s*:\s*" + re.escape(iface), b):
            target = b
            break
    if target is None:
        target = out

    signal_pct = None
    rx_rate = None
    tx_rate = None
    state_connected = False

    for line in target.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        k = key.strip().lower()
        v = val.strip()

        # 연결 상태
        if k in ("state", "상태"):
            if any(t in v.lower() for t in ("connected", "연결됨", "연결")):
                state_connected = True
        # SSID (BSSID 제외: 정확히 'ssid'만)
        elif k == "ssid":
            info["ssid"] = v
        # 신호 (%)
        elif k in ("signal", "신호"):
            signal_pct = first_float(v)
        # 수신 속도
        elif "receive rate" in k or "수신 속도" in k:
            rx_rate = first_float(v)
        # 송신/전송 속도
        elif "transmit rate" in k or "송신 속도" in k or "전송 속도" in k:
            tx_rate = first_float(v)

    info["connected"] = state_connected and (info["ssid"] not in (None, ""))
    info["rssi_dbm"] = _percent_to_dbm(signal_pct)
    rates = [r for r in (rx_rate, tx_rate) if r is not None]
    if rates:
        info["link_rate_mbps"] = max(rates)
    return info


def get_wifi_info(iface):
    if OS == "linux":
        return get_wifi_info_linux(iface)
    if OS == "windows":
        return get_wifi_info_windows(iface)
    return {"connected": False, "ssid": None, "rssi_dbm": None, "link_rate_mbps": None}


# ---------------------------------------------------------------------------
# iperf3 측정
# ---------------------------------------------------------------------------

def check_iperf3():
    """iperf3 실행 파일 존재 여부 확인."""
    return shutil.which("iperf3") is not None


def run_iperf3(server_ip, duration=10, reverse=False, port=5201):
    """iperf3 1회 실행 → (throughput_mbps, retransmits, ok).
    reverse=True 면 -R(다운로드: 서버→클라이언트). JSON으로 파싱.
    ok=False 면 연결 실패/타임아웃/에러.
    """
    cmd = ["iperf3", "-c", server_ip, "-t", str(duration), "-i", "1",
           "-p", str(port), "-J"]
    if reverse:
        cmd.append("-R")

    # iperf3 -t 시간 + 여유 버퍼
    rc, out, err = run_cmd(cmd, timeout=duration + 15)

    if rc == -1:  # TIMEOUT
        return None, None, False
    if rc == -2:  # iperf3 없음
        return None, None, False

    # JSON 파싱
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None, None, False

    if "error" in data:
        return None, None, False

    end = data.get("end", {})
    # 보낸 쪽/받은 쪽 합계
    sum_sent = end.get("sum_sent", {})
    sum_recv = end.get("sum_received", {})

    if reverse:
        # 다운로드: 클라이언트가 받은 속도
        bps = sum_recv.get("bits_per_second") or sum_sent.get("bits_per_second")
    else:
        # 업로드: 클라이언트가 보낸 속도
        bps = sum_sent.get("bits_per_second") or sum_recv.get("bits_per_second")

    retrans = sum_sent.get("retransmits")
    throughput_mbps = round(bps / 1e6, 2) if bps else None
    return throughput_mbps, retrans, True


def run_iperf3_with_retry(server_ip, duration, reverse, port):
    """실패 시 1회 재시도."""
    res = run_iperf3(server_ip, duration, reverse, port)
    if not res[2]:
        time.sleep(2)
        res = run_iperf3(server_ip, duration, reverse, port)
    return res


# ---------------------------------------------------------------------------
# 측정 1회 (한 감쇠 단계)
# ---------------------------------------------------------------------------

def measure_once(attenuation_db, server_ip, iface, duration, port):
    """한 감쇠 단계 측정 → 결과 dict 반환.
    링크가 끊겨 있으면 status='단절' 반환.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wifi = get_wifi_info(iface)

    if not wifi["connected"]:
        return {
            "attenuation_db": attenuation_db,
            "rssi_dbm": wifi.get("rssi_dbm"),
            "link_rate_mbps": wifi.get("link_rate_mbps"),
            "throughput_up_mbps": None,
            "throughput_down_mbps": None,
            "retransmits": None,
            "timestamp": ts,
            "status": "단절",
        }

    # 업로드(forward) 측정
    up_mbps, up_retrans, up_ok = run_iperf3_with_retry(server_ip, duration, False, port)
    # 다운로드(reverse) 측정
    down_mbps, _, down_ok = run_iperf3_with_retry(server_ip, duration, True, port)

    status = "정상"
    if not up_ok and not down_ok:
        status = "단절"  # 링크가 끊겨 iperf3 둘 다 실패

    return {
        "attenuation_db": attenuation_db,
        "rssi_dbm": wifi.get("rssi_dbm"),
        "link_rate_mbps": wifi.get("link_rate_mbps"),
        "throughput_up_mbps": up_mbps,
        "throughput_down_mbps": down_mbps,
        "retransmits": up_retrans,
        "timestamp": ts,
        "status": status,
    }


# ---------------------------------------------------------------------------
# 콘솔 표 출력
# ---------------------------------------------------------------------------

HEADERS = ["감쇠(dB)", "RSSI(dBm)", "링크(Mbps)", "UP(Mbps)", "DOWN(Mbps)", "재전송", "상태"]
COLW = [9, 10, 11, 10, 11, 8, 6]


def print_table_header():
    line = "".join(h.ljust(w) for h, w in zip(HEADERS, COLW))
    print("\n" + line)
    print("-" * sum(COLW))


def print_row(r):
    def fmt(v):
        return "-" if v is None else str(v)
    cells = [
        fmt(r["attenuation_db"]),
        fmt(r["rssi_dbm"]),
        fmt(r["link_rate_mbps"]),
        fmt(r["throughput_up_mbps"]),
        fmt(r["throughput_down_mbps"]),
        fmt(r["retransmits"]),
        r["status"],
    ]
    print("".join(c.ljust(w) for c, w in zip(cells, COLW)))


# ---------------------------------------------------------------------------
# 결과 저장 / 그래프
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "attenuation_db", "rssi_dbm", "link_rate_mbps",
    "throughput_up_mbps", "throughput_down_mbps",
    "retransmits", "timestamp", "status",
]
CSV_HEADER_KR = [
    "감쇠량(dB)", "RSSI(dBm)", "링크레이트(Mbps)",
    "쓰루풋_업로드(Mbps)", "쓰루풋_다운로드(Mbps)",
    "재전송횟수", "측정시각", "상태",
]


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER_KR)
        for r in rows:
            w.writerow([r[k] for k in CSV_FIELDS])
    print(f"[저장] CSV: {path}")


def save_plots(rows, prefix):
    try:
        import matplotlib
        matplotlib.use("Agg")  # GUI 없는 환경 대응
        import matplotlib.pyplot as plt
    except ImportError:
        print("[경고] matplotlib 미설치 → 그래프 생략. (pip install matplotlib)")
        return

    # 정상 측정값만 추출
    valid = [r for r in rows if r["status"] != "단절"]
    if not valid:
        print("[경고] 유효 측정값 없음 → 그래프 생략.")
        return

    att = [r["attenuation_db"] for r in valid]
    up = [r["throughput_up_mbps"] for r in valid]
    down = [r["throughput_down_mbps"] for r in valid]
    rssi = [r["rssi_dbm"] for r in valid]

    # 1) 감쇠량 vs 쓰루풋
    plt.figure(figsize=(8, 5))
    if any(v is not None for v in up):
        plt.plot(att, up, "o-", label="Upload")
    if any(v is not None for v in down):
        plt.plot(att, down, "s-", label="Download")
    plt.xlabel("Attenuation (dB)")
    plt.ylabel("Throughput (Mbps)")
    plt.title("Attenuation vs Throughput")
    plt.grid(True, alpha=0.3)
    plt.legend()
    p1 = f"{prefix}_atten_vs_throughput.png"
    plt.savefig(p1, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"[저장] 그래프: {p1}")

    # 2) RSSI vs 쓰루풋
    if any(v is not None for v in rssi):
        plt.figure(figsize=(8, 5))
        if any(v is not None for v in up):
            plt.plot(rssi, up, "o-", label="Upload")
        if any(v is not None for v in down):
            plt.plot(rssi, down, "s-", label="Download")
        plt.xlabel("RSSI (dBm)")
        plt.ylabel("Throughput (Mbps)")
        plt.title("RSSI vs Throughput")
        plt.grid(True, alpha=0.3)
        plt.legend()
        p2 = f"{prefix}_rssi_vs_throughput.png"
        plt.savefig(p2, dpi=120, bbox_inches="tight")
        plt.close()
        print(f"[저장] 그래프: {p2}")


# ---------------------------------------------------------------------------
# 환경 파악 출력
# ---------------------------------------------------------------------------

def print_environment(iface, server_ip):
    print("=" * 60)
    print(" WiFi 쓰루풋 자동 측정 — 환경 파악")
    print("=" * 60)
    print(f"  OS              : {platform.system()} ({OS})")
    print(f"  무선 인터페이스 : {iface or '미탐지'}")
    print(f"  iperf3 서버 IP  : {server_ip}")
    print(f"  iperf3 설치     : {'OK' if check_iperf3() else '미설치'}")

    wifi = get_wifi_info(iface) if iface else {}
    print(f"  현재 SSID       : {wifi.get('ssid')}")
    print(f"  현재 RSSI       : {wifi.get('rssi_dbm')} dBm")
    print(f"  현재 링크레이트 : {wifi.get('link_rate_mbps')} Mbps")
    print("=" * 60)

    if not check_iperf3():
        print("\n[안내] iperf3가 없습니다. 설치 후 다시 실행하세요.")
        if OS == "linux":
            print("  Ubuntu/Debian : sudo apt install iperf3")
            print("  Fedora        : sudo dnf install iperf3")
        else:
            print("  Windows : winget install iperf3   또는 https://iperf.fr 에서 다운로드")
        return False
    return True


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------

def parse_steps(steps_str):
    if not steps_str:
        return None
    out = []
    for tok in steps_str.replace(" ", "").split(","):
        if tok == "":
            continue
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out or None


def main():
    ap = argparse.ArgumentParser(
        description="WiFi 쓰루풋 자동 측정 (가변감쇠기 + iperf3)")
    ap.add_argument("--server", "-s", help="iperf3 서버(노트북1/공유기) IP")
    ap.add_argument("--iface", "-i", help="무선 인터페이스명 (미지정 시 자동 탐지)")
    ap.add_argument("--duration", "-t", type=int, default=10,
                    help="단계별 iperf3 측정 시간(초), 기본 10")
    ap.add_argument("--port", "-p", type=int, default=5201,
                    help="iperf3 포트, 기본 5201")
    ap.add_argument("--steps", help="감쇠 단계 미리 지정 (예: '0,3,6,10,15,20'). "
                                     "미지정 시 매 단계 수동 입력.")
    args = ap.parse_args()

    # 서버 IP
    server_ip = args.server
    if not server_ip:
        server_ip = input("iperf3 서버(노트북1/공유기) IP 입력: ").strip()
    if not server_ip:
        print("[오류] 서버 IP가 필요합니다.")
        sys.exit(1)

    # 인터페이스
    iface = args.iface or get_wireless_interface()
    if not iface:
        print("[경고] 무선 인터페이스를 자동 탐지하지 못했습니다. "
              "--iface 로 직접 지정하세요. (계속 진행은 가능하나 RSSI/링크레이트 미수집)")

    # 환경 파악
    if not print_environment(iface, server_ip):
        sys.exit(1)

    preset_steps = parse_steps(args.steps)
    rows = []
    print_table_header()

    try:
        if preset_steps:
            # 미리 지정된 단계 순회
            for db in preset_steps:
                input(f"\n>> 감쇠기를 {db}dB 로 맞추고 Enter 를 누르세요...")
                r = measure_once(db, server_ip, iface, args.duration, args.port)
                rows.append(r)
                print_row(r)
                if r["status"] == "단절":
                    print("\n[종료] 링크 단절 감지 → 측정 루프를 종료합니다.")
                    break
        else:
            # 완전 수동: 매번 감쇠량 입력, 빈 입력 시 종료
            print("\n각 단계마다 감쇠량(dB)을 입력하세요. (빈 줄 입력 시 측정 종료)")
            while True:
                raw = input("\n>> 감쇠기를 맞춘 뒤, 현재 감쇠량(dB) 입력 (종료: 빈 줄): ").strip()
                if raw == "":
                    break
                try:
                    db = float(raw)
                except ValueError:
                    print("  숫자를 입력하세요.")
                    continue
                r = measure_once(db, server_ip, iface, args.duration, args.port)
                rows.append(r)
                print_row(r)
                if r["status"] == "단절":
                    print("\n[종료] 링크 단절 감지 → 측정 루프를 종료합니다.")
                    break
    except KeyboardInterrupt:
        print("\n\n[중단] 사용자가 측정을 중단했습니다. 지금까지 결과를 저장합니다.")

    # 결과 저장
    if not rows:
        print("\n측정된 데이터가 없습니다. 종료합니다.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = f"results_{stamp}.csv"
    save_csv(rows, csv_path)
    save_plots(rows, f"results_{stamp}")

    print("\n[완료] 총 {}개 단계 측정.".format(len(rows)))


if __name__ == "__main__":
    main()
