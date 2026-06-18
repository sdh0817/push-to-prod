"""iperf3 서버/클라이언트 실행 및 결과 파싱 (Step 03, Windows 버전).

시스템 구성:
    [PC 이더넷 - iperf3 server]  <-- AP -->  [PC Wi-Fi 2 - iperf3 client]

스펙 문서(03)는 Linux(eth0/wlan0, ip addr) 기준이나 실행 환경이 Windows이므로:
    · 인터페이스 IP   : PowerShell Get-NetIPAddress
    · iperf3 실행파일 : PATH 우선, 없으면 winget 설치 경로 자동 탐지
    · 기본 인터페이스 : '이더넷'(서버 바인딩) / 'Wi-Fi 2'(클라이언트 바인딩)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

DEFAULT_PORT = 5201
ERROR_MARKERS = ("error", "refused", "timed out", "unable to connect",
                 "no route", "connect failed")


# ---------- iperf3 실행파일 탐지 ----------
def find_iperf3() -> str:
    """iperf3.exe 경로 반환 (PATH 우선, 없으면 winget 패키지 경로 탐색)."""
    p = shutil.which("iperf3")
    if p:
        return p
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "Microsoft", "WinGet", "Packages")
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            if "iperf3.exe" in files:
                return os.path.join(root, "iperf3.exe")
    return "iperf3"  # 최후의 수단


_IPERF3 = find_iperf3()


# ---------- 인터페이스 IP ----------
def get_interface_ip(interface: str) -> str | None:
    """지정 인터페이스의 IPv4 주소 반환, 없으면 None. APIPA(169.254.*) 제외."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetIPAddress -InterfaceAlias '{interface}' "
             f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    for line in (proc.stdout or "").splitlines():
        ip = line.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) and not ip.startswith("169.254."):
            return ip
    return None


# ---------- iperf3 서버 ----------
def start_iperf3_server(bind_ip: str, port: int = DEFAULT_PORT) -> subprocess.Popen:
    """iperf3 서버를 백그라운드로 기동, Popen 반환."""
    cmd = [_IPERF3, "-s", "-B", bind_ip, "-p", str(port)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def stop_iperf3_server(proc: subprocess.Popen) -> None:
    """iperf3 서버 종료 (포트 점유 해제). 미기동/이미종료도 안전."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------- 결과 파싱 ----------
_UNIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}  # → Mbits/sec 배율


def _to_mbps(value: float, prefix: str) -> float:
    return round(value * _UNIT.get(prefix.upper(), 1.0), 2)


def parse_iperf3_output(raw: str, direction: str) -> dict:
    """iperf3 텍스트 출력에서 throughput(Mbps) 추출.

    summary 라인 예:
        [  5]  0.00-10.00 sec  1.10 GBytes   944 Mbits/sec   receiver
    'receiver'(실수신 처리량) 우선, 없으면 'sender' 값을 사용.
    """
    result = {"DL_Mbps": None, "UL_Mbps": None, "status": "OK", "raw_log": raw}

    if any(m in raw.lower() for m in ERROR_MARKERS):
        result["status"] = "FAIL"
        return result

    pat = re.compile(r"([\d.]+)\s*([KMG]?)bits/sec.*?\b(receiver|sender)\b",
                     re.IGNORECASE)
    found = {"receiver": None, "sender": None}
    for m in pat.finditer(raw):
        found[m.group(3).lower()] = _to_mbps(float(m.group(1)), m.group(2))

    mbps = found["receiver"] if found["receiver"] is not None else found["sender"]
    if mbps is None:
        result["status"] = "NO_RESULT"
        return result

    if direction.upper() == "DL":
        result["DL_Mbps"] = mbps
    elif direction.upper() == "UL":
        result["UL_Mbps"] = mbps
    return result


def _run_one(server_ip: str, bind_ip: str, reverse: bool,
             port: int, duration: int) -> str:
    """단일 방향 iperf3 클라이언트 실행, raw 출력(stdout+stderr) 반환."""
    cmd = [_IPERF3, "-c", server_ip, "-B", bind_ip, "-p", str(port),
           "-t", str(duration)]
    if reverse:
        cmd.append("-R")  # DL: 서버 → 클라이언트
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=duration + 15,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "error: timed out"
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return f"error: {e}"
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def run_iperf3_client(
    server_ip: str,
    bind_ip: str,
    direction: str,        # "DL" | "UL" | "Both"
    port: int = DEFAULT_PORT,
    duration: int = 30,
) -> dict:
    """방향에 따라 iperf3 클라이언트 실행 후 결과 dict 반환.

    반환 예: {"DL_Mbps": 312.4, "UL_Mbps": 198.1, "status": "OK", "raw_log": "..."}
    """
    direction = direction.upper()
    out = {"DL_Mbps": None, "UL_Mbps": None, "status": "OK", "raw_log": ""}

    if direction in ("DL", "BOTH"):
        raw = _run_one(server_ip, bind_ip, True, port, duration)
        r = parse_iperf3_output(raw, "DL")
        out["DL_Mbps"] = r["DL_Mbps"]
        out["raw_log"] += "[DL]\n" + raw + "\n"
        if r["status"] != "OK":
            out["status"] = r["status"]

    if direction == "BOTH":
        time.sleep(2)  # DL 세션 정리 대기

    if direction in ("UL", "BOTH"):
        raw = _run_one(server_ip, bind_ip, False, port, duration)
        r = parse_iperf3_output(raw, "UL")
        out["UL_Mbps"] = r["UL_Mbps"]
        out["raw_log"] += "[UL]\n" + raw + "\n"
        if r["status"] != "OK" and out["status"] == "OK":
            out["status"] = r["status"]

    return out


if __name__ == "__main__":
    print(f"iperf3 경로 : {_IPERF3}")
    eth = sys.argv[1] if len(sys.argv) > 1 else "이더넷"
    wifi = sys.argv[2] if len(sys.argv) > 2 else "Wi-Fi 2"
    print(f"이더넷({eth}) IP : {get_interface_ip(eth)}")
    print(f"Wi-Fi({wifi}) IP : {get_interface_ip(wifi)}")
