"""ATT6000 USB 시리얼 가변 감쇠기 드라이버 (Step 01).

WTDZ ATT6000을 USB 시리얼(115200 baud)로 제어한다.

명령 규격:
    'wv' + 5자리 숫자(값을 100배, zero-pad) + '\\n'
    예) 30.00 dB -> 'wv03000\\n'
        60.00 dB -> 'wv06000\\n'
         0.25 dB -> 'wv00025\\n'

스펙 문서(00/01)는 Linux(/dev/ttyUSB0) 기준이지만 실행 환경이 Windows이므로
포트는 'COM4' 형식도 동일하게 지원한다 (pyserial은 양쪽 동일 API).

최대 감쇠는 기본 31.75 dB(127스텝 × 0.25 dB) — 실기기 ATT6000 1유닛 기준이다.
문서 01에는 90 dB로 적혀 있으나 단일 유닛 스펙은 31.75 dB이며, 캐스케이드 구성 시
ATT6000(max_db=...) 로 재정의할 수 있다.
"""

from __future__ import annotations

import sys
import time

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None


STEP_DB = 0.25       # 감쇠 분해능 (스텝)
MIN_DB = 0.0         # 최소 감쇠
MAX_DB = 31.75       # 최대 감쇠 (단일 유닛). 필요 시 생성자 인자로 변경.
SETTLE_SEC = 0.05    # set 후 안정화 대기 (50 ms)

# USB-시리얼 칩 식별 키워드 (포트 자동 탐지용)
_PORT_KEYWORDS = ("usb-serial", "ch340", "cp210", "ftdi", "prolific",
                  "att", "wtdz", "uart", "silicon labs")


def list_serial_ports() -> list[str]:
    """현재 인식된 모든 시리얼 포트명 목록."""
    if list_ports is None:
        return []
    return [p.device for p in list_ports.comports()]


def find_att6000_port() -> str | None:
    """ATT6000으로 추정되는 시리얼 포트를 자동 탐지.

    1) 설명/제조사에 USB-시리얼 칩 키워드가 들어간 포트 우선
    2) 그 외에는 포트가 정확히 하나일 때만 그 포트를 반환 (모호하면 None)
    """
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    if not ports:
        return None
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in desc for k in _PORT_KEYWORDS):
            return p.device
    return ports[0].device if len(ports) == 1 else None


class ATT6000:
    """ATT6000 가변 감쇠기 시리얼 제어 클래스 (context manager 지원)."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0,
                 max_db: float = MAX_DB):
        if serial is None:
            raise RuntimeError(
                "pyserial 미설치 — 'pip install pyserial' 후 다시 실행하세요.")
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.max_db = max_db
        self._ser: "serial.Serial | None" = None
        self._last_db: float = 0.0

    # ---- 연결 관리 ----
    def open(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
        except serial.SerialException as e:
            raise ConnectionError(
                f"ATT6000 포트 '{self.port}' 열기 실패: {e}\n"
                f"  · 포트명을 확인하세요 (Windows: COM4 / Linux: /dev/ttyUSB0)\n"
                f"  · 현재 인식된 포트: {list_serial_ports() or '없음'}\n"
                f"  · USB-시리얼 드라이버(CH340/CP210x/FTDI) 설치 여부를 확인하세요."
            ) from e

    def close(self) -> None:
        """이미 닫혔거나 열린 적 없어도 예외 없이 동작."""
        if self._ser is not None and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    # ---- 감쇠 제어 ----
    def _require_open(self) -> "serial.Serial":
        if self._ser is None or not self._ser.is_open:
            raise ConnectionError("포트가 열려 있지 않습니다. open()을 먼저 호출하세요.")
        return self._ser

    def set_attenuation(self, db: float) -> None:
        """감쇠 값을 0.25 dB 단위로 반올림하여 송신한다.

        범위(0.0 ~ max_db) 밖이면 ValueError.
        """
        if db < MIN_DB or db > self.max_db:
            raise ValueError(
                f"감쇠 값 {db} dB 범위 초과 (허용: {MIN_DB} ~ {self.max_db} dB)")
        ser = self._require_open()

        snapped = round(round(db / STEP_DB) * STEP_DB, 2)  # 0.25 단위 + 부동소수 정리
        cmd = f"wv{int(round(snapped * 100)):05d}\n"
        ser.write(cmd.encode("ascii"))
        ser.flush()
        self._last_db = snapped
        time.sleep(SETTLE_SEC)

    def get_attenuation(self) -> float:
        """마지막으로 설정한 감쇠 값(dB)."""
        return self._last_db

    def get_model(self) -> str | None:
        """'rid' 명령으로 모델명 조회 (연결 검증용). 무응답 시 None."""
        ser = self._require_open()
        try:
            ser.reset_input_buffer()
            ser.write(b"rid\n")
            ser.flush()
            time.sleep(0.1)
            resp = ser.readline().decode("ascii", errors="replace").strip()
            return resp or None
        except Exception:
            return None

    # ---- context manager ----
    def __enter__(self) -> "ATT6000":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# 단독 실행: 포트 탐지 + 송신 포맷 검증
if __name__ == "__main__":
    print("=== 인식된 시리얼 포트 ===")
    print(list_serial_ports() or "  (없음)")

    target = sys.argv[1] if len(sys.argv) > 1 else find_att6000_port()
    if not target:
        print("\nATT6000 포트를 찾지 못했습니다. 사용법: python att6000.py COM4")
        sys.exit(1)

    print(f"\nATT6000 포트: {target}")
    try:
        with ATT6000(target) as att:
            print(f"  모델(rid): {att.get_model() or '응답 없음'}")
            for v in (0.0, 10.0, 0.25, 31.75, 30.15):
                att.set_attenuation(v)
                applied = att.get_attenuation()
                print(f"  set {v:>6} dB -> 적용 {applied:.2f} dB "
                      f"(명령 wv{int(round(applied * 100)):05d})")
            att.set_attenuation(0.0)
            print("  복원: 0.00 dB")
    except (ConnectionError, ValueError) as e:
        print(f"\n[오류] {e}")
        sys.exit(1)
