"""wlan RSSI 읽기 래퍼 (Step 02, Windows 버전).

스펙 문서(02)는 Linux `iw dev wlan0 link`의 `signal: -62 dBm` 파싱 구조이지만,
실행 환경이 Windows이므로 `netsh wlan show interfaces` 출력을 사용한다.

  · 일부 Windows 빌드는 'Rssi' 필드로 실제 dBm을 직접 제공 → 그대로 사용
  · 없으면 '신호(Signal)' 품질(%)을 dBm으로 근사 변환 (Microsoft 표준 매핑):
        dBm = (quality / 2) - 100
        예) 100% -> -50 dBm, 80% -> -60 dBm, 50% -> -75 dBm, 0% -> -100 dBm

주의: netsh wlan 조회는 Windows '위치 서비스(Location)'가 켜져 있어야 동작한다.
기본 인터페이스명은 USB 어댑터 'Wi-Fi 2'.
"""

from __future__ import annotations

import re
import statistics
import subprocess
import sys
import time

DEFAULT_INTERFACE = "Wi-Fi 2"
NETSH_TIMEOUT = 5  # 초

# netsh 출력 키 (한글/영문 모두 대응)
_NAME_KEYS = ("name", "이름")
_SIGNAL_KEYS = ("signal", "신호")
_STATE_KEYS = ("state", "상태")
_RSSI_KEYS = ("rssi",)
_CONNECTED_TOKENS = ("connected", "연결됨", "연결")
_DISCONNECT_TOKENS = ("disconnect", "연결 안", "연결되지")


def quality_to_dbm(quality: float) -> float:
    """신호 품질(%) → dBm 근사 변환."""
    return round((quality / 2.0) - 100.0, 1)


def _run_netsh() -> str | None:
    """`netsh wlan show interfaces` 실행 후 stdout 반환, 실패 시 None."""
    try:
        proc = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=NETSH_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return proc.stdout or None


def _parse_interfaces(raw: str) -> list[dict]:
    """netsh 출력을 인터페이스별 dict 리스트로 파싱."""
    blocks: list[dict] = []
    cur: dict | None = None
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key in _NAME_KEYS:
            cur = {"name": val}
            blocks.append(cur)
        elif cur is not None:
            if key in _RSSI_KEYS:
                cur["rssi"] = val
            elif key in _SIGNAL_KEYS:
                cur["signal"] = val
            elif key in _STATE_KEYS:
                cur["state"] = val
    return blocks


def get_rssi(interface: str = DEFAULT_INTERFACE) -> float | None:
    """지정 인터페이스의 현재 RSSI(dBm) 반환. 실패/미연결 시 None.

    예외를 던지지 않는다 (스펙 02 요구사항: 항상 None 또는 음수 float).
    """
    raw = _run_netsh()
    if not raw:
        return None
    for blk in _parse_interfaces(raw):
        if blk.get("name", "").lower() != interface.lower():
            continue
        state = blk.get("state", "").lower()
        if state:
            if any(t in state for t in _DISCONNECT_TOKENS):
                return None
            if not any(t in state for t in _CONNECTED_TOKENS):
                return None
        # 1순위: 실제 dBm을 주는 Rssi 필드
        rssi_raw = blk.get("rssi")
        if rssi_raw:
            m = re.search(r"-?\d+", rssi_raw)
            if m:
                return float(m.group(0))
        # 2순위: 신호 품질(%) → dBm 근사
        sig = blk.get("signal")
        if sig:
            m = re.search(r"(\d+)\s*%", sig)
            if m:
                return quality_to_dbm(float(m.group(1)))
        return None
    return None  # 해당 인터페이스 없음


def wait_for_rssi_stable(
    interface: str = DEFAULT_INTERFACE,
    samples: int = 3,
    interval: float = 1.0,
) -> float | None:
    """samples 회 연속 읽기 평균 RSSI 반환 (노이즈 완화). 모두 실패 시 None."""
    vals: list[float] = []
    for i in range(samples):
        v = get_rssi(interface)
        if v is not None:
            vals.append(v)
        if i < samples - 1:
            time.sleep(interval)
    return round(statistics.mean(vals), 1) if vals else None


if __name__ == "__main__":
    iface = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INTERFACE
    print(f"인터페이스: {iface}")
    rssi = get_rssi(iface)
    if rssi is None:
        print("RSSI 읽기 실패 (미연결 / netsh 오류 / 위치 서비스 OFF)")
        print("  → Windows 설정 > 개인정보 보호 > 위치 서비스를 켜야 조회됩니다.")
        sys.exit(1)
    print(f"현재 RSSI     : {rssi} dBm")
    print(f"3회 평균 RSSI : {wait_for_rssi_stable(iface, 3, 0.5)} dBm")
